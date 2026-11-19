// Persistent on-device helper, launched with:
//   CLASSPATH=/data/local/tmp/qqserver.jar app_process / QQServer
// Keeps one UiAutomation connection open so each UI dump costs ~100ms instead of ~2.3s
// for a fresh `uiautomator dump`. Protocol (stdin/stdout, one command per line):
//   dump  -> uiautomator-compatible XML, then a line "<<END>>"
//   dump fresh -> same, but every node is re-read from the app (bypasses the accessibility cache)
//   shot  -> base64 JPEG on one line, then "<<END>>"
//   taps X Y COUNT INTERVAL_MS HOLD_MS -> injected touches with exact timing (double tap, long press)
//   swipe X1 Y1 X2 Y2 MS -> injected drag/fling along a straight line
//   focused -> base64 UTF-8 text of the focused input field ("" if empty), or "none"
//   settext B64 -> replace the focused input field's text (ACTION_SET_TEXT; bypasses the keyboard/IME)
//   quit
import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.util.Base64;
import android.view.accessibility.AccessibilityNodeInfo;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;

public class QQServer {
    private static UiAutomation ua;

    public static void main(String[] args) throws Exception {
        // AccessibilityInteractionClient needs a main looper; serve commands on a worker thread.
        Looper.prepareMainLooper();
        Thread worker = new Thread(() -> {
            try {
                serve();
            } catch (Throwable t) {
                t.printStackTrace();
            }
            System.exit(0);
        }, "qq-serve");
        worker.start();
        Looper.loop();
    }

    private static void serve() throws Exception {
        PrintStream out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), false, "UTF-8");
        HandlerThread ht = new HandlerThread("qq");
        ht.start();
        ua = connect(ht.getLooper());
        AccessibilityServiceInfo info = ua.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
        ua.setServiceInfo(info);
        out.println("<<READY>>");
        out.flush();

        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, "UTF-8"));
        String line;
        while ((line = in.readLine()) != null) {
            line = line.trim();
            try {
                if (line.equals("dump") || line.equals("dump fresh")) {
                    out.print(dump(line.endsWith("fresh")));
                } else if (line.startsWith("taps ")) {
                    String[] a = line.split(" ");
                    taps(Float.parseFloat(a[1]), Float.parseFloat(a[2]), Integer.parseInt(a[3]),
                            Long.parseLong(a[4]), Long.parseLong(a[5]));
                    out.print("ok\n");
                } else if (line.startsWith("swipe ")) {
                    String[] a = line.split(" ");
                    swipe(Float.parseFloat(a[1]), Float.parseFloat(a[2]), Float.parseFloat(a[3]),
                            Float.parseFloat(a[4]), Long.parseLong(a[5]));
                    out.print("ok\n");
                } else if (line.equals("focused")) {
                    AccessibilityNodeInfo f = focusedInput();
                    CharSequence t = f == null || f.isShowingHintText() ? null : f.getText();
                    out.print(f == null ? "none\n" : b64(t == null ? "" : t.toString()) + "\n");
                } else if (line.startsWith("settext")) {
                    String text = new String(Base64.decode(line.substring(7).trim(), Base64.DEFAULT), "UTF-8");
                    AccessibilityNodeInfo f = focusedInput();
                    android.os.Bundle args = new android.os.Bundle();
                    args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
                    boolean ok = f != null && f.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
                    out.print(ok ? "ok\n" : "ERR set text failed\n");
                } else if (line.equals("shot")) {
                    out.print(shot());
                } else if (line.equals("quit")) {
                    break;
                } else {
                    out.print("ERR unknown command\n");
                }
            } catch (Throwable t) {
                out.print("ERR " + t + "\n");
            }
            out.print("<<END>>\n");
            out.flush();
        }
        System.exit(0);
    }

    private static UiAutomation connect(Looper looper) throws Exception {
        Class<?> connCls = Class.forName("android.app.UiAutomationConnection");
        Class<?> iconn = Class.forName("android.app.IUiAutomationConnection");
        Object conn = connCls.getDeclaredConstructor().newInstance();
        UiAutomation u = null;
        for (Constructor<?> c : UiAutomation.class.getDeclaredConstructors()) {
            Class<?>[] p = c.getParameterTypes();
            if (p.length == 2 && p[0] == Looper.class && p[1] == iconn) {
                c.setAccessible(true);
                u = (UiAutomation) c.newInstance(looper, conn);
                break;
            }
        }
        if (u == null) {
            // Newer platforms: UiAutomation(Context, IUiAutomationConnection); a null context falls back to main looper.
            for (Constructor<?> c : UiAutomation.class.getDeclaredConstructors()) {
                Class<?>[] p = c.getParameterTypes();
                if (p.length == 2 && p[1] == iconn) {
                    c.setAccessible(true);
                    u = (UiAutomation) c.newInstance(null, conn);
                    break;
                }
            }
        }
        if (u == null) throw new IllegalStateException("no usable UiAutomation constructor");
        Method m;
        try {
            m = UiAutomation.class.getDeclaredMethod("connect", int.class);
            m.setAccessible(true);
            m.invoke(u, 0);
        } catch (NoSuchMethodException e) {
            m = UiAutomation.class.getDeclaredMethod("connect");
            m.setAccessible(true);
            m.invoke(u);
        }
        return u;
    }

    private static boolean fresh;

    private static String dump(boolean refresh) throws Exception {
        fresh = refresh;
        try {
            ua.waitForIdle(80, 1500);
        } catch (Exception ignored) {
        }
        AccessibilityNodeInfo root = null;
        for (int i = 0; i < 10 && root == null; i++) {
            root = ua.getRootInActiveWindow();
            if (root == null) Thread.sleep(100);
        }
        StringBuilder sb = new StringBuilder(32768);
        sb.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">");
        if (root != null) node(sb, root, 0, 0);
        sb.append("</hierarchy>\n");
        return sb.toString();
    }

    private static void node(StringBuilder sb, AccessibilityNodeInfo n, int index, int depth) {
        if (n == null || depth > 80) return;
        if (fresh) n.refresh();
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        sb.append("<node index=\"").append(index).append('"');
        attr(sb, "text", n.getText());
        attr(sb, "resource-id", n.getViewIdResourceName());
        attr(sb, "class", n.getClassName());
        attr(sb, "package", n.getPackageName());
        attr(sb, "content-desc", n.getContentDescription());
        CharSequence hint = n.getHintText();
        if (hint != null) attr(sb, "hint", hint);
        if (android.os.Build.VERSION.SDK_INT >= 30) {
            CharSequence st = n.getStateDescription();
            if (st != null) attr(sb, "state-desc", st);
        }
        bool(sb, "checkable", n.isCheckable());
        bool(sb, "checked", n.isChecked());
        bool(sb, "clickable", n.isClickable());
        bool(sb, "enabled", n.isEnabled());
        bool(sb, "focusable", n.isFocusable());
        bool(sb, "focused", n.isFocused());
        bool(sb, "scrollable", n.isScrollable());
        bool(sb, "long-clickable", n.isLongClickable());
        bool(sb, "password", n.isPassword());
        bool(sb, "selected", n.isSelected());
        bool(sb, "editable", n.isEditable());
        bool(sb, "visible-to-user", n.isVisibleToUser());
        sb.append(" bounds=\"[").append(r.left).append(',').append(r.top).append("][")
                .append(r.right).append(',').append(r.bottom).append("]\">");
        int count = n.getChildCount();
        for (int i = 0; i < count; i++) {
            AccessibilityNodeInfo c = n.getChild(i);
            if (c != null) node(sb, c, i, depth + 1);
        }
        sb.append("</node>");
    }

    private static void bool(StringBuilder sb, String k, boolean v) {
        sb.append(' ').append(k).append("=\"").append(v).append('"');
    }

    private static void attr(StringBuilder sb, String k, CharSequence v) {
        sb.append(' ').append(k).append("=\"");
        if (v != null) {
            String s = v.toString();
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '&': sb.append("&amp;"); break;
                    case '<': sb.append("&lt;"); break;
                    case '>': sb.append("&gt;"); break;
                    case '"': sb.append("&quot;"); break;
                    case '\n': sb.append("&#10;"); break;
                    case '\r': break;
                    default:
                        if (c >= 0x20 || c == '\t') sb.append(c);
                }
            }
        }
        sb.append('"');
    }

    private static void taps(float x, float y, int count, long intervalMs, long holdMs) throws Exception {
        for (int i = 0; i < count; i++) {
            if (i > 0) Thread.sleep(intervalMs);
            long down = SystemClock.uptimeMillis();
            touch(down, down, MotionEvent.ACTION_DOWN, x, y);
            if (holdMs > 0) Thread.sleep(holdMs);
            touch(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y);
        }
    }

    private static void swipe(float x1, float y1, float x2, float y2, long ms) throws Exception {
        long down = SystemClock.uptimeMillis();
        touch(down, down, MotionEvent.ACTION_DOWN, x1, y1);
        int steps = (int) Math.max(2, ms / 8);
        for (int i = 1; i <= steps; i++) {
            Thread.sleep(Math.max(1, ms / steps));
            float f = (float) i / steps;
            touch(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_MOVE, x1 + (x2 - x1) * f, y1 + (y2 - y1) * f);
        }
        touch(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x2, y2);
    }

    private static AccessibilityNodeInfo focusedInput() {
        AccessibilityNodeInfo root = ua.getRootInActiveWindow();
        if (root == null) return null;
        AccessibilityNodeInfo f = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (f != null) f.refresh();
        return f;
    }

    private static String b64(String s) throws Exception {
        return Base64.encodeToString(s.getBytes("UTF-8"), Base64.NO_WRAP);
    }

    private static void touch(long down, long t, int action, float x, float y) {
        MotionEvent e = MotionEvent.obtain(down, t, action, x, y, 0);
        e.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        if (!ua.injectInputEvent(e, true)) throw new IllegalStateException("injectInputEvent failed");
        e.recycle();
    }

    private static String shot() {
        Bitmap b = ua.takeScreenshot();
        if (b == null) throw new IllegalStateException("takeScreenshot returned null");
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        b.compress(Bitmap.CompressFormat.JPEG, 85, bos);
        b.recycle();
        return Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP) + "\n";
    }
}
