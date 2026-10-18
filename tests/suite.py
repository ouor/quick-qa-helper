"""Runs quickqa against several stock apps (non-destructive tasks) and prints a scoreboard.

    python tests/suite.py [--set complex|basic] [--only name1,name2] [--device SERIAL] [--effort medium]

The package names are those of AOSP / LineageOS stock apps (com.android.calculator2, com.android.deskclock, ...);
on Pixel or other vendor builds swap in that vendor's packages. The complex scenarios create and then delete a
contact and an alarm, toggle Dark theme and restore it, and open Chrome tabs; nothing is installed or bought.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CASES = {
    "calc": ("com.android.calculator2",
             "Calculate 12 × 34 using the calculator buttons and check that the result shown is 408."),
    "settings": ("com.android.settings",
                 "Open 'About phone' in Settings and report the Android version shown there."),
    "clock": ("com.android.deskclock",
              "Go to the Timer tab and enter a duration of 1 minute 30 seconds, but do NOT start it. "
              "Check that the timer display shows 1m 30s (01:30)."),
    "chrome": ("com.android.chrome",
               "Open https://example.com in Chrome (dismiss any first-run screens without signing in) "
               "and check that the page heading is 'Example Domain'."),
    "contacts": ("com.android.contacts",
                 "Search contacts for 'zzqx' and confirm that no matching contact is found."),
    "files": ("com.android.documentsui",
              "Open the Downloads folder in the Files app and report how many items it contains."),
}

# Multi-stage scenarios: several screens/apps, remembered values compared later, conditional branches,
# and self-cleanup. All are non-destructive to pre-existing data (nothing installed, bought, or sent).
COMPLEX = {
    "playstore": ("com.android.vending",
                  "In the Play Store (do NOT install or buy anything): search for 'Firefox' and open the page of "
                  "Mozilla's 'Firefox' browser. Open its 'About this app' section and note the 'Updated on' date. "
                  "Go back, find the ratings section and note the overall star rating and the number of reviews. "
                  "Then search for 'Brave' and open 'Brave Private Web Browser', and note its overall star rating "
                  "and number of reviews. Finally report both apps' ratings and review counts, Firefox's update "
                  "date, and which app has the higher star rating."),
    "contacts_lifecycle": ("com.android.contacts",
                           "Create a new contact (save it on the device if asked where) with first name 'QA', last "
                           "name 'Tester', phone 01012345678. Save it and confirm the detail screen shows that "
                           "number. Then edit the contact to add the email qa.tester@example.com and save. Go back "
                           "to the contact list, search for 'QA Tester', open it and confirm both the phone number "
                           "and the email are shown. Finally delete this contact and confirm that searching for "
                           "'QA Tester' finds no contact."),
    "alarm_lifecycle": ("com.android.deskclock",
                        "In the Alarm tab create a new alarm for 6:45 AM. Give it the label 'QA-alarm' and make it "
                        "repeat only on Monday and Wednesday. Confirm the alarm in the list shows 6:45, the label "
                        "and the two days. Then switch this alarm off and confirm it is off. Finally delete this "
                        "alarm and confirm no 'QA-alarm' alarm remains in the list. Do not touch other alarms."),
    "cross_app": ("com.android.settings",
                  "In Settings open 'About phone' and note the model name and the Android version. Then press "
                  "home, open Chrome (package com.android.chrome) and search Google for the model name followed "
                  "by 'specifications', using the value you noted. Open the first search result that is not an "
                  "ad and report the page's title. Then use the recent-apps screen (or launch Settings again) "
                  "to return to Settings and confirm it still shows the About phone screen with the same model "
                  "name."),
    "chrome_tabs": ("com.android.chrome",
                    "In Chrome open a NEW tab and load https://en.m.wikipedia.org/wiki/Android_(operating_system). "
                    "Find the initial release date in the article (use a screenshot if the text is not in the "
                    "element list) and note it. Open another NEW tab and load https://example.com. Open the tab "
                    "switcher and confirm both the Wikipedia tab and the example.com tab are listed. Close the "
                    "example.com tab from the tab switcher, then open the Wikipedia tab and confirm the article "
                    "title is still shown. Report the initial release date."),
    "display_restore": ("com.android.settings",
                        "In Settings open the Display settings. Note whether Dark theme is currently on or off. "
                        "Toggle Dark theme to the opposite state and confirm the switch changed. Then toggle it "
                        "back so it ends in the state you originally noted, and confirm it is restored. Report the "
                        "original state."),
}
MAX_STEPS = {"basic": 25, "complex": 60}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", help="adb serial (default: $QUICKQA_DEVICE, else the only connected device)")
    ap.add_argument("--only", help="comma-separated case names")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--set", choices=["basic", "complex"], default="complex")
    a = ap.parse_args()
    cases = CASES if a.set == "basic" else COMPLEX
    names = a.only.split(",") if a.only else list(cases)
    rows = []
    for name in names:
        app, task = cases[name]
        print(f"\n=== {name}: {task}", flush=True)
        t = time.time()
        p = subprocess.run([sys.executable, "-m", "quickqa", task, "--app", app,
                            *(["--device", a.device] if a.device else []),
                            "--effort", a.effort, "--max-steps", str(MAX_STEPS[a.set]),
                            "--save-screens", "--json", "-q"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        try:
            r = json.loads(p.stdout[p.stdout.index("{"):])
        except ValueError:
            print(p.stdout[-2000:], p.stderr[-2000:])
            rows.append((name, "crashed", 0, round(time.time() - t, 1), ""))
            continue
        steps = r["steps"]
        per = [s["timings"] for s in steps]
        avg = {k: round(sum(x.get(k, 0) for x in per) / max(1, sum(k in x for x in per)), 2)
               for k in ("observe", "plan", "select", "act")}
        print(f"status={r['status']} steps={len(steps)} {r['elapsed_s']}s avg={avg}")
        print(f"summary: {r['summary']}")
        for s in steps:
            sel = s["selection"]
            print(f"  {s['n']}. {s['action']} {s['target']!r} {s['text']!r} -> {sel.get('how', '')} "
                  f"{(sel.get('described') or '')[:70]} | {s['result']}")
        pu = r.get("planner_usage", {})
        if "input_tokens" in pu:
            print(f"planner: {pu['calls']} calls, in {pu['input_tokens']} out {pu['output_tokens']} "
                  f"peak {pu['peak_input_tokens']} ~${pu['est_cost_usd']}")
        rows.append((name, r["status"], len(steps), r["elapsed_s"], r["summary"]))
    print("\n=== SCOREBOARD")
    for name, status, n, el, summ in rows:
        print(f"{name:9} {status:22} steps={n:<3} {el:>6}s  {summ[:90]}")


if __name__ == "__main__":
    main()
