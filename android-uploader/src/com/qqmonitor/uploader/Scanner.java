package com.qqmonitor.uploader;

import android.content.ContentUris;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.MediaStore;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** 从 MediaStore 扫描手机录音：既给出全部音频（列表页），也给出可自动上传的合规项。 */
public final class Scanner {
    private static final Pattern NAME = Pattern.compile(
            "^(.+?)(\\d{4})[年./_\\-](\\d{1,2})[月./_\\-](\\d{1,2})日?(?:[_\\-\\s]?\\d{1,2})?\\.([A-Za-z0-9]{2,5})$",
            Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE);

    /** 电脑端能解码的格式（与 app/recordings.py SUPPORTED_EXTS 保持一致）。 */
    private static final Set<String> EXTS = new HashSet<>(Arrays.asList(
            "mp3", "m4a", "m4b", "wav", "amr", "aac", "ogg", "opus",
            "flac", "wma", "aiff", "aif", "caf", "mp2",
            "mp4", "m4v", "mkv", "webm", "mov", "avi", "3gp", "3gpp"));

    public static final class Item {
        public final long mediaId;
        public final String name;
        public final long size;
        public final long modified;
        public final long duration;
        public final Uri uri;
        public final String course;
        public final String date;
        public final boolean valid;
        public final String reason;

        Item(long mediaId, String name, long size, long modified, long duration,
             Uri uri, String course, String date, boolean valid, String reason) {
            this.mediaId = mediaId;
            this.name = name;
            this.size = size;
            this.modified = modified;
            this.duration = duration;
            this.uri = uri;
            this.course = course;
            this.date = date;
            this.valid = valid;
            this.reason = reason;
        }

        /** 兼容旧逻辑：已上传判断键。 */
        public String key() {
            return name + "|" + size + "|" + modified;
        }
    }

    private Scanner() {
    }

    public static List<Item> candidates(Context ctx) {
        List<Item> out = new ArrayList<>();
        for (Item it : allAudio(ctx)) {
            if (it.valid) out.add(it);
        }
        return out;
    }

    /** 全部音频文件（含文件名不合规/格式不支持的，供列表页展示）。 */
    public static List<Item> allAudio(Context ctx) {
        List<Item> out = new ArrayList<>();
        String[] courses = splitCourses(new Prefs(ctx).courses());
        String[] proj = {
                MediaStore.Audio.Media._ID,
                MediaStore.Audio.Media.DISPLAY_NAME,
                MediaStore.Audio.Media.SIZE,
                MediaStore.Audio.Media.DATE_MODIFIED,
                MediaStore.Audio.Media.DURATION,
        };
        try (Cursor c = ctx.getContentResolver().query(
                MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, proj,
                null, null, MediaStore.Audio.Media.DATE_MODIFIED + " DESC")) {
            if (c == null) return out;
            int iId = c.getColumnIndexOrThrow(MediaStore.Audio.Media._ID);
            int iName = c.getColumnIndexOrThrow(MediaStore.Audio.Media.DISPLAY_NAME);
            int iSize = c.getColumnIndexOrThrow(MediaStore.Audio.Media.SIZE);
            int iMod = c.getColumnIndexOrThrow(MediaStore.Audio.Media.DATE_MODIFIED);
            int iDur = c.getColumnIndexOrThrow(MediaStore.Audio.Media.DURATION);
            while (c.moveToNext()) {
                String name = c.getString(iName);
                if (name == null || name.isEmpty()) continue;
                long id = c.getLong(iId);
                Uri uri = ContentUris.withAppendedId(
                        MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, id);
                Valid v = validate(name, courses);
                out.add(new Item(id, name, c.getLong(iSize), c.getLong(iMod),
                        c.getLong(iDur), uri, v.course, v.date, v.ok, v.reason));
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    public static final class Valid {
        public final boolean ok;
        public final String course;
        public final String date;
        public final String reason;

        Valid(boolean ok, String course, String date, String reason) {
            this.ok = ok;
            this.course = course;
            this.date = date;
            this.reason = reason;
        }
    }

    public static Valid validate(String name, String[] courses) {
        int dot = name.lastIndexOf('.');
        String ext = dot >= 0 ? name.substring(dot + 1).toLowerCase(Locale.ROOT) : "";
        if (!EXTS.contains(ext)) {
            return new Valid(false, "", "", "不支持的格式 ." + (ext.isEmpty() ? "?" : ext));
        }
        Matcher m = NAME.matcher(name);
        if (!m.matches()) {
            return new Valid(false, "", "", "未匹配「分类名+年月日」");
        }
        String course = m.group(1) == null ? "" : m.group(1).trim();
        if (course.isEmpty()) {
            return new Valid(false, "", "", "缺少分类名");
        }
        boolean allowed = false;
        for (String s : courses) {
            if (s.equals(course)) {
                allowed = true;
                break;
            }
        }
        if (!allowed && !course.contains("会议")) {
            return new Valid(false, course, "", "分类「" + course + "」不在白名单");
        }
        String date = String.format(Locale.ROOT, "%s-%02d-%02d", m.group(2),
                Integer.parseInt(m.group(3)), Integer.parseInt(m.group(4)));
        return new Valid(true, course, date, "");
    }

    private static String[] splitCourses(String raw) {
        if (raw == null) return new String[0];
        String[] parts = raw.split("[,，\\s]+");
        List<String> out = new ArrayList<>();
        for (String p : parts) {
            if (p != null && !p.trim().isEmpty()) out.add(p.trim());
        }
        return out.toArray(new String[0]);
    }
}
