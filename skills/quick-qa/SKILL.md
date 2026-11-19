---
name: quick-qa
description: Fast Android UI QA executor. Give it one natural-language instruction ("open app X, do Y, check Z") and it drives a real Android device over adb on its own with a planner (Claude Sonnet 5.5) and an element selector/verifier (TypeSafe Jev), then returns a pass/fail report with a one-page report image, observations, app-state changes, crash log and a final screenshot. Use this instead of driving adb tap-by-tap yourself whenever a QA step involves navigating or operating an Android app UI.
---

# quick-qa

Delegate UI operation to a fast loop instead of spending your own turns on `adb shell input tap`.
You decide **what** to test; quick-qa does the clicking at ~3–4 s per step.

## Run

```bash
python "${CLAUDE_PLUGIN_ROOT}/qa.py" "<task>" --app <package>
```

First time only, if that fails with a missing module, install the dependencies:
`python -m pip install "${CLAUDE_PLUGIN_ROOT}"` (this also adds a `quickqa` command you can use instead).
It needs `adb` on PATH (or `$ADB`), a device listed by `adb devices`, and the environment variables
`ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY`. If a key is missing, tell the user; do not ask them to paste it.

- `<task>`: one concrete, checkable instruction. Include the expected result so it can be verified.
  Good: `"Add a todo named 'milk', then check it appears at the top of the list"`.
  Bad: `"test the app"` (too vague: split it into several runs).
- `--app <package>`: the app to test (package or `package/.Activity`). By default it is force-stopped and launched
  fresh, so every run starts from the app's first screen.
  - `--start resume`: no force-stop. The app is brought to the front on the screen it was left on, by the previous
    run or by you. Use it to split a long flow into short runs (set up state → open screen → check) or to start
    on a screen you navigated to yourself.
  - Without `--app` the run starts from whatever is on screen.
  - If the run falls out of the app to the home screen (for example one back too many), it stops with
    `FAILURE KIND: left_app` instead of relaunching, because relaunching would reset what the run set up.
- `--stop-when "<text>"`: end the run the moment that text is on screen, before any further tap. Status
  `stopped` (exit 0). Use it for "open the sheet and press nothing" checks next to dangerous buttons; add
  `--shot` to keep the evidence. The text must be in the UI tree (not only in a picture).
- `--shot <path>`: save the final screen as a full-resolution lossless PNG at that path (named evidence shots).
- `--pre "<cmd>"` / `--post "<cmd>"`: shell commands run on this PC before and after the run, for example to
  start and stop a capture. A failing `--pre` aborts the run; `--post` always runs. Both are listed as `HOOK` lines
  and in `report.json`.
- Safe mode is on by default: a tap that looks state-changing (delete, settings, view/sort/filter modes, toggles)
  is checked against the task and blocked when the task does not need it. `--no-safe` turns this off.
- `--device <serial>`: adb serial or host name. Default `$QUICKQA_DEVICE`, else the only connected device
  (with several devices connected, you must choose one). The first output line is `DEVICE: <serial>`: check it.
- `--vision always`: also send screenshots to the planner (needed for purely visual checks such as colors, images or layout).
  The default `auto` sends them when the UI tree cannot describe the screen (WebView pages, canvas, photos) or
  when the planner asks with its `look` action. Images are downscaled (planner 512px, `--image-side` to change).
- `--effort none|low|medium|high`: planner effort, default `medium` (`low` lost its way in long multi-page flows).
- `--max-input-tokens N` (default 100000): per-request planner prompt ceiling; prompts are shrunk to stay under it.
- `--out DIR`: where runs are written. Default is outside any repo: `$QUICKQA_OUT`, else
  `%LOCALAPPDATA%\quickqa\runs` on Windows or `~/.cache/quickqa/runs` elsewhere.
- `--report-lang <language>` for the report image texts (default `$QUICKQA_REPORT_LANG`, else English; the
  fixed labels exist in English and Korean), `--no-report-image` to skip the image.
- `--max-steps N` (default 25), `--save-screens` (a screenshot for every step), `--json` (full report on stdout), `-q`.

Run it in the background when a flow may take more than ~1 minute.

## Read the result

The exit code and the `STATUS` line:

| status | exit | meaning |
|---|---|---|
| `passed` | 0 | Planner finished and the independent Jev verifier agreed (p_done >= 0.7) |
| `passed_weak` | 0 | Verifier only just agreed (0.5–0.7): look at the report image before trusting it |
| `passed_with_crash_log` | 0 | Passed, but `logcat -b crash` has entries since the start; inspect them |
| `stopped` | 0 | The `--stop-when` text appeared; nothing was pressed after that |
| `unverified` | 3 | Planner said done twice but the verifier disagreed; look at the report image yourself |
| `failed` | 1 | See `FAILURE KIND` below |
| `max_steps` | 2 | Ran out of steps. `verifier p_done` still says how far it got: high means the task was probably done but not declared |

`FAILURE KIND`:
- `not_found`: searched and could not find it (not proof it is missing).
- `feature_missing`: the app positively shows it is absent.
- `expectation_mismatch`: the app works but shows something else; `SUMMARY` starts with "found instead: ...".
  Often the expectation's wording (which element holds the text) needs adjusting rather than the app.
- `cannot_perform`: quick-qa cannot do what was asked (a gesture it has no action for, an effect that never
  shows on screen, a check only possible through an action the task forbids). Do that part with adb yourself.
- `app_error`: a crash, an error message or a frozen app was seen.
- `blocked`: login wall, missing permission, PIN lock, or a failed `--pre` hook.
- `left_app`: the run fell out to the home screen; earlier steps' state may be gone.

**Start with `REPORT IMAGE`** (Read it): one page of numbered cards, each "purpose / operation /
screenshot with the tap point or scroll arrow marked", and a final card with the result and the values found.
That single image usually tells you what happened without opening anything else. Runs with more than
12 cards produce `report_1.png`, `report_2.png`, and so on.

Other lines:
- `SUMMARY` / `OBS`: what was seen (values, error dialogs, wrong text). Counts and positions come from the UI tree.
- `UNCONFIRMED`: parts of the task the run could not confirm on screen (for example a chip whose selected state
  the app does not expose). Check those yourself from the report image.
- `APP STATE CHANGES`: SharedPreferences keys and data files the run changed (debuggable builds only;
  release builds print nothing). Check it when the app has saved settings such as a view mode.
- `SAFE MODE BLOCKED`: taps the run wanted to make but safe mode refused.
- `PLANNER`: calls, tokens and estimated cost of the run.
- `REPORT`: `report.json` with every step (action, chosen element, how it was chosen, timings) and the final
  screen's element list. `FINAL SCREENSHOT` for visual confirmation.

## Guidance

- One behavior per run. Put exact strings and values in the task (`"type 'qa@test.com'"`, `"the total should be 408"`).
- Search queries are typed literally: on a device set to another language, ask for the term in that language
  if the app searches by label.
- Non-ASCII typing (Korean, Japanese, ...) requires the ADBKeyBoard IME on the device; otherwise the `type` step
  reports an error.
- One run at a time per device: two runs on the same device fight over the screen.
- Timed gestures: "double-tap 'Shift'", "tap X 3 times quickly", "long-press X for 2 seconds" run as one precise
  gesture. Say the timing in the task.

## Limits

- Effects that never appear on screen (clipboard contents, sound, vibration, what another device received) cannot
  be confirmed; they come back as `UNCONFIRMED` or `cannot_perform`.
- Selected/checked state is read from the UI tree. When the app does not expose it, the run judges from whether the
  tapped element visibly changed, and lists what stays uncertain under `UNCONFIRMED`.
- Text that exists only in an image or canvas cannot trigger `--stop-when`.
- Each step takes 3-5 s, so a check like "the label appears within 1 s" cannot be timed by the run itself;
  `report.json` has each step's `settle` time (seconds until the screen stopped changing) as a rough guide.
- quick-qa does not judge product intent: a `failed` or odd OBS is a lead for you to investigate, not a final bug verdict.
