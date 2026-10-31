package com.qqmonitor.uploader;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 手机端录音清单：MediaStore 的本地缓存 + 电脑处理状态（md5 作为业务键）。 */
public final class RecordingsDb extends SQLiteOpenHelper {
    private static final String DB = "recordings.db";
    private static final int VERSION = 1;

    public static final class Row {
        public long mediaId;
        public String md5 = "";
        public String name = "";
        public String uri = "";
        public long size;
        public long modified;
        public long duration;
        public String course = "";
        public String date = "";
        public boolean valid;
        public String reason = "";
        public int uploadState;      // 0 未传 / 2 已传
        public long serverId;
        public String serverStatus = "";
        public String serverDetail = "";
        public String mdPath = "";
        public boolean missing;

        public boolean canUpload() {
            return !missing && (uploadState != 2 || "missing".equals(serverStatus));
        }
    }

    public static final class ServerRow {
        public long id;
        public String filename = "";
        public String md5 = "";
        public long size;
        public String status = "";
        public String detail = "";
        public String mdPath = "";
        public String course = "";
        public String date = "";
    }

    public RecordingsDb(Context c) {
        super(c.getApplicationContext(), DB, null, VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE recording ("
                + "media_id INTEGER PRIMARY KEY,"
                + "md5 TEXT,"
                + "name TEXT,"
                + "uri TEXT,"
                + "size INTEGER,"
                + "modified INTEGER,"
                + "duration INTEGER,"
                + "course TEXT,"
                + "rec_date TEXT,"
                + "valid INTEGER,"
                + "reason TEXT,"
                + "upload_state INTEGER DEFAULT 0,"
                + "server_id INTEGER DEFAULT 0,"
                + "server_status TEXT DEFAULT '',"
                + "server_detail TEXT DEFAULT '',"
                + "md_path TEXT DEFAULT '',"
                + "missing INTEGER DEFAULT 0,"
                + "updated_at INTEGER)");
        db.execSQL("CREATE INDEX idx_rec_md5 ON recording(md5)");
        db.execSQL("CREATE INDEX idx_rec_size ON recording(size)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        // v1 起步，暂无迁移
    }

    /** 自动上传只看数据库状态，避免旧 Prefs 上传标记把“电脑无记录”的文件永久跳过。 */
    public synchronized boolean pendingUpload(String name, long size) {
        try (Cursor c = getReadableDatabase().rawQuery(
                "SELECT upload_state, valid FROM recording WHERE name=? AND size=?",
                new String[]{name, String.valueOf(size)})) {
            while (c.moveToNext()) {
                if (c.getInt(0) == 0 && c.getInt(1) == 1) return true;
            }
        }
        return false;
    }

    public synchronized void applyScan(List<Scanner.Item> items, Prefs prefs) {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        try {
            // 已上传的记录不因 MediaStore 重建/ID 变化而误报“手机里找不到”
            db.execSQL("UPDATE recording SET missing=0 WHERE upload_state = 2");
            db.execSQL("UPDATE recording SET missing=1 WHERE upload_state != 2");
            for (Scanner.Item it : items) {
                Cursor c = db.rawQuery(
                        "SELECT media_id, upload_state, md5, server_status FROM recording WHERE media_id=?",
                        new String[]{String.valueOf(it.mediaId)});
                boolean exists = c.moveToFirst();
                long rowMediaId = exists ? c.getLong(0) : -1;
                int state = exists ? c.getInt(1) : 0;
                String md5 = exists && !c.isNull(2) ? c.getString(2) : "";
                String serverStatus = exists && !c.isNull(3) ? c.getString(3) : "";
                c.close();
                if (!exists) {
                    // 手机重装/相册重建后 media_id 会变，退化为按文件名+大小匹配旧记录
                    Cursor old = db.rawQuery(
                            "SELECT media_id, upload_state, md5, server_status FROM recording WHERE name=? AND size=? LIMIT 1",
                            new String[]{it.name, String.valueOf(it.size)});
                    if (old.moveToFirst()) {
                        exists = true;
                        rowMediaId = old.getLong(0);
                        state = old.getInt(1);
                        md5 = old.isNull(2) ? "" : old.getString(2);
                        serverStatus = old.isNull(3) ? "" : old.getString(3);
                    }
                    old.close();
                }
                // 电脑已明确“无记录”时，不能再用本地旧上传标记把它重新算成已上传，
                // 否则它会永远从自动上传队列里消失；同时要把上一次误升级的 2 降回 0。
                if ("missing".equals(serverStatus)) {
                    if (state == 2) state = 0;
                    // 电脑明确没有这条记录：清掉旧的本地上传标记，
                    // 让自动上传服务重新把它当成待传文件，而不是永久跳过。
                    prefs.clearUploaded(it.key());
                } else if (prefs.isUploaded(it.key()) && state == 0) {
                    state = 2;
                }
                ContentValues v = new ContentValues();
                v.put("media_id", it.mediaId);
                v.put("name", it.name);
                v.put("uri", it.uri.toString());
                v.put("size", it.size);
                v.put("modified", it.modified);
                v.put("duration", it.duration);
                v.put("course", it.course == null ? "" : it.course);
                v.put("rec_date", it.date == null ? "" : it.date);
                v.put("valid", it.valid ? 1 : 0);
                v.put("reason", it.reason == null ? "" : it.reason);
                v.put("upload_state", state);
                v.put("missing", 0);
                v.put("updated_at", System.currentTimeMillis());
                if (exists) {
                    if (md5 != null && !md5.isEmpty()) v.put("md5", md5);
                    db.update("recording", v, "media_id=?", new String[]{String.valueOf(rowMediaId)});
                } else {
                    db.insert("recording", null, v);
                }
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
    }

    public synchronized List<Row> list() {
        List<Row> out = new ArrayList<>();
        try (Cursor c = getReadableDatabase().rawQuery(
                "SELECT media_id, md5, name, uri, size, modified, duration, course,"
                        + " rec_date, valid, reason, upload_state, server_id,"
                        + " server_status, server_detail, md_path, missing"
                        + " FROM recording ORDER BY missing ASC, modified DESC", null)) {
            while (c.moveToNext()) {
                Row r = new Row();
                r.mediaId = c.getLong(0);
                r.md5 = c.isNull(1) ? "" : c.getString(1);
                r.name = c.isNull(2) ? "" : c.getString(2);
                r.uri = c.isNull(3) ? "" : c.getString(3);
                r.size = c.getLong(4);
                r.modified = c.getLong(5);
                r.duration = c.getLong(6);
                r.course = c.isNull(7) ? "" : c.getString(7);
                r.date = c.isNull(8) ? "" : c.getString(8);
                r.valid = c.getInt(9) != 0;
                r.reason = c.isNull(10) ? "" : c.getString(10);
                r.uploadState = c.getInt(11);
                r.serverId = c.getLong(12);
                r.serverStatus = c.isNull(13) ? "" : c.getString(13);
                r.serverDetail = c.isNull(14) ? "" : c.getString(14);
                r.mdPath = c.isNull(15) ? "" : c.getString(15);
                r.missing = c.getInt(16) != 0;
                out.add(r);
            }
        }
        // 同一文件可能因 MediaStore 重建出现新旧两行：显示层合并，不删数据。
        Map<String, Row> unique = new LinkedHashMap<>();
        for (Row r : out) {
            String key = (r.name.isEmpty() || r.size <= 0)
                    ? ("id:" + r.mediaId) : (r.name + "|" + r.size);
            Row prev = unique.get(key);
            if (prev == null || better(r, prev)) unique.put(key, r);
        }
        return new ArrayList<>(unique.values());
    }

    private static boolean better(Row a, Row b) {
        int sa = (a.valid ? 4 : 0) + (a.uploadState == 2 ? 2 : 0) + (a.serverId > 0 ? 1 : 0);
        int sb = (b.valid ? 4 : 0) + (b.uploadState == 2 ? 2 : 0) + (b.serverId > 0 ? 1 : 0);
        if (sa != sb) return sa > sb;
        return a.mediaId > b.mediaId;
    }

    public synchronized void setMd5(long mediaId, String md5) {
        ContentValues v = new ContentValues();
        v.put("md5", md5);
        v.put("updated_at", System.currentTimeMillis());
        getWritableDatabase().update("recording", v, "media_id=?",
                new String[]{String.valueOf(mediaId)});
    }

    /** 读已缓存的 md5（从未算过时返回空串），上传时提前告诉电脑端，便于它收包前判重。 */
    public synchronized String md5For(long mediaId) {
        try (Cursor c = getReadableDatabase().rawQuery(
                "SELECT md5 FROM recording WHERE media_id=? LIMIT 1",
                new String[]{String.valueOf(mediaId)})) {
            if (c.moveToFirst() && !c.isNull(0)) {
                String md5 = c.getString(0);
                return md5 == null ? "" : md5;
            }
        } catch (Exception ignored) {
            // 读不到就当没有：上传照常继续，电脑端会自己算。
        }
        return "";
    }

    public synchronized void markUploaded(long mediaId, String md5) {
        ContentValues v = new ContentValues();
        v.put("upload_state", 2);
        v.put("updated_at", System.currentTimeMillis());
        if (md5 != null && !md5.isEmpty()) v.put("md5", md5);
        getWritableDatabase().update("recording", v, "media_id=?",
                new String[]{String.valueOf(mediaId)});
    }

    /** 用电脑返回的列表回填：优先 md5，其次 name+size。 */
    public synchronized void applyServer(List<ServerRow> rows) {
        SQLiteDatabase db = getWritableDatabase();
        Map<String, ServerRow> byMd5 = new HashMap<>();
        Map<String, ServerRow> byNameSize = new HashMap<>();
        for (ServerRow s : rows) {
            if (s.md5 != null && !s.md5.isEmpty()) byMd5.put(s.md5, s);
            byNameSize.put(s.filename + "|" + s.size, s);
        }
        java.util.Set<Long> matched = new java.util.HashSet<>();
        try (Cursor c = db.rawQuery(
                "SELECT media_id, md5, name, size FROM recording", null)) {
            while (c.moveToNext()) {
                long mediaId = c.getLong(0);
                String md5 = c.isNull(1) ? "" : c.getString(1);
                String name = c.isNull(2) ? "" : c.getString(2);
                long size = c.getLong(3);
                ServerRow s = !md5.isEmpty() ? byMd5.get(md5) : null;
                if (s == null) s = byNameSize.get(name + "|" + size);
                if (s == null) continue;
                matched.add(mediaId);
                ContentValues v = new ContentValues();
                v.put("upload_state", 2);
                v.put("server_id", s.id);
                v.put("server_status", s.status);
                v.put("server_detail", s.detail == null ? "" : s.detail);
                v.put("md_path", s.mdPath == null ? "" : s.mdPath);
                if (s.md5 != null && !s.md5.isEmpty()) v.put("md5", s.md5);
                v.put("updated_at", System.currentTimeMillis());
                db.update("recording", v, "media_id=?",
                        new String[]{String.valueOf(mediaId)});
            }
        }
        // 电脑端已没有记录的本地行：清掉旧状态，明确提示可重新上传（不自动重传，避免误判）
        try (Cursor c = db.rawQuery(
                "SELECT media_id FROM recording WHERE server_id>0 OR server_status<>'' OR upload_state=2", null)) {
            while (c.moveToNext()) {
                long mediaId = c.getLong(0);
                if (matched.contains(mediaId)) continue;
                ContentValues v = new ContentValues();
                v.put("upload_state", 0);
                v.put("server_id", 0);
                v.put("server_status", "missing");
                v.put("server_detail", "电脑端无记录，可重新上传");
                v.put("md_path", "");
                v.put("updated_at", System.currentTimeMillis());
                db.update("recording", v, "media_id=?",
                        new String[]{String.valueOf(mediaId)});
            }
        }
    }
}
