package com.qqmonitor.uploader;

import android.content.Context;
import android.content.SharedPreferences;

/** 配置与已上传集合。 */
public final class Prefs {
    private static final String FILE = "uploader";
    private final SharedPreferences sp;

    public Prefs(Context ctx) {
        sp = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public String server() {
        return sp.getString("server", "");
    }

    public void setServer(String v) {
        sp.edit().putString("server", v.trim()).apply();
    }

    public String token() {
        return sp.getString("token", "");
    }

    public void setToken(String v) {
        sp.edit().putString("token", v.trim()).apply();
    }

    public String courses() {
        // 示例课表，只用于首次启动；请在「设置 → 白名单」里按自己的课表改。
        return sp.getString("courses", "高等数学,线性代数,大学英语,大学物理,程序设计,体育");
    }

    /** 把后来新增的默认分类补进用户已有的名单（不覆盖自定义项）。 */
    public void ensureCourse(String course) {
        String csv = courses();
        for (String part : csv.split(",")) {
            if (part.trim().equals(course)) {
                return;
            }
        }
        setCourses(csv + "," + course);
    }

    public void setCourses(String v) {
        sp.edit().putString("courses", v.trim()).apply();
    }

    public int intervalMinutes() {
        return sp.getInt("interval", 15);
    }

    public void setInterval(int v) {
        sp.edit().putInt("interval", Math.max(5, v)).apply();
    }

    public boolean isUploaded(String key) {
        return sp.getBoolean("done_" + key, false);
    }

    public void markUploaded(String key) {
        sp.edit().putBoolean("done_" + key, true).apply();
    }

    public void clearUploaded(String key) {
        sp.edit().remove("done_" + key).apply();
    }

    public boolean welcomeDone() {
        return sp.getBoolean("welcome_done", false);
    }

    public void setWelcomeDone(boolean v) {
        sp.edit().putBoolean("welcome_done", v).apply();
    }

    public int uploadedCount() {
        return sp.getInt("uploaded_count", 0);
    }

    public void incUploaded() {
        sp.edit().putInt("uploaded_count", uploadedCount() + 1).apply();
    }

    public String lastUpload() {
        return sp.getString("last_upload", "");
    }

    public void setLastUpload(String v) {
        sp.edit().putString("last_upload", v).apply();
    }

    public long lastScanAt() {
        return sp.getLong("last_scan", 0L);
    }

    public void setLastScanAt(long v) {
        sp.edit().putLong("last_scan", v).apply();
    }

    /** 后台定时扫描是否开启（停止服务后为 false）。 */
    public boolean serviceEnabled() {
        return sp.getBoolean("service_enabled", welcomeDone());
    }

    public void setServiceEnabled(boolean v) {
        sp.edit().putBoolean("service_enabled", v).apply();
    }

    /* ---- 单文件失败退避：连续失败 3 次后暂停 30 分钟，避免每轮都卡在同一个大文件上 ---- */

    public int failCount(String key) {
        return sp.getInt("fail_" + key, 0);
    }

    public void markFailed(String key) {
        sp.edit().putInt("fail_" + key, failCount(key) + 1)
                .putLong("fail_at_" + key, System.currentTimeMillis())
                .apply();
    }

    public void clearFailed(String key) {
        sp.edit().remove("fail_" + key).remove("fail_at_" + key).apply();
    }

    /** true 表示暂时跳过（失败满 3 次且 30 分钟内没有再次尝试）。 */
    public boolean inFailureCooldown(String key) {
        if (failCount(key) < 3) {
            return false;
        }
        long at = sp.getLong("fail_at_" + key, 0L);
        return System.currentTimeMillis() - at < 30L * 60L * 1000L;
    }
}
