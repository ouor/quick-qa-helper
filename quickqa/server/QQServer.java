// Persistent on-device helper, launched with:
//   CLASSPATH=/data/local/tmp/qqserver.jar app_process / QQServer
// Keeps one UiAutomation connection open so each UI dump costs ~100ms instead of ~2.3s
// for a fresh `uiautomator dump`. Protocol (stdin/stdout, one command per line):
//   dump  -> uiautomator-compatible XML, then a line "<<END>>"
//   shot  -> base64 JPEG on one line, then "<<END>>"
//   quit
import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.os.HandlerThread;
import android.os.Looper;
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
                if (line.equals("dump")) {
                    out.print(dump());
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

    private static String dump() throws Exception {
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

    private static String shot() {
        Bitmap b = ua.takeScreenshot();
        if (b == null) throw new IllegalStateException("takeScreenshot returned null");
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        b.compress(Bitmap.CompressFormat.JPEG, 85, bos);
        b.recycle();
        return Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP) + "\n";
    }
}
