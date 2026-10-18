"""ADB access: device resolution, UI hierarchy parsing, screenshots, input."""
from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ADB = os.environ.get("ADB", "adb")
MAX_ELEMENTS = 150


def _run(args: list[str], timeout: float = 20, binary: bool = False):
    p = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def resolve_serial(name: str | None) -> str:
    """Accept an exact adb serial, a host name (matches `host:PORT` serials of wireless devices), or None to use
    the only connected device."""
    out = _run(["devices"])
    serials = [l.split("\t")[0] for l in out.splitlines()[1:] if l.strip().endswith("\tdevice")]
    if not name:
        if len(serials) == 1:
            return serials[0]
        raise RuntimeError(f"{len(serials)} devices connected ({serials}); pick one with --device or "
                           "$QUICKQA_DEVICE" if serials else "no device in `adb devices`")
    if name in serials:
        return name
    matches = [s for s in serials if s.split(":")[0] == name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"device '{name}' not found in `adb devices`: {serials}")
    raise RuntimeError(f"device '{name}' is ambiguous: {matches}")


@dataclass
class Element:
    id: str
    cls: str
    text: str
    desc: str
    rid: str
    bounds: tuple[int, int, int, int]
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    checkable: bool = False
    checked: bool = False
    selected: bool = False
    focused: bool = False
    enabled: bool = True
    password: bool = False
    label: str = ""  # text gathered from descendants (for clickable containers)

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def describe(self) -> str:
        parts = [self.cls]
        if self.text:
            parts.append(f'"{self.text[:80]}"')
        if self.desc and self.desc != self.text:
            parts.append(f"desc=\"{self.desc[:80]}\"")
        if self.label and not self.text and not self.desc:
            parts.append(f"label=\"{self.label[:80]}\"")
        if self.rid:
            parts.append(f"id={self.rid}")
        flags = [n for n, v in (
            ("clickable", self.clickable), ("long-clickable", self.long_clickable),
            ("scrollable", self.scrollable), ("editable", self.editable),
            ("checked" if self.checked else "unchecked", self.checkable),
            ("selected", self.selected), ("focused", self.focused),
            ("disabled", not self.enabled), ("password", self.password)) if v]
        if flags:
            parts.append("[" + ",".join(flags) + "]")
        x1, y1, x2, y2 = self.bounds
        parts.append(f"@({x1},{y1},{x2},{y2})")
        return " ".join(parts)


@dataclass
class Screen:
    package: str
    elements: list[Element] = field(default_factory=list)
    width: int = 0
    height: int = 0
    screenshot: bytes | None = None  # PNG or JPEG
    opaque: bool = False  # a large area shows content the UI tree does not describe (web, canvas, image)

    def by_id(self, eid: str) -> Element | None:
        return next((e for e in self.elements if e.id == eid), None)

    def listing(self) -> str:
        return "\n".join(f"{e.id}: {e.describe()}" for e in self.elements) or "(no accessible elements)"


_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _short_cls(c: str) -> str:
    c = c.rsplit(".", 1)[-1]
    return c or "View"


def parse_hierarchy(xml: str, width: int, height: int) -> tuple[str, list[Element], bool]:
    start = xml.find("<?xml")
    end = xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise ValueError("no hierarchy in dump output")
    root = ET.fromstring(xml[start:end + len("</hierarchy>")])
    package = ""
    elements: list[Element] = []

    def texts_under(node) -> list[str]:
        out = []
        for n in node.iter("node"):
            t = n.get("text") or n.get("content-desc") or ""
            if t.strip():
                out.append(t.strip())
        return out

    def walk(node, parent_label: str = ""):
        nonlocal package
        a = node.attrib
        package = package or a.get("package", "")
        m = _BOUNDS.match(a.get("bounds", ""))
        label = ""
        if m:
            x1, y1, x2, y2 = map(int, m.groups())
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width or x2, x2), min(height or y2, y2)
            visible = x2 > x1 and y2 > y1 and a.get("visible-to-user", "true") == "true"
            cls = a.get("class", "")
            t = lambda k: a.get(k) == "true"
            editable = "EditText" in cls or "TextField" in cls or a.get("editable") == "true"
            text = (a.get("text") or "").strip()
            desc = (a.get("content-desc") or "").strip()
            actionable = t("clickable") or t("long-clickable") or t("scrollable") or editable or t("checkable")
            interesting = text or desc or actionable
            # Plain text that only repeats the label of its clickable container adds noise, not information.
            redundant = not actionable and parent_label and all(
                v in parent_label for v in (text, desc) if v)
            if visible and interesting and not redundant:
                rid = a.get("resource-id", "")
                rid = rid.split(":id/")[-1] if ":id/" in rid else rid
                if (t("clickable") or t("long-clickable")) and not text and not desc:
                    label = " | ".join(texts_under(node)[:4])
                elements.append(Element(
                    id="", cls=_short_cls(cls), text=text, desc=desc, rid=rid,
                    bounds=(x1, y1, x2, y2), clickable=t("clickable"), long_clickable=t("long-clickable"),
                    scrollable=t("scrollable"), editable=editable, checkable=t("checkable"),
                    checked=t("checked"), selected=t("selected"), focused=t("focused"),
                    enabled=a.get("enabled", "true") == "true", password=t("password"), label=label))
        for child in node:
            walk(child, label or parent_label)

    for child in root:
        walk(child)
    elements = elements[:MAX_ELEMENTS]
    for i, e in enumerate(elements, 1):
        e.id = f"e{i}"
    return package, elements, _has_opaque_region(root, width, height)


def _has_opaque_region(root, width: int, height: int, min_fraction: float = 0.3) -> bool:
    """True when a large on-screen node exposes no text below it: a WebView whose content is not in the
    accessibility tree, a canvas/game, a photo or video. The element list alone cannot describe that area."""
    screen_area = max(1, width * height)
    for n in root.iter("node"):
        m = _BOUNDS.match(n.get("bounds", ""))
        if not m or n.get("visible-to-user", "true") != "true":
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        area = max(0, min(width, x2) - max(0, x1)) * max(0, min(height, y2) - max(0, y1))
        if area < min_fraction * screen_area:
            continue
        has_text = any((d.get("text") or d.get("content-desc") or "").strip()
                       for d in n.iter("node") if d is not n)
        if not has_text:
            return True
    return False


def thumbnail_signature(img: bytes | None) -> tuple:
    """Coarse 12x24 grayscale fingerprint used to notice visual changes the UI tree does not show."""
    if not img:
        return ()
    from PIL import Image
    im = Image.open(io.BytesIO(img))
    im.draft("L", (im.width // 8, im.height // 8))
    return tuple(v // 16 for v in im.convert("L").resize((12, 24)).getdata())


class Device:
    def __init__(self, name: str):
        self.serial = resolve_serial(name)
        size = self.shell("wm size")
        m = re.findall(r"(\d+)x(\d+)", size)
        self.width, self.height = map(int, m[-1]) if m else (0, 0)
        self._has_adbkeyboard = None
        self._uiserver: UiServer | None = None
        self._uiserver_failed = os.environ.get("QUICKQA_NO_SERVER") == "1"

    # -- low level -------------------------------------------------------
    def adb(self, *args, timeout: float = 20, binary: bool = False):
        return _run(["-s", self.serial, *args], timeout=timeout, binary=binary)

    def shell(self, cmd: str, timeout: float = 20) -> str:
        return self.adb("shell", cmd, timeout=timeout)

    # -- observation -----------------------------------------------------
    def _server(self) -> "UiServer | None":
        if self._uiserver is None and not self._uiserver_failed:
            try:
                self._uiserver = UiServer(self)
            except Exception as ex:
                self._uiserver_failed = True
                print(f"[quickqa] fast UI server unavailable, falling back to uiautomator dump: {ex}")
        return self._uiserver

    def dump_xml(self, retries: int = 3) -> str:
        srv = self._server()
        if srv:
            try:
                out = srv.call("dump")
                if "<hierarchy" in out and "<node" in out:
                    return out
            except Exception as ex:
                print(f"[quickqa] UI server dump failed ({ex}); restarting it")
                srv.close()
                self._uiserver = None
        last = ""
        for _ in range(retries):
            out = self.adb("exec-out", "uiautomator dump --compressed /dev/tty", timeout=30)
            if "<hierarchy" in out:
                return out
            last = out
            time.sleep(0.3)
        raise RuntimeError(f"uiautomator dump failed: {last.strip()[:200]}")

    def screenshot(self) -> bytes:
        """PNG or JPEG bytes of the current screen."""
        srv = self._server()
        if srv:
            try:
                import base64
                return base64.b64decode(srv.call("shot").strip())
            except Exception:
                pass
        return self.adb("exec-out", "screencap -p", timeout=30, binary=True)

    def observe(self, with_screenshot: bool = True) -> Screen:
        if self._server():
            xml = self.dump_xml()
            img = self.screenshot() if with_screenshot else None
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(2) as ex:
                fx = ex.submit(self.dump_xml)
                fs = ex.submit(self.screenshot) if with_screenshot else None
                xml = fx.result()
                img = fs.result() if fs else None
        package, elements, opaque = parse_hierarchy(xml, self.width, self.height)
        ok = img and (img[:4] == b"\x89PNG" or img[:2] == b"\xff\xd8")
        return Screen(package=package, elements=elements, width=self.width, height=self.height,
                      screenshot=img if ok else None, opaque=opaque)

    def close(self):
        if self._uiserver:
            self._uiserver.close()
            self._uiserver = None

    def device_time(self) -> str:
        return self.shell("date +'%m-%d %H:%M:%S.000'").strip()

    def crash_log(self, since: str) -> str:
        return self.adb("logcat", "-b", "crash", "-d", "-T", since, timeout=20).strip()

    def third_party_packages(self) -> list[str]:
        return sorted(l.removeprefix("package:").strip() for l in self.shell("pm list packages -3").splitlines() if l.strip())

    # -- input -----------------------------------------------------------
    def tap(self, x: int, y: int):
        self.shell(f"input tap {x} {y}")

    def long_press(self, x: int, y: int, ms: int = 800):
        self.shell(f"input swipe {x} {y} {x} {y} {ms}")

    def swipe(self, x1, y1, x2, y2, ms: int = 300):
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}")

    def scroll(self, direction: str, region: tuple[int, int, int, int] | None = None):
        """direction = where the hidden content is: 'down' reveals content below (finger moves up)."""
        x1, y1, x2, y2 = region or (0, int(self.height * 0.15), self.width, int(self.height * 0.85))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        dx, dy = int((x2 - x1) * 0.35), int((y2 - y1) * 0.35)
        vec = {"down": (0, -dy), "up": (0, dy), "right": (-dx, 0), "left": (dx, 0)}[direction]
        self.swipe(cx - vec[0], cy - vec[1], cx + vec[0], cy + vec[1], 350)

    def key(self, code: str | int):
        self.shell(f"input keyevent {code}")

    def clear_focused_field(self):
        # Ctrl+A then Delete (Android 13+ `input keycombination`), fall back to many DELs.
        out = self.shell("input keycombination 113 29 && input keyevent 67")
        if "Error" in out or "Unknown" in out:
            self.shell("input keyevent KEYCODE_MOVE_END && input keyevent " + " ".join(["67"] * 60))

    def type_text(self, text: str):
        if text.isascii():
            # `input text` treats %s as space; escape for the remote shell.
            self.shell("input text " + shlex.quote(text.replace("%", "\\%").replace(" ", "%s")))
            return
        if self._has_adbkeyboard is None:
            self._has_adbkeyboard = "com.android.adbkeyboard" in self.shell("pm list packages com.android.adbkeyboard")
        if not self._has_adbkeyboard:
            raise RuntimeError("non-ASCII text needs ADBKeyBoard (com.android.adbkeyboard) installed and enabled as IME")
        import base64
        b64 = base64.b64encode(text.encode()).decode()
        self.shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}")

    def launch(self, package: str):
        package = package.strip()
        if "/" in package:
            self.shell(f"am start -n {shlex.quote(package)}")
        else:
            self.shell(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1")

    def force_stop(self, package: str):
        self.shell(f"am force-stop {shlex.quote(package)}")

    def ensure_ready(self) -> str:
        """Wake the screen and dismiss a lock screen that has no PIN/pattern. Returns "" when the device is usable,
        else why not. A secure lock is left alone: quick-qa never enters credentials."""
        if "mWakefulness=Awake" not in self.shell("dumpsys power | grep mWakefulness="):
            self.key("KEYCODE_WAKEUP")
            time.sleep(0.6)
        if "isKeyguardShowing=true" in self.shell("dumpsys window | grep isKeyguardShowing"):
            self.shell("wm dismiss-keyguard")
            time.sleep(0.8)
            if "isKeyguardShowing=true" in self.shell("dumpsys window | grep isKeyguardShowing"):
                return "the device is locked with a PIN/pattern/password; unlock it and run again"
        return ""


SERVER_JAR = Path(__file__).parent / "server" / "qqserver.jar"
REMOTE_JAR = "/data/local/tmp/qqserver.jar"


class UiServer:
    """Persistent on-device UiAutomation process (see server/QQServer.java)."""

    def __init__(self, dev: "Device"):
        if not SERVER_JAR.exists():
            raise RuntimeError(f"{SERVER_JAR} missing - run quickqa/server/build.sh")
        local = hashlib.md5(SERVER_JAR.read_bytes()).hexdigest()
        remote = dev.shell(f"md5sum {REMOTE_JAR} 2>/dev/null").split(" ")[0].strip()
        if remote != local:
            dev.adb("push", str(SERVER_JAR), REMOTE_JAR, timeout=30)
        self.p = subprocess.Popen(
            [ADB, "-s", dev.serial, "shell", f"CLASSPATH={REMOTE_JAR} exec app_process / QQServer"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lock = threading.Lock()
        first = self._read_until(b"<<READY>>", timeout=15)
        if first is None:
            self.close()
            raise RuntimeError("server did not report ready")

    def _read_until(self, marker: bytes, timeout: float) -> bytes | None:
        buf = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                return None
            if line.strip() == marker:
                return b"".join(buf)
            buf.append(line)
        return None

    def call(self, cmd: str, timeout: float = 20) -> str:
        with self.lock:
            if self.p.poll() is not None:
                raise RuntimeError("server exited")
            self.p.stdin.write(cmd.encode() + b"\n")
            self.p.stdin.flush()
            out = self._read_until(b"<<END>>", timeout)
        if out is None:
            raise RuntimeError(f"no response to {cmd}")
        text = out.decode("utf-8", "replace")
        if text.startswith("ERR "):
            raise RuntimeError(text.strip())
        return text

    def close(self):
        try:
            self.p.stdin.write(b"quit\n")
            self.p.stdin.flush()
            self.p.wait(timeout=2)
        except Exception:
            self.p.kill()


def downscale_jpeg(png: bytes, max_side: int = 960, quality: int = 70) -> tuple[bytes, float]:
    """Return (jpeg bytes, scale factor applied)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    scale = min(1.0, max_side / max(im.size))
    if scale < 1.0:
        im = im.resize((round(im.width * scale), round(im.height * scale)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), scale
