package com.qqmonitor.uploader;

import android.content.Context;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.Deque;
import java.util.Locale;

/** 简单的本地日志（Activity 里显示最近 200 行）。 */
public final class LogStore {
    private static final int MAX_LINES = 200;
    private static final String NAME = "upload.log";

    private LogStore() {
    }

    public static synchronized void add(Context ctx, String msg) {
        try {
            File f = new File(ctx.getFilesDir(), NAME);
            Deque<String> lines = readLines(f);
            String ts = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA).format(new Date());
            lines.addLast(ts + "  " + msg);
            while (lines.size() > MAX_LINES) {
                lines.removeFirst();
            }
            StringBuilder sb = new StringBuilder();
            for (String l : lines) {
                sb.append(l).append('\n');
            }
            try (FileOutputStream out = new FileOutputStream(f)) {
                out.write(sb.toString().getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
        }
    }

    public static synchronized String read(Context ctx) {
        try {
            Deque<String> lines = readLines(new File(ctx.getFilesDir(), NAME));
            StringBuilder sb = new StringBuilder();
            for (String l : lines) {
                sb.append(l).append('\n');
            }
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private static Deque<String> readLines(File f) throws Exception {
        Deque<String> lines = new ArrayDeque<>();
        if (!f.exists()) {
            return lines;
        }
        try (BufferedReader r = new BufferedReader(
                new InputStreamReader(new FileInputStream(f), StandardCharsets.UTF_8))) {
            String line;
            while ((line = r.readLine()) != null) {
                lines.addLast(line);
            }
        }
        return lines;
    }
}
