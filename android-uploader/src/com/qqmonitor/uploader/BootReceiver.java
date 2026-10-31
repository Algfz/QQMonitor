package com.qqmonitor.uploader;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 开机后重新排定时任务（不直接上传，避免被系统限制）。 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            return;
        }
        Prefs p = new Prefs(context);
        if (p.token().isEmpty()) {
            return;
        }
        UploadService.schedule(context);
        UploadService.start(context);
        LogStore.add(context, "开机完成，已恢复录音上传任务");
    }
}
