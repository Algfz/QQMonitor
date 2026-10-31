package com.qqmonitor.uploader;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/** 首屏：录音清单 + 后台运行状态 + 筛选；可逐条上传/重试。 */
public class RecordingsActivity extends Activity {
    private static final int REQ_PERM = 51;
    private static final int FILTER_ALL = 0;
    private static final int FILTER_UPLOADED = 1;
    private static final int FILTER_VALID = 2;
    private static final int FILTER_INVALID = 3;

    private Prefs prefs;
    private RecordingsDb db;
    private LinearLayout statusBox;
    private LinearLayout listBox;
    private TextView summary;
    private TextView syncHint;
    private final List<TextView> chips = new ArrayList<>();
    private int filter = FILTER_VALID;
    private boolean busy;
    private BroadcastReceiver uploadReceiver;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = new Prefs(this);
        db = new RecordingsDb(this);
        if (!prefs.welcomeDone()) {
            startActivity(new Intent(this, MainActivity.class)
                    .putExtra("welcome", true)
                    .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP));
            finish();
            return;
        }
        styleWindow();
        for (String c : new String[]{"线性代数", "大学物理", "程序设计", "体育"}) {
            prefs.ensureCourse(c);
        }
        setContentView(buildPage());
        render(db.list());
        if (!audioGranted()) {
            requestAudio();
        } else {
            refreshAll();
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (uploadReceiver != null) return;
        uploadReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (!busy) syncServer();
            }
        };
        IntentFilter filter = new IntentFilter(UploadService.ACTION_UPLOADED);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(uploadReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(uploadReceiver, filter);
        }
    }

    @Override
    protected void onStop() {
        if (uploadReceiver != null) {
            unregisterReceiver(uploadReceiver);
            uploadReceiver = null;
        }
        super.onStop();
    }

    private void styleWindow() {
        Window w = getWindow();
        w.setStatusBarColor(Ui.BG);
        w.setNavigationBarColor(Ui.BG);
        if (Build.VERSION.SDK_INT >= 26) {
            w.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
                    | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (statusBox != null) {
            render(db.list());
        }
    }

    private View buildPage() {
        FrameLayout root = new FrameLayout(this);
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Ui.BG);
        scroll.setFillViewport(true);
        LinearLayout col = Ui.column(this);
        col.setPadding(Ui.dp(this, 18), Ui.dp(this, 10), Ui.dp(this, 18), Ui.dp(this, 200));
        scroll.addView(col, Ui.matchWrap());
        root.addView(scroll, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout head = Ui.row(this);
        LinearLayout titles = Ui.column(this);
        titles.addView(Ui.title(this, "录音", 22));
        titles.addView(Ui.text(this, "QQMonitor · 手机端上传", 11.5f, Ui.MUTED, false));
        head.addView(titles, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView refresh = Ui.text(this, "刷新", 13, Ui.PRIMARY, true);
        refresh.setPadding(Ui.dp(this, 8), Ui.dp(this, 6), 0, Ui.dp(this, 6));
        refresh.setOnClickListener(v -> {
            if (busy) return;
            refreshAll();
        });
        head.addView(refresh);
        col.addView(head);
        col.addView(Ui.spacer(this, 12));

        statusBox = Ui.column(this);
        col.addView(statusBox);

        HorizontalScrollView filterScroll = new HorizontalScrollView(this);
        filterScroll.setHorizontalScrollBarEnabled(false);
        LinearLayout filterRow = Ui.row(this);
        addChip(filterRow, "全部", FILTER_ALL);
        addChip(filterRow, "已上传", FILTER_UPLOADED);
        addChip(filterRow, "合规未上传", FILTER_VALID);
        addChip(filterRow, "不合规", FILTER_INVALID);
        filterScroll.addView(filterRow);
        LinearLayout.LayoutParams flp = Ui.matchWrap();
        flp.topMargin = Ui.dp(this, 12);
        col.addView(filterScroll, flp);

        summary = Ui.text(this, "正在读取…", 12, Ui.MUTED, false);
        LinearLayout.LayoutParams slp = Ui.matchWrap();
        slp.topMargin = Ui.dp(this, 10);
        col.addView(summary, slp);
        syncHint = Ui.text(this, "", 11, 0xFF8A938C, false);
        LinearLayout.LayoutParams hlp = Ui.matchWrap();
        hlp.topMargin = Ui.dp(this, 2);
        col.addView(syncHint, hlp);
        col.addView(Ui.spacer(this, 10));

        listBox = Ui.column(this);
        col.addView(listBox);

        LinearLayout nav = Ui.bottomNav(this, "records");
        FrameLayout.LayoutParams nlp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM);
        root.addView(nav, nlp);
        Ui.padSystemBars(root, scroll, nav);
        return root;
    }

    private void addChip(LinearLayout row, String label, final int f) {
        TextView chip = Ui.chip(this, label, f == filter);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.rightMargin = Ui.dp(this, 8);
        chip.setOnClickListener(v -> {
            filter = f;
            render(db.list());
        });
        chips.add(chip);
        row.addView(chip, lp);
    }

    private void styleChips() {
        String[] labels = {"全部", "已上传", "合规未上传", "不合规"};
        int[] values = {FILTER_ALL, FILTER_UPLOADED, FILTER_VALID, FILTER_INVALID};
        for (int i = 0; i < chips.size(); i++) {
            TextView c = chips.get(i);
            boolean active = values[i] == filter;
            c.setTextColor(active ? Color.WHITE : Ui.TEXT);
            c.setBackground(active ? Ui.shape(Ui.PRIMARY, 999, 0, this)
                    : Ui.shape(Ui.CARD, 999, Ui.BORDER, this));
        }
    }

    private void refreshStatusCard(List<RecordingsDb.Row> rows) {
        statusBox.removeAllViews();
        LinearLayout card = Ui.card(this);
        LinearLayout row = Ui.row(this);
        View dot = new View(this);
        GradientDrawable g = new GradientDrawable();
        g.setShape(GradientDrawable.OVAL);
        boolean enabled = prefs.serviceEnabled();
        g.setColor(enabled ? 0xFF2E7D5B : 0xFF9AA39C);
        dot.setBackground(g);
        row.addView(dot, new LinearLayout.LayoutParams(Ui.dp(this, 9), Ui.dp(this, 9)));

        LinearLayout texts = Ui.column(this);
        texts.addView(Ui.text(this, enabled ? "后台扫描运行中" : "后台扫描已停止",
                15.5f, Ui.TEXT, true));
        String sub = "每 " + prefs.intervalMinutes() + " 分钟检查 · 上次扫描 "
                + lastScanText();
        texts.addView(Ui.text(this, sub, 11.5f, Ui.MUTED, false));
        LinearLayout.LayoutParams tlp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        tlp.leftMargin = Ui.dp(this, 9);
        row.addView(texts, tlp);
        card.addView(row);

        LinearLayout counts = Ui.row(this);
        int up = 0, okPending = 0, badPending = 0;
        for (RecordingsDb.Row r : rows) {
            if (r.uploadState == 2) up++;
            else if (!r.missing) {
                if (r.valid) okPending++;
                else badPending++;
            }
        }
        counts.addView(miniStat("已上传", up), Ui.weight());
        counts.addView(miniStat("合规待传", okPending), Ui.weight());
        counts.addView(miniStat("不合规", badPending), Ui.weight());
        LinearLayout.LayoutParams clp = Ui.matchWrap();
        clp.topMargin = Ui.dp(this, 10);
        card.addView(counts, clp);

        LinearLayout actions = Ui.row(this);
        TextView scan = Ui.button(this, "立即扫描并上传", true);
        scan.setOnClickListener(v -> {
            if (prefs.token().isEmpty()) {
                Toast.makeText(this, "先去设置里填写 token", Toast.LENGTH_SHORT).show();
                return;
            }
            UploadService.schedule(this);
            UploadService.start(this);
            syncHint.setText("正在检查录音…");
            Toast.makeText(this, "已开始扫描", Toast.LENGTH_SHORT).show();
            scan.postDelayed(this::refreshAll, 2500);
        });
        TextView toggle = Ui.button(this, enabled ? "停止服务" : "开启服务", false);
        toggle.setOnClickListener(v -> {
            if (enabled) {
                UploadService.cancel(this);
                stopService(new Intent(this, UploadService.class));
                Toast.makeText(this, "已停止后台扫描", Toast.LENGTH_SHORT).show();
            } else {
                UploadService.schedule(this);
                UploadService.start(this);
                Toast.makeText(this, "已开启后台扫描", Toast.LENGTH_SHORT).show();
            }
            render(db.list());
        });
        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        slp.rightMargin = Ui.dp(this, 8);
        actions.addView(scan, slp);
        actions.addView(toggle, new LinearLayout.LayoutParams(
                Ui.dp(this, 104), ViewGroup.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams alp = Ui.matchWrap();
        alp.topMargin = Ui.dp(this, 12);
        card.addView(actions, alp);

        if (!batteryOk()) {
            TextView warn = Ui.text(this, "⚠ 电池优化未设置，后台可能被系统限制 · 点此设置",
                    11.5f, Ui.ACCENT, true);
            LinearLayout.LayoutParams wlp = Ui.matchWrap();
            wlp.topMargin = Ui.dp(this, 10);
            warn.setOnClickListener(v -> openBatterySettings());
            card.addView(warn, wlp);
        }
        statusBox.addView(card);
    }

    private View miniStat(String label, int n) {
        LinearLayout v = Ui.column(this);
        v.addView(Ui.text(this, label, 11, Ui.MUTED, false));
        v.addView(Ui.text(this, String.valueOf(n), 18, Ui.TEXT, true));
        return v;
    }

    private void refreshAll() {
        if (busy) return;
        busy = true;
        syncHint.setText("正在扫描手机录音…");
        new Thread(() -> {
            try {
                List<Scanner.Item> items = Scanner.allAudio(this);
                db.applyScan(items, prefs);
                // 扫描后不立刻按旧的本地上传状态渲染，等电脑端对账结束再一次性刷新，
                // 避免“合规待传”先显示 0、半秒后又被服务端纠正成 4 的闪跳。
                syncServer();
            } catch (Exception e) {
                runOnUiThread(() -> {
                    syncHint.setText("扫描失败：" + e.getMessage());
                    busy = false;
                });
            }
        }).start();
    }

    private void syncServer() {
        runOnUiThread(() -> syncHint.setText("正在同步电脑状态…"));
        new Thread(() -> {
            String err = "";
            try {
                List<RecordingsDb.ServerRow> rows = Api.fetchRecordings(prefs, 200);
                db.applyServer(rows);
            } catch (Exception e) {
                err = e.getMessage();
            }
            final String error = err;
            runOnUiThread(() -> {
                busy = false;
                render(db.list());
                if (!error.isEmpty()) {
                    syncHint.setText("电脑同步失败：" + error + "（仍可上传）");
                } else {
                    syncHint.setText("电脑同步于 "
                            + new SimpleDateFormat("HH:mm", Locale.CHINA)
                            .format(new Date()));
                }
            });
        }).start();
    }

    private void render(List<RecordingsDb.Row> rows) {
        if (statusBox == null) return;
        refreshStatusCard(rows);
        styleChips();

        int up = 0, ok = 0, bad = 0;
        for (RecordingsDb.Row r : rows) {
            if (r.uploadState == 2) up++;
            else if (!r.missing) {
                if (r.valid) ok++;
                else bad++;
            }
        }
        summary.setText("共 " + rows.size() + " 条 · 已上传 " + up
                + " · 合规待传 " + ok + " · 不合规 " + bad);

        listBox.removeAllViews();
        List<RecordingsDb.Row> shown = new ArrayList<>();
        for (RecordingsDb.Row r : rows) {
            if (filter == FILTER_UPLOADED && r.uploadState != 2) continue;
            if (filter == FILTER_VALID
                    && (r.uploadState == 2 || !r.valid || r.missing)) continue;
            if (filter == FILTER_INVALID
                    && (r.uploadState == 2 || r.valid || r.missing)) continue;
            shown.add(r);
        }
        if (shown.isEmpty()) {
            TextView empty = Ui.text(this, "这个筛选下没有录音", 13, Ui.MUTED, false);
            empty.setGravity(Gravity.CENTER);
            empty.setPadding(0, Ui.dp(this, 34), 0, 0);
            listBox.addView(empty);
            return;
        }
        int shownCount = 0;
        for (RecordingsDb.Row r : shown) {
            if (shownCount >= 200) {
                TextView more = Ui.text(this, "只显示前 200 条，可用上方筛选缩小范围",
                        11.5f, Ui.MUTED, false);
                more.setGravity(Gravity.CENTER);
                more.setPadding(0, Ui.dp(this, 14), 0, 0);
                listBox.addView(more);
                break;
            }
            listBox.addView(rowCard(r));
            listBox.addView(Ui.spacer(this, 8));
            shownCount++;
        }
    }

    private View rowCard(RecordingsDb.Row r) {
        LinearLayout card = Ui.card(this);
        card.setPadding(Ui.dp(this, 14), Ui.dp(this, 10), Ui.dp(this, 14), Ui.dp(this, 10));

        TextView name = Ui.text(this, r.name, 14, Ui.TEXT, true);
        name.setMaxLines(1);
        name.setEllipsize(TextUtils.TruncateAt.END);
        card.addView(name);

        TextView meta = Ui.text(this, metaText(r) + (r.uploadState == 2 ? " · 已上传" : ""),
                11.5f, Ui.MUTED, false);
        LinearLayout.LayoutParams mlp = Ui.matchWrap();
        mlp.topMargin = Ui.dp(this, 2);
        card.addView(meta, mlp);

        LinearLayout statusRow = Ui.row(this);
        statusRow.setPadding(0, Ui.dp(this, 4), 0, 0);
        statusRow.setGravity(Gravity.CENTER_VERTICAL);
        TextView valid = Ui.text(this, r.valid ? "✓ 文件名合规" : "⚠ " + r.reason,
                11.5f, r.valid ? 0xFF2E7D5B : Ui.ACCENT, true);
        statusRow.addView(valid, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));

        boolean failed = "archived".equals(r.serverStatus) || "error".equals(r.serverStatus);
        if (r.canUpload()) {
            TextView btn = Ui.smallButton(this, "上传", true);
            btn.setOnClickListener(v -> upload(r, btn));
            statusRow.addView(btn);
        } else if (failed && r.serverId > 0) {
            TextView retry = Ui.smallButton(this, "重新转写", false);
            retry.setOnClickListener(v -> retry(r, retry));
            statusRow.addView(retry);
        }
        card.addView(statusRow);

        String server = serverText(r);
        if (!server.isEmpty()) {
            TextView st = Ui.text(this, server, 11f, 0xFF5B635E, false);
            LinearLayout.LayoutParams lp = Ui.matchWrap();
            lp.topMargin = Ui.dp(this, 2);
            card.addView(st, lp);
        }
        if (r.missing) {
            TextView gone = Ui.text(this, "手机里已找不到这个文件", 11f, Ui.ACCENT, false);
            LinearLayout.LayoutParams lp = Ui.matchWrap();
            lp.topMargin = Ui.dp(this, 2);
            card.addView(gone, lp);
        }
        return card;
    }

    private String metaText(RecordingsDb.Row r) {
        StringBuilder sb = new StringBuilder();
        if (r.size > 0) sb.append(byteText(r.size)).append(" · ");
        String d = !r.date.isEmpty() ? r.date
                : new SimpleDateFormat("yyyy-MM-dd", Locale.CHINA)
                .format(new Date(r.modified * 1000L));
        sb.append(d);
        if (r.duration > 0) {
            long sec = r.duration / 1000L;
            sb.append(" · ").append(sec / 60).append(" 分 ").append(sec % 60).append(" 秒");
        }
        return sb.toString();
    }

    private String byteText(long size) {
        if (size >= 1024L * 1024) {
            return String.format(Locale.CHINA, "%.1f MB", size / 1024.0 / 1024.0);
        }
        return String.format(Locale.CHINA, "%d KB", Math.max(1, size / 1024));
    }

    private String serverText(RecordingsDb.Row r) {
        if (r.serverStatus.isEmpty()) return "";
        switch (r.serverStatus) {
            case "pending":
                return "电脑：排队转写";
            case "transcribing":
                return "电脑：转写中…";
            case "transcribed":
                return "电脑：已转写并入库" + (r.mdPath.isEmpty() ? "" : "（有文字稿）");
            case "archived":
                return "电脑：仅归档（未生成文字稿）";
            case "error":
                return "电脑：失败 — " + r.serverDetail;
            case "missing":
                return "电脑：无记录，可重新上传";
            default:
                return "电脑：" + r.serverStatus;
        }
    }

    private void upload(RecordingsDb.Row r, TextView btn) {
        if (prefs.token().isEmpty()) {
            Toast.makeText(this, "先去设置里填写 token", Toast.LENGTH_SHORT).show();
            return;
        }
        btn.setEnabled(false);
        btn.setText("上传中…");
        new Thread(() -> {
            String md5 = r.md5;
            try {
                if (md5 == null || md5.isEmpty()) {
                    md5 = Md5.of(this, Uri.parse(r.uri));
                    db.setMd5(r.mediaId, md5);
                }
                Scanner.Item item = new Scanner.Item(r.mediaId, r.name, r.size,
                        r.modified, r.duration, Uri.parse(r.uri), r.course,
                        r.date, r.valid, r.reason);
                String base = Api.resolveBase(prefs);
                Uploader.Result res = Uploader.upload(this, item, prefs, base, md5);
                final String msg = res.message;
                if (res.ok) {
                    db.markUploaded(r.mediaId, md5);
                    prefs.markUploaded(item.key());
                    prefs.incUploaded();
                    prefs.setLastUpload(new SimpleDateFormat("HH:mm", Locale.CHINA)
                            .format(new Date()) + "  " + r.name);
                }
                runOnUiThread(() -> {
                    Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
                    btn.setEnabled(true);
                    btn.setText("上传到电脑");
                    if (res.ok) syncServer();
                    else render(db.list());
                });
            } catch (Exception e) {
                runOnUiThread(() -> {
                    Toast.makeText(this, "上传失败：" + e.getMessage(),
                            Toast.LENGTH_LONG).show();
                    btn.setEnabled(true);
                    btn.setText("上传到电脑");
                });
            }
        }).start();
    }

    private void retry(RecordingsDb.Row r, TextView btn) {
        btn.setEnabled(false);
        btn.setText("正在重新排队…");
        new Thread(() -> {
            try {
                Api.retry(prefs, r.serverId);
                runOnUiThread(() -> {
                    Toast.makeText(this, "已重新排队转写", Toast.LENGTH_SHORT).show();
                    syncServer();
                });
            } catch (Exception e) {
                runOnUiThread(() -> {
                    Toast.makeText(this, "重试失败：" + e.getMessage(),
                            Toast.LENGTH_LONG).show();
                    btn.setEnabled(true);
                    btn.setText("重新转写");
                });
            }
        }).start();
    }

    private boolean batteryOk() {
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        return pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
    }

    private void openBatterySettings() {
        if (batteryOk()) {
            Toast.makeText(this, "已允许后台运行，无需再设置", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            i.setData(Uri.parse("package:" + getPackageName()));
            startActivity(i);
            Toast.makeText(this, "请在系统弹窗里选择「允许」", Toast.LENGTH_LONG).show();
        } catch (Exception e) {
            try {
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
                Toast.makeText(this, "无法打开电池设置：" + e.getMessage(),
                        Toast.LENGTH_LONG).show();
            }
        }
    }

    private String lastScanText() {
        long t = prefs.lastScanAt();
        if (t <= 0) return "未扫描";
        return new SimpleDateFormat("HH:mm", Locale.CHINA).format(new Date(t));
    }

    private boolean audioGranted() {
        String perm = Build.VERSION.SDK_INT >= 33
                ? Manifest.permission.READ_MEDIA_AUDIO
                : Manifest.permission.READ_EXTERNAL_STORAGE;
        return checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED;
    }

    private void requestAudio() {
        String perm = Build.VERSION.SDK_INT >= 33
                ? Manifest.permission.READ_MEDIA_AUDIO
                : Manifest.permission.READ_EXTERNAL_STORAGE;
        requestPermissions(new String[]{perm}, REQ_PERM);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_PERM) {
            if (audioGranted()) refreshAll();
            else syncHint.setText("没有读取录音权限，无法列出文件");
        }
    }
}
