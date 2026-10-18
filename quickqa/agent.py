"""The fast QA loop.

Per step:
  1. observe   - uiautomator dump + screenshot in parallel (adb)
  2. plan      - Claude Haiku 5.5 decides the next action (structured JSON output)
  3. select    - TypeSafe Jev picks the concrete element (Choice + Noul), falling back to
                 a Box question on the screenshot when the hierarchy has no matching node
  4. act       - adb input
When the planner says "done", Jev independently verifies the goal (Noul) before we accept it.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .device import Device, Element, Screen, downscale_jpeg, thumbnail_signature
from .llm import Cerebras, Claude, Jev, PromptTooLarge, new_image_ref
from .state import app_state_snapshot, diff_app_state

TARGETED = {"tap", "long_press", "type"}
ACTIONS = ["tap", "long_press", "type", "scroll", "back", "home", "enter", "launch", "wait", "look", "none"]
# not_found: searched and could not locate it; feature_missing: positive evidence the app lacks it.
FAILURE_KINDS = ["", "not_found", "feature_missing", "app_error", "blocked"]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "purpose": {"type": "string"},
        "observation": {"type": "string"},
        "status": {"type": "string", "enum": ["continue", "done", "failed"]},
        "action": {"type": "string", "enum": ACTIONS},
        "target": {"type": "string"},
        "element_hint": {"type": "string"},
        "text": {"type": "string"},
        "direction": {"type": "string", "enum": ["up", "down", "left", "right", "none"]},
        "clear_first": {"type": "boolean"},
        "failure_kind": {"type": "string", "enum": FAILURE_KINDS},
    },
    "required": ["thought", "purpose", "observation", "status", "action", "target", "element_hint", "text",
                 "direction", "clear_first", "failure_kind"],
    "additionalProperties": False,
}

SYSTEM = """You drive an Android phone to carry out one QA task. Each turn you get the task, the
actions so far, and the current screen as a list of UI elements (id: class "text" desc=... id=resource-id [flags] @(bounds)).
Return the single next action as JSON.

Fields:
- thought: one short sentence of reasoning.
- purpose: for the human-readable step report, written in {lang}, at most ~40 characters: why you take this
  action (e.g. "to open the search field"). When status is "done" or "failed": a one-line {lang} conclusion that
  states the actual values the task asked for (e.g. "Android 16, model Pixel 8 confirmed"), not just "confirmed".
- observation: anything QA-relevant you see now (wrong text, error dialog, crash, unexpected state, or the value
  the task asked you to check). "" if nothing.
- status: "continue" while work remains; "done" when the task's goal is visibly achieved on the CURRENT screen
  (set action "none"); "failed" if it cannot be achieved (app crashed, feature missing, blocked) - explain in observation.
- action: tap | long_press | type | scroll | back | home | enter | launch | wait | none
  * tap/long_press: target = short natural-language description of the element (e.g. "the '+' add button").
  * type: target = the input field; text = what to type; clear_first = true to replace existing content.
  * scroll: direction = where the hidden content is ("down" reveals content below); target = scrollable container or "".
  * launch: text = package name (or package/activity).
  * wait: text = seconds (e.g. "1.5"), for loading screens.
  * look: attach a screenshot to your next turn. Use it when the element list does not show what you need
    (web pages/WebView content, images, colors, charts, games). Then decide from the screenshot.
  * back / home / enter: no target.
- element_hint: the element id you believe is the target (e.g. "e12"), or "".
- text, direction ("none" if unused), clear_first (false if unused).
- failure_kind: "" unless status is "failed". Then: "not_found" = you searched (see below) and could not locate
  the element/screen; "feature_missing" = the app positively shows the feature does not exist (an explicit message,
  a disabled/absent option in the place it must be) - never merely because you did not see it; "app_error" = crash,
  error dialog, hang; "blocked" = a login wall, missing permission or data you may not create.

Rules: do exactly what the task asks, nothing more. If the task asks to perform an action N times, count the
matching actions in RECENT ACTIONS and stop at N. When a task asks you to check a change, record the
"before" value in observation BEFORE acting, then compare against it afterwards. Never repeat an action that already failed to change the
screen more than twice - try another way. Dismiss unrelated popups/permission dialogs only when they block the task, choosing the least
invasive option (deny / not now / skip / no thanks) unless the task needs the permission. Never sign in, buy,
delete or send anything unless the task explicitly says so.
Do not declare done unless the current screen proves it.
Report only values you actually saw in the element list or an attached screenshot in this run - never from
memory or general knowledge. If a value you need is not visible, scroll or use "look" to find it.
When a screenshot is attached, it is the ground truth for content the element list does not show.

Finding things: when the element you need is not in the list, it is usually just off screen. Many apps collapse
the top bar (search, ⋮ overflow menu) when the content is scrolled, so FIRST scroll up to the very top, then look
in ⋮ / overflow menus, then scroll down, then go back or relaunch the app and look again. Do not tap unrelated
buttons (filters, view-mode/layout toggles, sort options, settings) to explore - those change saved app state.
Counting and positions: count items and judge columns/rows from the element list bounds, not from a screenshot
estimate, and say in observation that you counted from the element list. "Scroll to the bottom/end" means keep
scrolling until a step is marked "(screen unchanged)"."""

VERIFY_THRESHOLD = 0.5
STRONG_PASS = 0.7  # verifier scores in [0.5, 0.7) pass as "passed_weak"
# Safe mode looks twice at taps whose label suggests a state change; Jev then decides if the task needs it.
RISKY = re.compile(r"delete|remove|erase|clear|reset|discard|uninstall|log ?out|sign ?out|unsubscribe|buy|purchase"
                   r"|pay|order|send|submit|post|share|sort|filter|view|layout|grid|theme|mode|setting"
                   r"|삭제|지우기|제거|비우기|초기화|재설정|버리기|로그아웃|탈퇴|구매|결제|주문|전송|보내기|게시|공유"
                   r"|정렬|필터|보기|레이아웃|목록|카드|테마|모드|설정", re.I)
SAFE_NAV = re.compile(r"더보기|more options|navigate up|위로 탐색|뒤로", re.I)
CHOICE_CONFIDENCE = 0.5
PRESENT_THRESHOLD = 0.5   # below this, the target is probably not a node in the tree -> Box on screenshot
VISIBLE_THRESHOLD = 0.25
# Longest image side sent to each model. On a dense web page qwen read the same text at 512px as at 1024px
# with 1/4 of the image tokens; Box keeps more pixels because its coordinates are tapped directly.
PLANNER_IMAGE_SIDE = 512
VERIFY_IMAGE_SIDE = 768
BOX_IMAGE_SIDE = 1024


def _ext(img: bytes) -> str:
    return ".png" if img[:4] == b"\x89PNG" else ".jpg"


@dataclass
class StepLog:
    n: int
    package: str
    plan: dict
    selection: dict = field(default_factory=dict)
    result: str = ""
    timings: dict = field(default_factory=dict)
    thumb: bytes | None = None  # downscaled screenshot of the screen the action was taken on (for the report image)


class QARunner:
    def __init__(self, device: Device, task: str, *, app: str | None = None, max_steps: int = 25,
                 vision: str = "auto", reasoning_effort: str = "medium", restart_app: bool = False,
                 planner: str = "claude", planner_model: str | None = None, max_input_tokens: int = 100_000,
                 image_side: int = PLANNER_IMAGE_SIDE, report_lang: str = "English", report_image: bool = True,
                 run_dir: Path | None = None, save_screens: bool = False, verbose: bool = True,
                 safe: bool = True):
        self.d, self.task, self.app = device, task, app
        self.safe = safe
        self.blocked_taps: list[str] = []
        self.max_steps, self.vision, self.restart_app = max_steps, vision, restart_app
        self.image_side = image_side
        self.system = SYSTEM.replace("{lang}", report_lang)
        self.report_lang, self.report_image = report_lang, report_image
        if planner == "cerebras":
            self.planner = Cerebras(model=planner_model or "qwen-3.8-27b", reasoning_effort=reasoning_effort)
        else:
            self.planner = Claude(model=planner_model or "claude-haiku-5-5", effort=reasoning_effort,
                                  max_input_tokens=max_input_tokens)
        self.jev = Jev()
        self.run_dir, self.save_screens, self.verbose = run_dir, save_screens, verbose
        self.steps: list[StepLog] = []
        self.observations: list[str] = []
        self.feedback = ""  # message for the planner about the previous step's outcome
        self.look_next = False  # planner asked for a screenshot on its next turn

    def log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    # ------------------------------------------------------------------ planning
    def _history(self, window: int = 12) -> str:
        lines = []
        for s in self.steps[-window:]:
            p = s.plan
            tgt = s.selection.get("described") or p.get("target") or ""
            extra = f' text="{p["text"]}"' if p["action"] in ("type", "launch", "wait") and p.get("text") else ""
            extra += f' dir={p["direction"]}' if p["action"] == "scroll" else ""
            line = f'{s.n}. {p["action"]} {tgt[:100]}{extra} -> {s.result}'
            if p.get("observation"):
                # Earlier observations are the planner's only memory of before/after values.
                line += f' | noted before acting: {p["observation"][:200]}'
            lines.append(line)
        return "\n".join(lines) or "(none yet)"

    # Prompt sizes tried in order when a prompt would exceed the planner's input-token ceiling:
    # (history window, max earlier notes, max listed elements, keep screenshot)
    SHRINK_LEVELS = [(12, 30, None, True), (6, 10, 80, True), (3, 0, 40, False)]

    def plan(self, screen: Screen, packages: list[str] | None) -> tuple[dict, dict]:
        use_image = self.vision == "always" or (self.vision == "auto" and (len(screen.elements) < 4 or screen.opaque or self.look_next))
        self.look_next = False
        img = downscale_jpeg(screen.screenshot, self.image_side)[0] if use_image and screen.screenshot else None
        last_err = None
        for window, max_notes, max_elements, keep_image in self.SHRINK_LEVELS:
            parts = [f"TASK: {self.task}"]
            if self.app:
                parts.append(f"TARGET APP: {self.app}")
            if packages:
                parts.append("INSTALLED (3rd-party) PACKAGES: " + ", ".join(packages))
            older = [f"step {s.n}: {s.plan['observation'][:200]}" for s in self.steps[:-window]
                     if s.plan.get("observation")]
            if older and max_notes:
                # Long scenarios: keep values noted early on (to compare later) after they leave the action window.
                parts.append("NOTES FROM EARLIER STEPS:\n" + "\n".join(older[-max_notes:]))
            parts += [f"STEP {len(self.steps) + 1}/{self.max_steps}",
                      "RECENT ACTIONS:\n" + self._history(window)]
            if self.feedback:
                parts.append("NOTE: " + self.feedback)
            listing = screen.listing() if max_elements is None else "\n".join(
                f"{e.id}: {e.describe()}" for e in screen.elements[:max_elements])
            parts.append(f"CURRENT SCREEN (foreground package: {screen.package}, {screen.width}x{screen.height}):\n"
                         + listing)
            image = img if keep_image else None
            if image:
                parts.append("A screenshot of the current screen is attached.")
            try:
                return self.planner.json_chat(self.system, "\n\n".join(parts), PLAN_SCHEMA, image_jpeg=image)
            except PromptTooLarge as ex:
                last_err = ex
                self.log(f"    prompt {ex.args[0]} tokens > limit; shrinking")
        raise RuntimeError(f"planner prompt still over the input-token limit after shrinking ({last_err})")

    # ------------------------------------------------------------------ selection
    def _candidates(self, screen: Screen, action: str) -> list[Element]:
        els = screen.elements
        if action == "type":
            edit = [e for e in els if e.editable]
            return edit or els
        return els

    def select(self, screen: Screen, plan: dict) -> dict:
        """Let Jev pick the element; returns {x, y, element?, how, ...}."""
        action, target = plan["action"], plan["target"] or plan["action"]
        cands = self._candidates(screen, action)
        hint = plan["element_hint"] if screen.by_id(plan["element_hint"]) else ""
        sel: dict = {"target": target, "hint": hint or None}
        if cands:
            verb = {"tap": "tap", "long_press": "long-press", "type": "type into"}[action]
            elements = {e.id: e.describe() for e in cands}
            answers = self.jev.ask(
                state={"task": self.task, "screen_elements": elements},
                questions={
                    "pick": {"type": "choice",
                             "instructions": {"goal": f"{verb}: {target}", "why": plan["thought"],
                                              "question": "Which element in `screen_elements` should we "
                                                          f"{verb} to achieve `goal`?"},
                             "criteria": elements},
                    "present": {"type": "noul",
                                "instructions": f"Is the element '{target}' one of the `screen_elements`?"},
                })
            pick, present = answers["pick"], answers["present"]["noul"]
            sel.update(jev_choice=pick["choice"], confidence=round(pick["confidence"], 3), present=round(present, 3))
            if present >= PRESENT_THRESHOLD or (hint and present >= VISIBLE_THRESHOLD):
                if hint and hint != pick["choice"] and pick["confidence"] < CHOICE_CONFIDENCE:
                    chosen, how = hint, "planner-hint (jev unsure)"
                else:
                    chosen, how = pick["choice"], "jev-choice" + (" (agrees)" if hint == pick["choice"] else "")
                el = screen.by_id(chosen)
                sel.update(element=chosen, how=how, x=el.center[0], y=el.center[1], described=el.describe(),
                           label=el.text or el.desc or el.label or el.rid or el.cls, bounds=el.bounds)
                return sel
        # Fallback: locate on the screenshot with a Box question.
        if screen.screenshot:
            jpeg, scale = downscale_jpeg(screen.screenshot, BOX_IMAGE_SIDE, 80)
            ref = new_image_ref("screen")
            answers = self.jev.ask(
                state={"screen": ref},
                questions={"box": {"type": "box", "instructions": f"the UI element in `screen`: {target}"},
                           "visible": {"type": "noul", "instructions": f"Is '{target}' visible in `screen`?"}},
                images={ref: jpeg})
            box, visible = answers["box"]["box"], answers["visible"]["noul"]
            sel.update(box=box, box_confidence=round(answers["box"]["confidence"], 3), visible=round(visible, 3))
            if visible >= VISIBLE_THRESHOLD:
                x = round((box["left"] + box["right"]) / 2 * screen.width)
                y = round((box["top"] + box["bottom"]) / 2 * screen.height)
                sel.update(how="jev-box", x=x, y=y, described=f"{target} (screen box)", label=target,
                           bounds=(round(box["left"] * screen.width), round(box["top"] * screen.height),
                                   round(box["right"] * screen.width), round(box["bottom"] * screen.height)))
                return sel
        sel.update(how="not-found")
        return sel

    def _loop_warning(self, window: int = 6, limit: int = 4) -> str:
        """Spot the planner repeating one move (e.g. endless scrolling past the target) and tell it to change tack."""
        recent = self.steps[-window:]
        if len(recent) < limit:
            return ""
        key = lambda s: (s.plan["action"], s.selection.get("element") or s.plan["target"], s.plan["direction"])
        last = key(recent[-1])
        count = sum(1 for s in recent if key(s) == last)
        if count < limit:
            return ""
        return (f"WARNING: you repeated '{last[0]} {last[1]}' {count} times in the last {len(recent)} steps without "
                "reaching the goal. Stop repeating it. Use 'look' to see the real screen, scroll the other "
                "direction, go back, or reach the content another way (a different button, tab or search).")

    def _unsearched(self) -> str:
        """Which basic searches the planner skipped before claiming something is missing."""
        acts = [(s.plan["action"], s.plan["direction"]) for s in self.steps]
        missing = []
        if ("scroll", "up") not in acts:
            missing.append("scrolled up to the top (collapsed toolbars hide search / ⋮ buttons)")
        if not any(a in ("back", "launch") for a, _ in acts):
            missing.append("gone back or relaunched the app to look from its first screen")
        return " and ".join(missing)

    def _safe_block(self, screen: Screen, plan: dict, sel: dict) -> str:
        """Safe mode: ask Jev whether a state-changing tap is actually required by the task; block it if not."""
        if plan["action"] not in ("tap", "long_press") or sel.get("how") == "not-found":
            return ""
        el = screen.by_id(sel.get("element", ""))
        label = sel.get("label") or plan["target"]
        text = " ".join(filter(None, [label, el.desc if el else "", el.rid if el else ""]))
        risky = (el is not None and el.checkable) or (RISKY.search(text) and not SAFE_NAV.search(text))
        if not risky:
            return ""
        p = self.jev.ask(
            {"task": self.task, "control": sel.get("described") or label, "why_agent_taps_it": plan["thought"]},
            {"needed": {"type": "noul",
                        "instructions": "Is tapping `control` required by the `task` - the task names this control "
                                        "or its effect, or the task cannot be completed without it?",
                        "criteria": {"true": "The task asks for this, or it is a necessary step toward the task.",
                                     "false": "It is exploration or an unrelated change: deleting data, changing a "
                                              "setting, switching a view/layout/sort/filter mode, buying, sending."}}},
        )["needed"]["noul"]
        sel["safe_check"] = round(p, 3)
        if p >= 0.5:
            return ""
        self.blocked_taps.append(f"step {len(self.steps) + 1}: {label} (p_needed={p:.2f})")
        return (f"blocked by safe mode: '{label}' changes app state and the task does not require it. "
                "Find another way (scroll up to the top, ⋮ menus, back) instead of exploring with it.")

    # ------------------------------------------------------------------ verification
    def verify(self, screen: Screen, plan: dict) -> float:
        state = {"task": self.task, "actions_taken": self._history(),
                 # values noted on earlier screens (long scenarios compare data the final screen no longer shows)
                 "notes_from_earlier_steps": self.observations[-30:],
                 "foreground_package": screen.package,
                 "screen_elements": {e.id: e.describe() for e in screen.elements},
                 "agent_claim": plan["observation"] or plan["thought"]}
        images = None
        if screen.screenshot:  # web content and visuals are often missing from the accessibility tree
            ref = new_image_ref("screen")
            state["screenshot"] = ref
            images = {ref: downscale_jpeg(screen.screenshot, VERIFY_IMAGE_SIDE)[0]}
        # Two phrasings of the same judgment, averaged: a single Noul swung between 0.22 and 0.65 on near-identical
        # screens, and the two questions err in different directions.
        answers = self.jev.ask(state, {
            "done": {
                "type": "noul",
                "instructions": "Judging from `screen_elements`, `screenshot`, `actions_taken` and `notes_from_earlier_steps`, has the `task` been "
                                "fully carried out (its goal is achieved / the requested check was performed)?",
                "criteria": {"true": "The current screen and actions show the task is complete.",
                             "false": "Something the task requires is still missing or contradicted."}},
            "missing": {
                "type": "noul",
                "instructions": "Is any part of the `task` (an action it asks for, or a value it asks to check or "
                                "report) still not done or not shown by `screen_elements`, `screenshot`, `notes_from_earlier_steps` and "
                                "`actions_taken`?"},
        }, images=images)
        return (answers["done"]["noul"] + (1 - answers["missing"]["noul"])) / 2

    # ------------------------------------------------------------------ actions
    def act(self, screen: Screen, plan: dict, sel: dict) -> str:
        a = plan["action"]
        if a in TARGETED:
            if sel.get("how") == "not-found":
                return f"target not found on screen: {plan['target']}"
            x, y = sel["x"], sel["y"]
            if a == "tap":
                self.d.tap(x, y)
            elif a == "long_press":
                self.d.long_press(x, y)
            else:
                el = screen.by_id(sel.get("element", ""))
                if not (el and el.focused):
                    self.d.tap(x, y)
                    time.sleep(0.25)
                if plan["clear_first"]:
                    self.d.clear_focused_field()
                self.d.type_text(plan["text"])
            return "ok"
        if a == "scroll":
            direction = plan["direction"] if plan["direction"] != "none" else "down"
            el = screen.by_id(plan["element_hint"])
            region = el.bounds if el and el.scrollable else None
            self.d.scroll(direction, region)
            sel.update(direction=direction, bounds=region)
            return "ok"
        if a in ("back", "home", "enter"):
            self.d.key({"back": 4, "home": 3, "enter": 66}[a])
            return "ok"
        if a == "launch":
            self.d.launch(plan["text"] or self.app or "")
            time.sleep(1.0)
            return "ok"
        if a == "look":
            self.look_next = True
            return "screenshot will be attached next turn"
        if a == "wait":
            try:
                time.sleep(min(10.0, float(plan["text"] or 1)))
            except ValueError:
                time.sleep(1.0)
            return "ok"
        return "no-op"

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict:
        t0 = time.time()
        since = self.d.device_time()
        not_ready = self.d.ensure_ready()
        if not_ready:
            self.log(f"[0] {not_ready}")
            return {"task": self.task, "app": self.app, "device": self.d.serial, "status": "failed",
                    "failure_kind": "blocked", "verifier_p_done": None, "summary": not_ready,
                    "observations": [not_ready], "steps": [], "crash_log": "", "safe_mode_blocked": [],
                    "app_state_changes": None, "final_screen": None, "elapsed_s": round(time.time() - t0, 1),
                    "planner_usage": self.planner.usage_report()}
        if self.restart_app and self.app:
            self.d.force_stop(self.app)
        if self.app:
            self.d.launch(self.app)
            time.sleep(1.2)
        packages = None if self.app else self.d.third_party_packages()
        status, verify_rejections, prev_sig, same_count = "max_steps", 0, None, 0
        final_screen: Screen | None = None
        verified = None
        failure_pushback, failure_kind = False, ""
        state_before = app_state_snapshot(self.d, self.app) if self.app else None

        for n in range(1, self.max_steps + 1):
            tm: dict = {}
            t = time.time()
            screen = self.d.observe(with_screenshot=True)
            tm["observe"] = round(time.time() - t, 2)
            final_screen = screen
            if self.save_screens and self.run_dir and screen.screenshot:
                (self.run_dir / f"step{n:02d}{_ext(screen.screenshot)}").write_bytes(screen.screenshot)

            sig = (screen.package, tuple(e.describe() for e in screen.elements), thumbnail_signature(screen.screenshot))
            same_count = same_count + 1 if sig == prev_sig else 0
            if self.steps and same_count:
                self.steps[-1].result += " (screen unchanged)"
            prev_sig = sig

            t = time.time()
            try:
                plan, usage = self.plan(screen, packages if n == 1 else None)
            except Exception as ex:  # refusal, oversize prompt, API failure: end the run with a clear reason
                self.observations.append(f"step {n}: planner error: {ex}")
                status = "failed"
                self.log(f"[{n}] planner error: {ex}")
                break
            tm["plan"] = round(time.time() - t, 2)
            step = StepLog(n=n, package=screen.package, plan=plan, timings=tm,
                           thumb=downscale_jpeg(screen.screenshot, 800, 75)[0] if screen.screenshot else None)
            if plan["observation"]:
                self.observations.append(f"step {n}: {plan['observation']}")
            self.feedback = ""
            self.log(f"[{n}] {plan['status']} {plan['action']} {plan['target']!r} {plan['text']!r} - {plan['thought']}")

            if plan["status"] == "done":
                t = time.time()
                verified = self.verify(screen, plan)
                tm["verify"] = round(time.time() - t, 2)
                step.result = f"verifier p(done)={verified:.2f}"
                self.steps.append(step)
                self.log(f"    verify p={verified:.2f}")
                if verified >= VERIFY_THRESHOLD or verify_rejections >= 1:
                    if verified >= STRONG_PASS:
                        status = "passed"
                    elif verified >= VERIFY_THRESHOLD:
                        status = "passed_weak"  # near the line: the agent should look at the report image
                    else:
                        status = "unverified"
                    break
                verify_rejections += 1
                self.feedback = (f"An independent verifier is NOT convinced the task is complete (p={verified:.2f}). "
                                 "Re-check the screen; continue if something is missing, or declare done again "
                                 "only if the screen clearly proves it.")
                continue
            if plan["status"] == "failed":
                missing = self._unsearched() if plan["failure_kind"] in ("not_found", "feature_missing") else ""
                if missing and not failure_pushback:
                    # "I couldn't see it" is not "it doesn't exist": make it search before giving up, once.
                    failure_pushback = True
                    step.result = f"failure rejected: not searched yet ({missing})"
                    self.steps.append(step)
                    self.feedback = (f"Do not give up yet: you have not {missing}. Collapsed toolbars and overflow "
                                     "menus often hide what you need. Search, then decide.")
                    continue
                step.result = f"planner declared failure ({plan['failure_kind'] or 'unspecified'})"
                self.steps.append(step)
                status, failure_kind = "failed", plan["failure_kind"]
                break

            if plan["action"] in TARGETED:
                t = time.time()
                step.selection = self.select(screen, plan)
                tm["select"] = round(time.time() - t, 2)
                self.log(f"    select {step.selection.get('how')} -> {step.selection.get('element') or ''} "
                         f"{step.selection.get('described', '')[:90]}")
            t = time.time()
            try:
                blocked = self._safe_block(screen, plan, step.selection) if self.safe else ""
                step.result = blocked or self.act(screen, plan, step.selection)
            except Exception as ex:  # surface device errors to the planner instead of aborting
                step.result = f"error: {ex}"
            tm["act"] = round(time.time() - t, 2)
            if step.result != "ok":
                self.feedback = f"Previous action result: {step.result}"
            self.steps.append(step)
            loop = self._loop_warning()
            if loop:
                self.feedback = (self.feedback + " " if self.feedback else "") + loop
            time.sleep(0.3)

        if status == "max_steps":
            final_screen = self.d.observe(with_screenshot=True)
        state_changes = None
        if state_before is not None:
            state_changes = diff_app_state(state_before, app_state_snapshot(self.d, self.app))
        crash = ""
        try:
            crash = self.d.crash_log(since)
        except Exception:
            pass
        if crash and status == "passed":
            status = "passed_with_crash_log"

        report = {
            "task": self.task,
            "app": self.app,
            "device": self.d.serial,
            "status": status,
            "failure_kind": failure_kind or None,
            "verifier_p_done": None if verified is None else round(verified, 3),
            "safe_mode_blocked": self.blocked_taps,
            # None: app data not readable (release build without root); [] : nothing changed
            "app_state_changes": state_changes,
            "summary": self.steps[-1].plan["observation"] or self.steps[-1].plan["thought"] if self.steps else "",
            "observations": self.observations,
            "steps": [{"n": s.n, "package": s.package, "purpose": s.plan["purpose"],
                       "action": s.plan["action"], "target": s.plan["target"],
                       "text": s.plan["text"], "thought": s.plan["thought"], "selection": s.selection,
                       "result": s.result, "timings": s.timings} for s in self.steps],
            "crash_log": crash[-4000:],
            "final_screen": {"package": final_screen.package,
                             "elements": [e.describe() for e in final_screen.elements]} if final_screen else None,
            "elapsed_s": round(time.time() - t0, 1),
            "planner_usage": self.planner.usage_report(),
        }
        if self.run_dir:
            if final_screen and final_screen.screenshot:
                final = self.run_dir / ("final" + _ext(final_screen.screenshot))
                final.write_bytes(final_screen.screenshot)
                report["final_screenshot"] = str(final)
            if self.report_image:
                from .report import render_report
                try:
                    report["report_images"] = [str(p) for p in render_report(
                        self.run_dir, self.task, status, verified, self.steps,
                        final_screen.screenshot if final_screen else None, self.d.width, self.d.height,
                        failure_kind, state_changes, self.blocked_taps, self.report_lang)]
                except Exception as ex:  # the report image is a convenience; never fail the run over it
                    report["report_image_error"] = str(ex)
            (self.run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
            report["report_path"] = str(self.run_dir / "report.json")
        return report
