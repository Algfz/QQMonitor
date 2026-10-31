package com.qqmonitor.uploader;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.os.Build;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** 与 WebUI v4「调度台」一致的设计令牌与常用控件。 */
public final class Ui {
    public static final int BG = 0xFFF3F5F2;
    public static final int CARD = 0xFFFFFFFF;
    public static final int TEXT = 0xFF171A18;
    public static final int MUTED = 0xFF5B635E;
    public static final int PRIMARY = 0xFF17352B;
    public static final int ACCENT = 0xFFB23A2C;
    public static final int BORDER = 0xFFD7DBD5;
    public static final int SOFT = 0xFFEDF0EC;

    private Ui() {
    }

    public static int dp(Context c, float v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                c.getResources().getDisplayMetrics()));
    }

    public static GradientDrawable shape(int color, float radiusDp, int strokeColor, Context c) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(color);
        d.setCornerRadius(dp(c, radiusDp));
        if (strokeColor != 0) {
            d.setStroke(Math.max(1, dp(c, 1)), strokeColor);
        }
        return d;
    }

    public static RippleDrawable ripple(Context c, int color, float radiusDp, int rippleColor) {
        GradientDrawable bg = shape(color, radiusDp, 0, c);
        return new RippleDrawable(ColorStateList.valueOf(rippleColor), bg, null);
    }

    public static TextView text(Context c, String s, float sizeSp, int color, boolean bold) {
        TextView t = new TextView(c);
        t.setText(s);
        t.setTextSize(sizeSp);
        t.setTextColor(color);
        t.setTypeface(Typeface.create("sans-serif" + (bold ? "-medium" : ""), Typeface.NORMAL));
        t.setLineSpacing(dp(c, 3), 1f);
        return t;
    }

    public static TextView title(Context c, String s, float sizeSp) {
        TextView t = text(c, s, sizeSp, TEXT, true);
        t.setLetterSpacing(-0.01f);
        return t;
    }

    public static TextView section(Context c, String s) {
        TextView t = text(c, s, 11.5f, MUTED, true);
        t.setLetterSpacing(0.12f);
        t.setAllCaps(true);
        return t;
    }

    public static LinearLayout column(Context c) {
        LinearLayout l = new LinearLayout(c);
        l.setOrientation(LinearLayout.VERTICAL);
        return l;
    }

    public static LinearLayout row(Context c) {
        LinearLayout l = new LinearLayout(c);
        l.setOrientation(LinearLayout.HORIZONTAL);
        l.setGravity(Gravity.CENTER_VERTICAL);
        return l;
    }

    public static LinearLayout card(Context c) {
        LinearLayout l = column(c);
        l.setBackground(shape(CARD, 16, BORDER, c));
        l.setPadding(dp(c, 16), dp(c, 14), dp(c, 16), dp(c, 14));
        l.setElevation(dp(c, 1));
        return l;
    }

    public static TextView button(Context c, String label, boolean primary) {
        TextView t = text(c, label, 14, primary ? Color.WHITE : TEXT, true);
        t.setGravity(Gravity.CENTER);
        t.setPadding(dp(c, 14), dp(c, 13), dp(c, 14), dp(c, 13));
        t.setBackground(primary
                ? ripple(c, PRIMARY, 12, 0x33FFFFFF)
                : ripple(c, Color.TRANSPARENT, 12, 0x1A17352B));
        if (!primary) {
            t.setBackground(shape(0x00000000, 12, BORDER, c));
            RippleDrawable rp = new RippleDrawable(ColorStateList.valueOf(0x1A17352B),
                    shape(0x00000000, 12, BORDER, c), null);
            t.setBackground(rp);
        }
        t.setClickable(true);
        t.setFocusable(true);
        return t;
    }

    public static TextView smallButton(Context c, String label, boolean primary) {
        TextView t = text(c, label, 12, primary ? Color.WHITE : PRIMARY, true);
        t.setGravity(Gravity.CENTER);
        t.setPadding(dp(c, 12), dp(c, 6), dp(c, 12), dp(c, 6));
        RippleDrawable rp = new RippleDrawable(ColorStateList.valueOf(0x1A17352B),
                primary ? shape(PRIMARY, 10, 0, c) : shape(0x00000000, 10, BORDER, c), null);
        t.setBackground(rp);
        t.setClickable(true);
        t.setFocusable(true);
        return t;
    }

    public static EditText field(Context c, String hint, String value, boolean multiline, boolean password) {
        EditText e = new EditText(c);
        e.setHint(hint);
        e.setText(value);
        e.setTextSize(14);
        e.setTextColor(TEXT);
        e.setHintTextColor(0xFF9AA39C);
        e.setBackground(shape(SOFT, 10, BORDER, c));
        e.setPadding(dp(c, 12), dp(c, 10), dp(c, 12), dp(c, 10));
        e.setSingleLine(!multiline);
        if (multiline) {
            e.setMinLines(3);
            e.setGravity(Gravity.TOP | Gravity.START);
        }
        int inputType = InputType.TYPE_CLASS_TEXT;
        if (multiline) {
            inputType |= InputType.TYPE_TEXT_FLAG_MULTI_LINE;
        }
        if (password) {
            inputType |= InputType.TYPE_TEXT_VARIATION_PASSWORD;
        }
        e.setInputType(inputType);
        e.setTypeface(Typeface.create("sans-serif", Typeface.NORMAL));
        return e;
    }

    public static LinearLayout labelBlock(Context c, String label, View input, String hint) {
        LinearLayout l = column(c);
        l.addView(text(c, label, 12, MUTED, false));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(c, 5);
        l.addView(input, lp);
        if (hint != null) {
            TextView h = text(c, hint, 11, 0xFF8A938C, false);
            LinearLayout.LayoutParams hl = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            hl.topMargin = dp(c, 5);
            l.addView(h, hl);
        }
        return l;
    }

    public static View spacer(Context c, int heightDp) {
        View v = new View(c);
        v.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(c, heightDp)));
        return v;
    }

    public static LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    public static Button dummy() {
        return null;
    }

    /** 让内容避开状态栏/刘海，底栏避开手势条。 */
    public static void padSystemBars(View root, final View content, final View bottomBar) {
        final int baseTop = content.getPaddingTop();
        final int baseBottomBar = bottomBar == null ? 0 : bottomBar.getPaddingBottom();
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int top;
            int bottom;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets i = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                top = i.top;
                bottom = i.bottom;
            } else {
                top = insets.getSystemWindowInsetTop();
                bottom = insets.getSystemWindowInsetBottom();
            }
            content.setPadding(content.getPaddingLeft(), baseTop + top,
                    content.getPaddingRight(), content.getPaddingBottom());
            if (bottomBar != null) {
                bottomBar.setPadding(bottomBar.getPaddingLeft(), bottomBar.getPaddingTop(),
                        bottomBar.getPaddingRight(), baseBottomBar + bottom);
            }
            return insets;
        });
        root.requestApplyInsets();
    }

    public static LinearLayout.LayoutParams weight() {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1);
    }

    /** 底部导航：录音 / 设置。半透明悬浮胶囊，内容可从下面透出（近似 ColorOS 液态玻璃）。 */
    public static LinearLayout bottomNav(final Activity a, String current) {
        LinearLayout outer = new LinearLayout(a);
        outer.setOrientation(LinearLayout.VERTICAL);
        outer.setPadding(dp(a, 14), dp(a, 8), dp(a, 14), dp(a, 8));

        LinearLayout bar = row(a);
        GradientDrawable glass = new GradientDrawable(
                GradientDrawable.Orientation.TL_BR,
                new int[]{0xFCFFFFFF, 0xF7F3F5F0});
        glass.setCornerRadius(dp(a, 26));
        glass.setStroke(Math.max(1, dp(a, 1)), 0xD8FFFFFF);
        bar.setBackground(glass);
        bar.setElevation(dp(a, 16));
        bar.setPadding(dp(a, 6), dp(a, 5), dp(a, 6), dp(a, 5));
        bar.addView(tab(a, "录音", "records", current), weight());
        bar.addView(tab(a, "设置", "settings", current), weight());
        outer.addView(bar, matchWrap());
        return outer;
    }

    private static View tab(final Activity a, String label, final String key, final String current) {
        boolean active = key.equals(current);
        LinearLayout v = column(a);
        v.setGravity(Gravity.CENTER);
        v.setPadding(0, dp(a, 10), 0, dp(a, 10));
        v.setClickable(true);
        if (active) {
            v.setBackground(shape(0xFFE3EAE2, 18, 0, a));
        }
        TextView t = text(a, label, 12.5f, active ? PRIMARY : MUTED, active);
        t.setGravity(Gravity.CENTER);
        v.addView(t);
        v.setOnClickListener(x -> {
            if (active) {
                return;
            }
            if ("records".equals(key) && a instanceof MainActivity) {
                a.finish();
                a.overridePendingTransition(0, 0);
                return;
            }
            Intent i = new Intent(a, key.equals("records") ? RecordingsActivity.class : MainActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            a.startActivity(i);
            a.overridePendingTransition(0, 0);
        });
        return v;
    }

    /** 筛选 chip。 */
    public static TextView chip(Context c, String label, boolean active) {
        TextView t = text(c, label, 13, active ? Color.WHITE : TEXT, active);
        t.setGravity(Gravity.CENTER);
        t.setPadding(dp(c, 14), dp(c, 8), dp(c, 14), dp(c, 8));
        if (active) {
            t.setBackground(shape(PRIMARY, 999, 0, c));
        } else {
            t.setBackground(shape(CARD, 999, BORDER, c));
        }
        t.setClickable(true);
        return t;
    }
}
