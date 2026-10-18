"""Before/after snapshot of an app's private data, to show what a QA run changed (view mode, saved filters...).

Uses `run-as`, so it works for debuggable builds - the apps under development. Release builds return None.
"""
from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET

NOISY = re.compile(r"(^\./(cache|code_cache|app_webview|no_backup)/)|(-wal$|-shm$|-journal$)")
_SNAPSHOT = ("find . -type f 2>/dev/null | head -400 | while read f; do md5sum \"$f\"; done; "
             "for f in shared_prefs/*.xml; do [ -f \"$f\" ] && echo \"==> $f\" && cat \"$f\"; done; echo __QQ_OK__")


def app_state_snapshot(dev, app: str | None) -> dict | None:
    if not app:
        return None
    pkg = app.split("/")[0]
    out = dev.shell(f"run-as {shlex.quote(pkg)} sh -c {shlex.quote(_SNAPSHOT)}", timeout=30)
    if "__QQ_OK__" not in out:  # run-as refused (release build); its error goes to stderr
        return None
    out = out.replace("__QQ_OK__", "")
    hashes, prefs, cur = {}, {}, None
    for line in out.splitlines():
        if line.startswith("==> "):
            cur = line[4:].strip()
            prefs[cur] = []
        elif cur is not None:
            prefs[cur].append(line)
        else:
            m = re.match(r"([0-9a-f]{32})\s+(.+)$", line.strip())
            if m and not NOISY.search(m.group(2)):
                hashes[m.group(2)] = m.group(1)
    return {"hashes": hashes, "prefs": {k: _parse_prefs("\n".join(v)) for k, v in prefs.items()}}


def _parse_prefs(xml: str) -> dict:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    out = {}
    for el in root:
        name = el.get("name")
        if name is not None:
            out[name] = el.get("value") if el.get("value") is not None else (el.text or "")
    return out


def diff_app_state(before: dict | None, after: dict | None) -> list[str] | None:
    if before is None or after is None:
        return None
    changes: list[str] = []
    for f in sorted(set(before["prefs"]) | set(after["prefs"])):
        b, a = before["prefs"].get(f, {}), after["prefs"].get(f, {})
        for k in sorted(set(b) | set(a)):
            if b.get(k) != a.get(k):
                changes.append(f"{f}: {k}: {str(b.get(k))[:60]} -> {str(a.get(k))[:60]}")
    for f in sorted(set(before["hashes"]) | set(after["hashes"])):
        if f.startswith("./shared_prefs/"):
            continue  # reported key by key above
        if f not in before["hashes"]:
            changes.append(f"added {f}")
        elif f not in after["hashes"]:
            changes.append(f"removed {f}")
        elif before["hashes"][f] != after["hashes"][f]:
            changes.append(f"modified {f}")
    return changes
