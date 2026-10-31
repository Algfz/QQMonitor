package com.qqmonitor.uploader;

import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/** 只读接口：拉取电脑端录音列表（GET /api/recordings）。 */
public final class Api {
    private static final String TAG = "QQMonitor";
    /**
     * 本机回环兜底（USB 调试 adb reverse tcp:8088 tcp:8088，或本机代理）。
     * 明文 HTTP 只对这个地址放行，见 res/xml/network_security_config.xml。
     */
    private static final String LOOPBACK = "http://127.0.0.1:8088";

    private Api() {
    }

    /**
     * 探测服务器地址，返回第一个可用的 base。
     *
     * <p>顺序固定为「https 优先 → 用户配置的 http → 本机回环明文兜底」：
     * 先把地址按协议分成两桶，https 全试完才轮到 http，最后才是 {@link #LOOPBACK}。
     * 这样加密通道永远优先，明文只在前面全部失败时才出现，且只命中回环地址。
     *
     * @throws IllegalStateException 全部不可用；message 带尝试过的地址列表和排查提示，
     *         调用方（RecordingsActivity / UploadService）直接展示即可。
     */
    public static String resolveBase(Prefs prefs) {
        String raw = prefs.server();
        if (raw == null) raw = "";
        String token = prefs.token();

        List<String> secure = new ArrayList<>();
        List<String> plain = new ArrayList<>();
        for (String piece : raw.split("[,，\\s]+")) {
            String base = piece.trim();
            if (base.isEmpty()) continue;
            if (!base.startsWith("http://") && !base.startsWith("https://")) {
                // 没写协议的老写法按局域网 http 处理，但排在所有 https 之后。
                base = "http://" + base;
            }
            base = base.replaceAll("/+$", "");
            List<String> bucket = base.startsWith("https://") ? secure : plain;
            if (!bucket.contains(base)) bucket.add(base);
        }
        List<String> candidates = new ArrayList<>(secure);
        candidates.addAll(plain);
        if (!candidates.contains(LOOPBACK)) candidates.add(LOOPBACK);

        List<String> tried = new ArrayList<>();
        for (String base : candidates) {
            tried.add(base);
            if (probe(base, token)) {
                Log.i(TAG, "使用服务器 " + base + "（尝试顺序："
                        + String.join(" → ", tried) + "）");
                return base;
            }
        }
        String msg = "连不上电脑端（已试 " + String.join("、", tried) + "）。请确认："
                + "① 电脑端 QQMonitor 已启动且 8088 端口可访问；"
                + "② 手机与电脑在同一网络 / Tailscale 已连接；"
                + "③ 走 USB 调试需先在电脑执行 adb reverse tcp:8088 tcp:8088";
        Log.w(TAG, msg);
        throw new IllegalStateException(msg);
    }

    private static boolean probe(String base, String token) {
        HttpURLConnection conn = null;
        try {
            URL u = new URL(base + "/api/recordings?limit=1");
            conn = (HttpURLConnection) u.openConnection();
            // 探测只是连通性判断，超时比正式请求短，避免一个死地址拖住整个进度。
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(6000);
            conn.setRequestMethod("GET");
            conn.setRequestProperty("Authorization", "Bearer " + token);
            int code = conn.getResponseCode();
            if (code != 200) {
                Log.d(TAG, "探测 " + base + " → HTTP " + code);
            }
            return code == 200;
        } catch (Exception e) {
            // 不再静默：logcat 里能看清是超时、拒绝连接还是明文被系统拦截。
            String reason = e.getMessage() == null ? "" : "：" + e.getMessage();
            Log.d(TAG, "探测 " + base + " 失败（" + e.getClass().getSimpleName() + "）" + reason);
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    public static List<RecordingsDb.ServerRow> fetchRecordings(Prefs prefs, int limit)
            throws Exception {
        String token = prefs.token();
        if (token.isEmpty()) throw new IllegalStateException("未配置 token");
        String base = resolveBase(prefs);
        String endpoint = base.replaceAll("/+$", "")
                + "/api/recordings?limit=" + Math.max(1, Math.min(200, limit));
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(endpoint).openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(30000);
            conn.setRequestMethod("GET");
            conn.setRequestProperty("Authorization", "Bearer " + token);
            int code = conn.getResponseCode();
            String body = readBody(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code < 200 || code >= 300) {
                throw new IllegalStateException("HTTP " + code + "：" + trim(body));
            }
            JSONObject root = new JSONObject(body);
            JSONArray arr = root.optJSONArray("recordings");
            List<RecordingsDb.ServerRow> out = new ArrayList<>();
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.optJSONObject(i);
                    if (o == null) continue;
                    RecordingsDb.ServerRow r = new RecordingsDb.ServerRow();
                    r.id = o.optLong("id");
                    r.filename = o.optString("filename", "");
                    r.md5 = o.optString("md5", "");
                    r.size = o.optLong("size");
                    r.status = o.optString("status", "");
                    r.detail = o.optString("detail", "");
                    r.mdPath = o.optString("md_path", "");
                    r.course = o.optString("course", "");
                    r.date = o.optString("rec_date", "");
                    out.add(r);
                }
            }
            return out;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    public static void retry(Prefs prefs, long id) throws Exception {
        String token = prefs.token();
        if (token.isEmpty()) throw new IllegalStateException("未配置 token");
        String base = resolveBase(prefs);
        String endpoint = base.replaceAll("/+$", "") + "/api/recordings/retry";
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(endpoint).openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(60000);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            conn.setRequestProperty("Authorization", "Bearer " + token);
            byte[] body = ("{\"id\":" + id + "}").getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(body.length);
            try (java.io.OutputStream out = conn.getOutputStream()) {
                out.write(body);
            }
            int code = conn.getResponseCode();
            String text = readBody(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code < 200 || code >= 300) {
                throw new IllegalStateException("HTTP " + code + "：" + trim(text));
            }
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String readBody(InputStream in) throws Exception {
        if (in == null) return "";
        try (InputStream s = in; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }

    private static String trim(String s) {
        if (s == null) return "";
        return s.length() > 200 ? s.substring(0, 200) : s;
    }
}
