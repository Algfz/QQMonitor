package com.qqmonitor.uploader;

import android.content.Context;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/** 把单个录音以 multipart 形式上传到 PC 的 /api/recordings/upload。 */
public final class Uploader {

    /** 建连：局域网/Tailscale 握手通常 <1s，10s 足够；弱网下也不至于秒失败。 */
    private static final int CONNECT_TIMEOUT_MS = 10000;
    /** 读：服务端收完文件要落盘、算 md5、入库，600s 太长，60s 对几十 MB 的录音够用。 */
    private static final int READ_TIMEOUT_MS = 60000;
    /** 写停滞阈值：HttpURLConnection 没有写超时，只能靠看门狗盯进度。 */
    private static final long WRITE_STALL_MS = 120000;

    public static final class Result {
        public final boolean ok;
        public final String message;

        Result(boolean ok, String message) {
            this.ok = ok;
            this.message = message;
        }
    }

    private Uploader() {
    }

    /**
     * @param md5 本地已算好的 md5（可为空串）。服务端仍会自己重算一遍，这里只是提前给出，
     *            让它有机会在收包前判重；空串时行为与旧版完全一致。
     */
    public static Result upload(Context ctx, Scanner.Item item, Prefs prefs, String base,
                                String md5) {
        String token = prefs.token();
        if (base == null || base.isEmpty()) {
            return new Result(false, "未找到可用的服务器地址");
        }
        if (token.isEmpty()) {
            return new Result(false, "未配置访问 token");
        }
        String endpoint = base.replaceAll("/+$", "") + "/api/recordings/upload";
        String boundary = "----QQMonitor" + System.currentTimeMillis();
        String safeMd5 = md5 == null ? "" : md5.trim();
        HttpURLConnection conn = null;
        AtomicLong progress = new AtomicLong(System.currentTimeMillis());
        AtomicBoolean done = new AtomicBoolean(false);
        AtomicBoolean stalled = new AtomicBoolean(false);
        try {
            URL url = new URL(endpoint);
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setRequestProperty("Authorization", "Bearer " + token);
            conn.setRequestProperty("Content-Type",
                    "multipart/form-data; boundary=" + boundary);
            if (!safeMd5.isEmpty()) {
                // 双保险：header 方便中间层/服务端提前判断，multipart 字段保证不被代理丢掉。
                conn.setRequestProperty("X-QQMonitor-Md5", safeMd5);
            }

            String encodedName = URLEncoder.encode(item.name, "UTF-8").replace("+", "%20");
            StringBuilder head = new StringBuilder();
            if (!safeMd5.isEmpty()) {
                head.append("--").append(boundary).append("\r\n")
                        .append("Content-Disposition: form-data; name=\"md5\"\r\n\r\n")
                        .append(safeMd5).append("\r\n");
            }
            head.append("--").append(boundary).append("\r\n")
                    .append("Content-Disposition: form-data; name=\"file\"; filename=\"")
                    .append(asciiName(item.name)).append("\";")
                    .append(" filename*=UTF-8''").append(encodedName).append("\r\n")
                    .append("Content-Type: audio/mpeg\r\n\r\n");
            byte[] headerBytes = head.toString().getBytes(StandardCharsets.UTF_8);
            byte[] tailBytes = ("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8);
            long size = item.size;
            if (size > 0) {
                conn.setFixedLengthStreamingMode(headerBytes.length + size + tailBytes.length);
            } else {
                conn.setChunkedStreamingMode(1 << 16);
            }

            Thread watchdog = startWriteWatchdog(conn, progress, done, stalled);
            try (OutputStream out = conn.getOutputStream()) {
                progress.set(System.currentTimeMillis());
                out.write(headerBytes);
                try (InputStream in = new BufferedInputStream(
                        ctx.getContentResolver().openInputStream(item.uri))) {
                    if (in == null) {
                        return new Result(false, "无法读取录音文件");
                    }
                    byte[] buf = new byte[1 << 16];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                        progress.set(System.currentTimeMillis());
                    }
                }
                out.write(tailBytes);
                out.flush();
            } finally {
                done.set(true);
                watchdog.interrupt();
            }

            int code = conn.getResponseCode();
            String body = readBody(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code >= 200 && code < 300) {
                return new Result(true, "已上传（" + code + "）");
            }
            String hint = body == null ? "" : body;
            if (hint.length() > 200) {
                hint = hint.substring(0, 200);
            }
            return new Result(false, "HTTP " + code + "：" + hint);
        } catch (Exception e) {
            if (stalled.get()) {
                return new Result(false, "上传超时：已 120 秒没有进度（网络可能已断开）");
            }
            String reason = e.getMessage();
            if (reason == null || reason.isEmpty()) reason = e.getClass().getSimpleName();
            return new Result(false, "上传异常：" + reason);
        } finally {
            done.set(true);
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /**
     * 写停滞看门狗：HttpURLConnection 只支持连接/读超时，写方一旦对端不 ACK 会一直阻塞。
     * 每 120s 没有新进度就主动断开连接，让上面的写循环抛异常返回，而不是永久挂起。
     */
    private static Thread startWriteWatchdog(final HttpURLConnection conn,
                                             final AtomicLong progress,
                                             final AtomicBoolean done,
                                             final AtomicBoolean stalled) {
        Thread t = new Thread(() -> {
            while (!done.get()) {
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException e) {
                    return;
                }
                if (done.get()) {
                    return;
                }
                if (System.currentTimeMillis() - progress.get() > WRITE_STALL_MS) {
                    stalled.set(true);
                    try {
                        conn.disconnect();
                    } catch (Exception ignored) {
                    }
                    return;
                }
            }
        }, "upload-watchdog");
        t.setDaemon(true);
        t.start();
        return t;
    }

    /** 纯 ASCII 兜底文件名；真实中文名走 RFC 5987 的 filename*，老解析器看这个字段也不会崩。 */
    private static String asciiName(String name) {
        if (name == null || name.isEmpty()) return "upload.mp3";
        StringBuilder sb = new StringBuilder(name.length());
        for (int i = 0; i < name.length(); i++) {
            char c = name.charAt(i);
            if (c >= 0x20 && c <= 0x7e && c != '"' && c != '\\') {
                sb.append(c);
            } else {
                sb.append('_');
            }
        }
        return sb.toString();
    }

    private static String readBody(InputStream in) {
        if (in == null) {
            return "";
        }
        try (InputStream s = in; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buf = new byte[4096];
            int n;
            while ((n = s.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            return out.toString(StandardCharsets.UTF_8.name());
        } catch (Exception e) {
            return "";
        }
    }
}
