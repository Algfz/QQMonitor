package com.qqmonitor.uploader;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/** 主界面 + 首次启动引导（4 步）。 */
public class MainActivity extends Activity {
    private static final int REQ_PERM = 41;

    private Prefs prefs;
    private int step = 0;

    private EditText serverInput;
    private EditText tokenInput;
    private EditText coursesInput;
    private EditText intervalInput;

    private TextView statPending;
    private TextView statusText;
    private TextView statusSub;
    private TextView logView;
    private TextView permAudio;
    private TextView permNotify;
    private TextView permBattery;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = new Prefs(this);
        styleWindow();
        boolean welcome = getIntent() != null && getIntent().getBooleanExtra("welcome", false);
        if (!prefs.welcomeDone() || welcome) {
            showWelcome(0);
        } else {
            showMain();
        }
    }

    private void styleWindow() {
        Window w = getWindow();
        w.setStatusBarColor(Ui.BG);
        w.setNavigationBarColor(Ui.BG);
        if (Build.VERSION.SDK_INT >= 26) {
            View decor = w.getDecorView();
            decor.setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
                    | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (prefs.welcomeDone() && logView != null) {
            refreshLog();
            refreshPermLabels();
        } else if (!prefs.welcomeDone() && step == 3) {
            showWelcome(3);
        }
    }

    // ================= 引导 =================

    private void showWelcome(int index) {
        step = index;
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Ui.BG);
        scroll.setFillViewport(true);
        LinearLayout col = Ui.column(this);
        col.setPadding(Ui.dp(this, 22), Ui.dp(this, 18), Ui.dp(this, 22), Ui.dp(this, 28));
        scroll.addView(col, Ui.matchWrap());

        col.addView(dots(index));
        col.addView(Ui.spacer(this, 18));

        switch (index) {
            case 0:
                buildWelcomeIntro(col);
                break;
            case 1:
                buildWelcomeServer(col);
                break;
            case 2:
                buildWelcomeRules(col);
                break;
            default:
                buildWelcomePermissions(col);
                break;
        }
        FrameLayout root = new FrameLayout(this);
        root.addView(scroll, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
        Ui.padSystemBars(root, scroll, null);
    }

    private View dots(int active) {
        LinearLayout row = Ui.row(this);
        row.setGravity(Gravity.CENTER);
        for (int i = 0; i < 4; i++) {
            View d = new View(this);
            GradientDrawable g = new GradientDrawable();
            g.setShape(GradientDrawable.OVAL);
            g.setColor(i == active ? Ui.PRIMARY : Ui.BORDER);
            d.setBackground(g);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    Ui.dp(this, i == active ? 22 : 8), Ui.dp(this, 8));
            lp.rightMargin = Ui.dp(this, 6);
            row.addView(d, lp);
        }
        return row;
    }

    private void buildWelcomeIntro(LinearLayout col) {
        ImageView icon = new ImageView(this);
        icon.setImageResource(R.mipmap.ic_launcher);
        LinearLayout.LayoutParams ilp = new LinearLayout.LayoutParams(
                Ui.dp(this, 88), Ui.dp(this, 88));
        ilp.gravity = Gravity.CENTER_HORIZONTAL;
        col.addView(icon, ilp);

        TextView title = Ui.title(this, "录音，自动进知识库", 24);
        title.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams tlp = Ui.matchWrap();
        tlp.topMargin = Ui.dp(this, 20);
        col.addView(title, tlp);

        TextView sub = Ui.text(this,
                "把手机里的录音按分类与日期整理好，\n经内网（局域网或 Tailscale）上传到电脑转成文字。", 14, Ui.MUTED, false);
        sub.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams slp = Ui.matchWrap();
        slp.topMargin = Ui.dp(this, 10);
        slp.bottomMargin = Ui.dp(this, 24);
        col.addView(sub, slp);

        String[][] features = {
                {"自动扫描", "每 15 分钟检查一次手机录音"},
                {"只传合规的", "文件名必须是「分类名+日期」"},
                {"电脑转文字", "上传后本地 ASR 转写并建立索引"},
        };
        for (String[] f : features) {
            LinearLayout card = Ui.card(this);
            TextView name = Ui.text(this, f[0], 14, Ui.TEXT, true);
            TextView desc = Ui.text(this, f[1], 12, Ui.MUTED, false);
            LinearLayout.LayoutParams dlp = Ui.matchWrap();
            dlp.topMargin = Ui.dp(this, 3);
            card.addView(name);
            card.addView(desc, dlp);
            LinearLayout.LayoutParams clp = Ui.matchWrap();
            clp.bottomMargin = Ui.dp(this, 10);
            col.addView(card, clp);
        }

        TextView go = Ui.button(this, "开始配置", true);
        go.setOnClickListener(v -> showWelcome(1));
        LinearLayout.LayoutParams glp = Ui.matchWrap();
        glp.topMargin = Ui.dp(this, 18);
        col.addView(go, glp);

        TextView skip = Ui.text(this, "跳过引导，直接进入", 12, Ui.MUTED, false);
        skip.setGravity(Gravity.CENTER);
        skip.setPadding(0, Ui.dp(this, 14), 0, 0);
        skip.setOnClickListener(v -> finishWelcome());
        col.addView(skip);
    }

    private void buildWelcomeServer(LinearLayout col) {
        col.addView(Ui.section(this, "第 1 步 / 连接电脑"));
        TextView title = Ui.title(this, "填写电脑地址和访问 token", 20);
        LinearLayout.LayoutParams tlp = Ui.matchWrap();
        tlp.topMargin = Ui.dp(this, 6);
        col.addView(title, tlp);

        serverInput = Ui.field(this, "服务器地址",
                prefs.server(), false, false);
        LinearLayout serverBlock = Ui.labelBlock(this, "服务器地址", serverInput,
                "可填多个，逗号分隔；例如 Tailscale 地址, http://127.0.0.1:8088（USB 调试）");
        LinearLayout.LayoutParams blp = Ui.matchWrap();
        blp.topMargin = Ui.dp(this, 18);
        col.addView(serverBlock, blp);

        tokenInput = Ui.field(this, "api.token", prefs.token(), false, true);
        LinearLayout tokenBlock = Ui.labelBlock(this, "访问 token", tokenInput,
                "电脑 config.yaml → api.token");
        LinearLayout.LayoutParams tklp = Ui.matchWrap();
        tklp.topMargin = Ui.dp(this, 14);
        col.addView(tokenBlock, tklp);

        TextView show = Ui.text(this, "显示 token", 12, Ui.PRIMARY, true);
        show.setPadding(0, Ui.dp(this, 8), 0, 0);
        show.setOnClickListener(v -> {
            boolean hidden = tokenInput.getInputType() == (InputType.TYPE_CLASS_TEXT
                    | InputType.TYPE_TEXT_VARIATION_PASSWORD);
            tokenInput.setInputType(hidden ? InputType.TYPE_CLASS_TEXT
                    : InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
            show.setText(hidden ? "隐藏 token" : "显示 token");
        });
        col.addView(show);

        addNav(col, false, 1);
    }

    private void buildWelcomeRules(LinearLayout col) {
        col.addView(Ui.section(this, "第 2 步 / 上传规则"));
        TextView title = Ui.title(this, "哪些录音需要上传", 20);
        LinearLayout.LayoutParams tlp = Ui.matchWrap();
        tlp.topMargin = Ui.dp(this, 6);
        col.addView(title, tlp);

        coursesInput = Ui.field(this, "分类名（课程/会议），用逗号分隔", prefs.courses(), true, false);
        colAdd(col, Ui.labelBlock(this, "分类白名单", coursesInput,
                "名字含「会议」的录音也会上传"), 14);

        intervalInput = Ui.field(this, "分钟", String.valueOf(prefs.intervalMinutes()), false, false);
        intervalInput.setInputType(InputType.TYPE_CLASS_NUMBER);
        col.addView(Ui.labelBlock(this, "检查间隔（分钟，最少 5）", intervalInput, null));

        addNav(col, true, 2);
    }

    private void buildWelcomePermissions(LinearLayout col) {
        col.addView(Ui.section(this, "第 3 步 / 权限"));
        TextView title = Ui.title(this, "最后，允许它在后台工作", 20);
        LinearLayout.LayoutParams tlp = Ui.matchWrap();
        tlp.topMargin = Ui.dp(this, 6);
        tlp.bottomMargin = Ui.dp(this, 16);
        col.addView(title, tlp);

        LinearLayout audio = permissionRow("读取录音权限",
                "读取手机里的录音文件", permStateAudio(), "授权", v -> requestAudio());
        permAudio = (TextView) audio.getTag();
        col.addView(audio);

        LinearLayout notify = permissionRow("通知权限",
                "上传进度与结果显示在通知栏", permStateNotify(), "授权", v -> requestNotify());
        permNotify = (TextView) notify.getTag();
        col.addView(notify);

        LinearLayout battery = permissionRow("电池优化",
                "设为不受限制，避免后台被系统杀掉", permStateBattery(), "去设置", v -> batterySettings());
        permBattery = (TextView) battery.getTag();
        col.addView(battery);

        TextView done = Ui.button(this, "完成并启动", true);
        done.setOnClickListener(v -> finishWelcome());
        LinearLayout.LayoutParams dlp = Ui.matchWrap();
        dlp.topMargin = Ui.dp(this, 22);
        col.addView(done, dlp);

        TextView back = Ui.text(this, "上一步", 13, Ui.MUTED, false);
        back.setGravity(Gravity.CENTER);
        back.setPadding(0, Ui.dp(this, 14), 0, 0);
        back.setOnClickListener(v -> showWelcome(2));
        col.addView(back);
    }

    private LinearLayout permissionRow(String title, String desc, String state,
                                       String action, View.OnClickListener click) {
        LinearLayout card = Ui.card(this);
        LinearLayout top = Ui.row(this);
        LinearLayout texts = Ui.column(this);
        texts.addView(Ui.text(this, title, 14, Ui.TEXT, true));
        TextView d = Ui.text(this, desc, 12, Ui.MUTED, false);
        LinearLayout.LayoutParams dlp = Ui.matchWrap();
        dlp.topMargin = Ui.dp(this, 3);
        texts.addView(d, dlp);
        top.addView(texts, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView st = Ui.text(this, state, 12, state.startsWith("已") ? 0xFF2E7D5B : Ui.ACCENT, true);
        top.addView(st);
        card.addView(top);

        TextView btn = Ui.button(this, action, false);
        LinearLayout.LayoutParams blp = Ui.matchWrap();
        blp.topMargin = Ui.dp(this, 10);
        btn.setOnClickListener(click);
        card.addView(btn, blp);
        card.setTag(st);
        LinearLayout.LayoutParams clp = Ui.matchWrap();
        clp.bottomMargin = Ui.dp(this, 10);
        card.setLayoutParams(clp);
        return card;
    }

    private void addNav(LinearLayout col, boolean allowBack, int index) {
        LinearLayout row = Ui.row(this);
        LinearLayout.LayoutParams rlp = Ui.matchWrap();
        rlp.topMargin = Ui.dp(this, 22);
        if (allowBack) {
            TextView back = Ui.button(this, "上一步", false);
            back.setOnClickListener(v -> showWelcome(index - 1));
            row.addView(back, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1));
            View gap = new View(this);
            row.addView(gap, new LinearLayout.LayoutParams(Ui.dp(this, 10),
                    ViewGroup.LayoutParams.WRAP_CONTENT));
        }
        TextView next = Ui.button(this, "下一步", true);
        next.setOnClickListener(v -> {
            saveInputs();
            showWelcome(index + 1);
        });
        row.addView(next, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, allowBack ? 1 : 2));
        col.addView(row, rlp);
    }

    private void saveInputs() {
        if (serverInput != null && serverInput.getText().length() > 0) {
            prefs.setServer(serverInput.getText().toString());
        }
        if (tokenInput != null) {
            prefs.setToken(tokenInput.getText().toString());
        }
        if (coursesInput != null && coursesInput.getText().length() > 0) {
            prefs.setCourses(coursesInput.getText().toString());
        }
        if (intervalInput != null) {
            try {
                prefs.setInterval(Integer.parseInt(intervalInput.getText().toString().trim()));
            } catch (Exception ignored) {
            }
        }
        refreshInputs();
    }

    private void refreshInputs() {
        if (serverInput != null) serverInput.setText(prefs.server());
        if (tokenInput != null) tokenInput.setText(prefs.token());
        if (coursesInput != null) coursesInput.setText(prefs.courses());
        if (intervalInput != null) intervalInput.setText(String.valueOf(prefs.intervalMinutes()));
    }

    private void finishWelcome() {
        saveInputs();
        prefs.setWelcomeDone(true);
        UploadService.schedule(this);
        UploadService.start(this);
        Toast.makeText(this, "已启用后台扫描", Toast.LENGTH_SHORT).show();
        startActivity(new Intent(this, RecordingsActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP));
        finish();
    }

    // ================= 主界面 =================

    private void showMain() {
        FrameLayout root = new FrameLayout(this);
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Ui.BG);
        scroll.setFillViewport(true);
        LinearLayout col = Ui.column(this);
        col.setPadding(Ui.dp(this, 18), Ui.dp(this, 12), Ui.dp(this, 18), Ui.dp(this, 170));
        scroll.addView(col, Ui.matchWrap());
        root.addView(scroll, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        col.addView(header());
        col.addView(Ui.spacer(this, 14));
        col.addView(serviceCard());
        col.addView(Ui.spacer(this, 10));
        col.addView(permissionsCard());
        col.addView(Ui.spacer(this, 10));
        col.addView(connectionCard());
        col.addView(Ui.spacer(this, 10));
        col.addView(rulesCard());
        col.addView(Ui.spacer(this, 10));
        col.addView(logCard());
        col.addView(Ui.spacer(this, 14));

        TextView save = Ui.button(this, "保存设置", true);
        save.setOnClickListener(v -> {
            saveInputs();
            UploadService.schedule(this);
            if (prefs.serviceEnabled()) {
                UploadService.start(this);
            }
            Toast.makeText(this, "设置已保存，已重新排程", Toast.LENGTH_SHORT).show();
        });
        col.addView(save, Ui.matchWrap());

        TextView replay = Ui.text(this, "重看新手引导", 12, Ui.MUTED, false);
        replay.setGravity(Gravity.CENTER);
        replay.setPadding(0, Ui.dp(this, 14), 0, 0);
        replay.setOnClickListener(v -> {
            prefs.setWelcomeDone(false);
            showWelcome(0);
        });
        col.addView(replay);

        TextView version = Ui.text(this, "QQMonitor 录音上传 · v2.5", 11, 0xFF8A938C, false);
        version.setGravity(Gravity.CENTER);
        version.setPadding(0, Ui.dp(this, 10), 0, 0);
        col.addView(version);

        LinearLayout nav = Ui.bottomNav(this, "settings");
        FrameLayout.LayoutParams nlp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM);
        root.addView(nav, nlp);

        setContentView(root);
        Ui.padSystemBars(root, scroll, nav);
        refreshLog();
        refreshPermLabels();
    }

    private View serviceCard() {
        LinearLayout card = Ui.card(this);
        LinearLayout row = Ui.row(this);
        View dot = new View(this);
        GradientDrawable g = new GradientDrawable();
        g.setShape(GradientDrawable.OVAL);
        g.setColor(prefs.serviceEnabled() ? 0xFF2E7D5B : 0xFF9AA39C);
        dot.setBackground(g);
        row.addView(dot, new LinearLayout.LayoutParams(Ui.dp(this, 9), Ui.dp(this, 9)));
        LinearLayout texts = Ui.column(this);
        TextView title = Ui.text(this, prefs.serviceEnabled() ? "后台扫描运行中" : "后台扫描已停止",
                14.5f, Ui.TEXT, true);
        texts.addView(title);
        texts.addView(Ui.text(this, "每 " + prefs.intervalMinutes() + " 分钟检查一次手机录音",
                11.5f, Ui.MUTED, false));
        LinearLayout.LayoutParams tlp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        tlp.leftMargin = Ui.dp(this, 9);
        row.addView(texts, tlp);
        TextView toggle = Ui.button(this, prefs.serviceEnabled() ? "停止" : "开启", false);
        toggle.setOnClickListener(v -> {
            if (prefs.serviceEnabled()) {
                UploadService.cancel(this);
                stopService(new Intent(this, UploadService.class));
                Toast.makeText(this, "已停止后台扫描", Toast.LENGTH_SHORT).show();
            } else {
                UploadService.schedule(this);
                UploadService.start(this);
                Toast.makeText(this, "已开启后台扫描", Toast.LENGTH_SHORT).show();
            }
            showMain();
        });
        row.addView(toggle, new LinearLayout.LayoutParams(
                Ui.dp(this, 72), ViewGroup.LayoutParams.WRAP_CONTENT));
        card.addView(row);
        return card;
    }

    private View permissionsCard() {
        LinearLayout card = Ui.card(this);
        card.addView(Ui.section(this, "权限"));
        permAudio = permissionRow(card, "读取手机录音", this::requestAudio);
        permNotify = permissionRow(card, "通知（上传进度）", this::requestNotify);
        permBattery = permissionRow(card, "电池优化", this::batterySettings);
        refreshPermLabels();
        return card;
    }

    private TextView permissionRow(LinearLayout card, String label, Runnable action) {
        LinearLayout row = Ui.row(this);
        row.setPadding(0, Ui.dp(this, 9), 0, 0);
        TextView name = Ui.text(this, label, 13, Ui.TEXT, false);
        row.addView(name, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView state = Ui.text(this, "", 11.5f, Ui.MUTED, false);
        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        slp.rightMargin = Ui.dp(this, 10);
        row.addView(state, slp);
        TextView go = Ui.text(this, "设置", 12, Ui.PRIMARY, true);
        go.setOnClickListener(v -> action.run());
        row.addView(go);
        card.addView(row);
        return state;
    }

    private void refreshPermLabels() {
        if (permAudio != null) {
            permAudio.setText(permStateAudio());
            permAudio.setTextColor(audioGranted() ? 0xFF2E7D5B : Ui.ACCENT);
        }
        if (permNotify != null) {
            permNotify.setText(permStateNotify());
            permNotify.setTextColor(notifyGranted() ? 0xFF2E7D5B : Ui.ACCENT);
        }
        if (permBattery != null) {
            permBattery.setText(permStateBattery());
            permBattery.setTextColor(batteryOk() ? 0xFF2E7D5B : Ui.ACCENT);
        }
    }

    private View header() {
        LinearLayout row = Ui.row(this);
        ImageView icon = new ImageView(this);
        icon.setImageResource(R.mipmap.ic_launcher);
        row.addView(icon, new LinearLayout.LayoutParams(Ui.dp(this, 40), Ui.dp(this, 40)));
        LinearLayout texts = Ui.column(this);
        texts.addView(Ui.title(this, "设置", 19));
        texts.addView(Ui.text(this, "连接、权限与上传规则", 11.5f, Ui.MUTED, false));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.leftMargin = Ui.dp(this, 12);
        row.addView(texts, lp);
        return row;
    }

    private View statusCard() {
        LinearLayout card = Ui.card(this);
        LinearLayout row = Ui.row(this);
        View dot = new View(this);
        GradientDrawable g = new GradientDrawable();
        g.setShape(GradientDrawable.OVAL);
        g.setColor(prefs.token().isEmpty() ? Ui.ACCENT : 0xFF2E7D5B);
        dot.setBackground(g);
        row.addView(dot, new LinearLayout.LayoutParams(Ui.dp(this, 9), Ui.dp(this, 9)));

        LinearLayout texts = Ui.column(this);
        statusText = Ui.text(this, "", 16, Ui.TEXT, true);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.leftMargin = Ui.dp(this, 9);
        texts.addView(statusText);
        statusSub = Ui.text(this, "", 12, Ui.MUTED, false);
        texts.addView(statusSub);
        row.addView(texts, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        card.addView(row);

        TextView scan = Ui.button(this, "立即扫描并上传", true);
        LinearLayout.LayoutParams slp = Ui.matchWrap();
        slp.topMargin = Ui.dp(this, 14);
        scan.setOnClickListener(v -> {
            if (prefs.token().isEmpty()) {
                Toast.makeText(this, "先在下方填写 token", Toast.LENGTH_SHORT).show();
                return;
            }
            UploadService.start(this);
            statusSub.setText("正在检查录音…");
        });
        card.addView(scan, slp);

        LinearLayout row2 = Ui.row(this);
        TextView stop = Ui.button(this, "停止服务", false);
        stop.setOnClickListener(v -> {
            UploadService.cancel(this);
            stopService(new Intent(this, UploadService.class));
            Toast.makeText(this, "已停止后台扫描", Toast.LENGTH_SHORT).show();
            refreshStatus();
        });
        TextView battery = Ui.button(this, "电池优化", false);
        battery.setOnClickListener(v -> batterySettings());
        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        LinearLayout.LayoutParams blp2 = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        blp.rightMargin = Ui.dp(this, 8);
        row2.addView(stop, blp);
        row2.addView(battery, blp2);
        LinearLayout.LayoutParams r2lp = Ui.matchWrap();
        r2lp.topMargin = Ui.dp(this, 8);
        card.addView(row2, r2lp);
        return card;
    }

    private View recordingsCard() {
        LinearLayout card = Ui.card(this);
        card.addView(Ui.section(this, "录音清单"));
        TextView tip = Ui.text(this,
                "查看手机里全部录音：文件名是否合规、是否上传、电脑是否转写入库。可逐条手动上传。",
                12, Ui.MUTED, false);
        LinearLayout.LayoutParams tlp = Ui.matchWrap();
        tlp.topMargin = Ui.dp(this, 4);
        card.addView(tip, tlp);
        TextView open = Ui.button(this, "打开我的录音", false);
        LinearLayout.LayoutParams olp = Ui.matchWrap();
        olp.topMargin = Ui.dp(this, 12);
        open.setOnClickListener(v -> startActivity(new Intent(this, RecordingsActivity.class)));
        card.addView(open, olp);
        return card;
    }

    private View statsCard() {
        LinearLayout card = Ui.card(this);
        LinearLayout row = Ui.row(this);

        LinearLayout a = Ui.column(this);
        a.addView(Ui.text(this, "已上传", 11.5f, Ui.MUTED, false));
        TextView up = Ui.text(this, String.valueOf(prefs.uploadedCount()), 22, Ui.TEXT, true);
        a.addView(up);

        LinearLayout b = Ui.column(this);
        b.addView(Ui.text(this, "待上传", 11.5f, Ui.MUTED, false));
        statPending = Ui.text(this, "…", 22, Ui.TEXT, true);
        b.addView(statPending);

        LinearLayout c = Ui.column(this);
        c.addView(Ui.text(this, "上次扫描", 11.5f, Ui.MUTED, false));
        TextView last = Ui.text(this, lastScanText(), 13, Ui.MUTED, false);
        c.addView(last);

        row.addView(a, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        row.addView(b, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        row.addView(c, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        card.addView(row);
        return card;
    }

    private String lastScanText() {
        long t = prefs.lastScanAt();
        if (t <= 0) return "未扫描";
        return new SimpleDateFormat("HH:mm", Locale.CHINA).format(new Date(t));
    }

    private View connectionCard() {
        LinearLayout card = Ui.card(this);
        card.addView(Ui.section(this, "连接"));
        serverInput = Ui.field(this, "服务器地址", prefs.server(), false, false);
        colAdd(card, Ui.labelBlock(this, "服务器地址", serverInput,
                "可填多个，逗号分隔；USB 调试可加 http://127.0.0.1:8088"), 10);
        tokenInput = Ui.field(this, "api.token", prefs.token(), false, true);
        colAdd(card, Ui.labelBlock(this, "访问 token", tokenInput,
                "电脑 config.yaml → api.token"), 10);

        TextView show = Ui.text(this, "显示 token", 12, Ui.PRIMARY, true);
        show.setOnClickListener(v -> {
            boolean hidden = tokenInput.getInputType() == (InputType.TYPE_CLASS_TEXT
                    | InputType.TYPE_TEXT_VARIATION_PASSWORD);
            tokenInput.setInputType(hidden ? InputType.TYPE_CLASS_TEXT
                    : InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
            show.setText(hidden ? "隐藏 token" : "显示 token");
        });
        card.addView(show);
        return card;
    }

    private View rulesCard() {
        LinearLayout card = Ui.card(this);
        card.addView(Ui.section(this, "上传规则"));
        coursesInput = Ui.field(this, "分类名（课程/会议），用逗号分隔", prefs.courses(), true, false);
        colAdd(card, Ui.labelBlock(this, "分类白名单", coursesInput,
                "名字含「会议」的录音也会上传"), 10);
        intervalInput = Ui.field(this, "分钟", String.valueOf(prefs.intervalMinutes()), false, false);
        intervalInput.setInputType(InputType.TYPE_CLASS_NUMBER);
        colAdd(card, Ui.labelBlock(this, "检查间隔（分钟）", intervalInput, "最少 5 分钟"), 8);
        return card;
    }

    private View logCard() {
        LinearLayout card = Ui.card(this);
        LinearLayout head = Ui.row(this);
        head.addView(Ui.section(this, "日志"), new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView clear = Ui.text(this, "清空", 12, Ui.MUTED, false);
        clear.setOnClickListener(v -> {
            try {
                new java.io.File(getFilesDir(), "upload.log").delete();
            } catch (Exception ignored) {
            }
            refreshLog();
        });
        head.addView(clear);
        card.addView(head);

        logView = Ui.text(this, "暂无日志", 11, Ui.MUTED, false);
        logView.setTypeface(Typeface.MONOSPACE);
        logView.setMaxLines(12);
        logView.setVerticalScrollBarEnabled(true);
        logView.setLineSpacing(Ui.dp(this, 2), 1f);
        LinearLayout.LayoutParams lp = Ui.matchWrap();
        lp.topMargin = Ui.dp(this, 8);
        card.addView(logView, lp);
        return card;
    }

    private void colAdd(LinearLayout parent, View child, int topMarginDp) {
        LinearLayout.LayoutParams lp = Ui.matchWrap();
        lp.topMargin = Ui.dp(this, topMarginDp);
        parent.addView(child, lp);
    }

    // ================= 数据刷新 =================

    private void refreshStatus() {
        if (statusText == null) return;
        boolean hasToken = !prefs.token().isEmpty();
        statusText.setText(hasToken ? "已配置 · 后台待命" : "还差一个 token");
        statusSub.setText(hasToken
                ? "每 " + prefs.intervalMinutes() + " 分钟检查一次"
                : "填写 token 后才能上传");
    }

    private void refreshStats() {
        if (statPending == null) return;
        new Thread(() -> {
            int pending = 0;
            try {
                List<Scanner.Item> items = Scanner.candidates(this);
                for (Scanner.Item it : items) {
                    if (!prefs.isUploaded(it.key())) pending++;
                }
            } catch (Exception ignored) {
            }
            final int p = pending;
            runOnUiThread(() -> statPending.setText(String.valueOf(p)));
        }).start();
    }

    private void refreshLog() {
        if (logView == null) return;
        String all = LogStore.read(this);
        if (all.trim().isEmpty()) {
            logView.setText("暂无日志");
            return;
        }
        String[] lines = all.trim().split("\n");
        int start = Math.max(0, lines.length - 8);
        StringBuilder sb = new StringBuilder();
        for (int i = start; i < lines.length; i++) {
            sb.append(lines[i]).append('\n');
        }
        logView.setText(sb.toString().trim());
    }

    // ================= 权限 =================

    private boolean audioGranted() {
        String perm = Build.VERSION.SDK_INT >= 33
                ? Manifest.permission.READ_MEDIA_AUDIO
                : Manifest.permission.READ_EXTERNAL_STORAGE;
        return checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean notifyGranted() {
        if (Build.VERSION.SDK_INT < 33) return true;
        return checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED;
    }

    private boolean batteryOk() {
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        return pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
    }

    private String permStateAudio() {
        return audioGranted() ? "已授权" : "未授权";
    }

    private String permStateNotify() {
        return notifyGranted() ? "已授权" : "未授权";
    }

    private String permStateBattery() {
        return batteryOk() ? "已设置" : "未设置";
    }

    private void requestAudio() {
        String perm = Build.VERSION.SDK_INT >= 33
                ? Manifest.permission.READ_MEDIA_AUDIO
                : Manifest.permission.READ_EXTERNAL_STORAGE;
        requestPermissions(new String[]{perm}, REQ_PERM);
    }

    private void requestNotify() {
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_PERM);
        }
    }

    private void batterySettings() {
        if (batteryOk()) {
            Toast.makeText(this, "已允许后台运行，无需再设置", Toast.LENGTH_SHORT).show();
            try {
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
            }
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

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_PERM && !prefs.welcomeDone()) {
            showWelcome(3);
        }
    }
}
