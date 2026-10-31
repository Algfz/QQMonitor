package com.qqmonitor.uploader;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;

import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/** 前台服务：定时扫描 MediaStore，把合规录音上传到 PC。 */
public class UploadService extends Service {
    public static final String ACTION_SCAN = "com.qqmonitor.uploader.SCAN";
    public static final String ACTION_UPLOADED = "com.qqmonitor.uploader.UPLOADED";
    private static final String CHANNEL = "uploader";
    private static final int NOTIFY_ID = 7;

    private final AtomicBoolean running = new AtomicBoolean(false);
    private ExecutorService executor;
    private PowerManager.WakeLock wakeLock;

    public static void start(Context ctx) {
        new Prefs(ctx).setServiceEnabled(true);
        Intent i = new Intent(ctx, UploadService.class).setAction(ACTION_SCAN);
        if (Build.VERSION.SDK_INT >= 26) {
            ctx.startForegroundService(i);
        } else {
            ctx.startService(i);
        }
    }

    public static void schedule(Context ctx) {
        Prefs p = new Prefs(ctx);
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        Intent i = new Intent(ctx, UploadService.class).setAction(ACTION_SCAN);
        PendingIntent pi = PendingIntent.getService(ctx, 7, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        long every = Math.max(5, p.intervalMinutes()) * 60_000L;
        am.setInexactRepeating(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + 10_000L, every, pi);
    }

    public static void cancel(Context ctx) {
        new Prefs(ctx).setServiceEnabled(false);
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        Intent i = new Intent(ctx, UploadService.class).setAction(ACTION_SCAN);
        PendingIntent pi = PendingIntent.getService(ctx, 7, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        am.cancel(pi);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        executor = Executors.newSingleThreadExecutor();
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "录音上传", NotificationManager.IMPORTANCE_LOW);
            nm.createNotificationChannel(ch);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = notification("正在检查录音…", 0, 0);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFY_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIFY_ID, n);
        }
        if (!running.compareAndSet(false, true)) {
            return START_STICKY;
        }
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "QQMonitor:upload");
        wakeLock.acquire(60L * 60L * 1000L);
        executor.execute(this::doScan);
        return START_STICKY;
    }

    private void doScan() {
        Prefs prefs = new Prefs(this);
        prefs.setLastScanAt(System.currentTimeMillis());
        RecordingsDb db = new RecordingsDb(this);
        try {
            List<Scanner.Item> items = Scanner.candidates(this);
            int todo = 0;
            for (Scanner.Item it : items) {
                if (db.pendingUpload(it.name, it.size)) {
                    todo++;
                }
            }
            LogStore.add(this, "扫描到 " + items.size() + " 个合规录音，待上传 " + todo);
            int done = 0;
            int skipped = 0;
            String base;
            try {
                base = Api.resolveBase(prefs);
            } catch (IllegalStateException e) {
                // 探测失败不再静默：日志里带尝试过的地址列表和排查提示
                LogStore.add(this, e.getMessage() + "；本轮暂停");
                return;
            }
            LogStore.add(this, "使用服务器：" + base);
            for (Scanner.Item it : items) {
                if (!db.pendingUpload(it.name, it.size)) {
                    continue;
                }
                if (prefs.inFailureCooldown(it.key())) {
                    skipped++;
                    continue;
                }
                if (prefs.token().isEmpty()) {
                    LogStore.add(this, "未配置 token，跳过上传");
                    break;
                }
                notify("上传 " + it.name, done, todo);
                String md5 = db.md5For(it.mediaId);
                Uploader.Result r = Uploader.upload(this, it, prefs, base, md5);
                if (!r.ok && isNetworkError(r.message)) {
                    // 网络/隧道暂时不可用：不计失败、不进入退避，下一轮直接重试
                    LogStore.add(this, "网络不可达，本轮暂停：" + it.name);
                    break;
                }
                if (!r.ok && !Thread.currentThread().isInterrupted()) {
                    // 网络抖动/临时错误：立刻重试一次
                    try {
                        Thread.sleep(2000);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                    r = Uploader.upload(this, it, prefs, base, md5);
                }
                if (r.ok) {
                    prefs.markUploaded(it.key());
                    prefs.clearFailed(it.key());
                    prefs.incUploaded();
                    prefs.setLastUpload(new java.text.SimpleDateFormat("HH:mm", java.util.Locale.CHINA)
                            .format(new java.util.Date()) + "  " + it.name);
                    LogStore.add(this, "✓ " + it.name + "  " + r.message);
                    Intent uploaded = new Intent(ACTION_UPLOADED);
                    uploaded.setPackage(getPackageName());
                    sendBroadcast(uploaded);
                } else {
                    prefs.markFailed(it.key());
                    LogStore.add(this, "✗ " + it.name + "  " + r.message);
                    if (Thread.currentThread().isInterrupted()) {
                        LogStore.add(this, "上传被系统中断，下一轮继续");
                        break;
                    }
                }
                done++;
            }
            if (skipped > 0) {
                LogStore.add(this, "本轮跳过 " + skipped + " 个连续失败的文件（30 分钟后再试）");
            }
            LogStore.add(this, "本轮结束：成功 " + done + " 个");
        } catch (Exception e) {
            LogStore.add(this, "扫描失败：" + e.getMessage());
        } finally {
            running.set(false);
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
            wakeLock = null;
            stopForeground(true);
            stopSelf();
        }
    }

    private static boolean isNetworkError(String msg) {
        if (msg == null) return false;
        String m = msg.toLowerCase(java.util.Locale.ROOT);
        return m.contains("failed to connect") || m.contains("timeout") || m.contains("timed out")
                || m.contains("unreachable") || m.contains("unknownhost") || m.contains("unknown host")
                || m.contains("connection reset") || m.contains("network is unreachable")
                || m.contains("no route to host");
    }

    private void notify(String text, int done, int total) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.notify(NOTIFY_ID, notification(text, done, total));
    }

    private Notification notification(String text, int done, int total) {
        Intent open = new Intent(this, RecordingsActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        b.setContentTitle("录音上传")
                .setContentText(total > 0 ? text + "（" + done + "/" + total + "）" : text)
                .setSmallIcon(R.mipmap.ic_stat_upload)
                .setOngoing(true)
                .setContentIntent(pi);
        return b.build();
    }

    @Override
    public void onDestroy() {
        if (executor != null) {
            executor.shutdownNow();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
