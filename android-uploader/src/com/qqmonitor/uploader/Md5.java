package com.qqmonitor.uploader;

import android.content.Context;
import android.net.Uri;

import java.io.InputStream;
import java.security.MessageDigest;

/** 懒计算文件 md5（只在首次上传/同步时算一次）。 */
public final class Md5 {
    private Md5() {
    }

    public static String of(Context ctx, Uri uri) throws Exception {
        MessageDigest md = MessageDigest.getInstance("MD5");
        try (InputStream in = ctx.getContentResolver().openInputStream(uri)) {
            if (in == null) throw new IllegalStateException("无法读取文件");
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                md.update(buf, 0, n);
            }
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) {
            sb.append(String.format("%02x", b & 0xff));
        }
        return sb.toString();
    }
}
