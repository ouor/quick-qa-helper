---
name: quick-qa
description: Fast Android UI QA executor. Give it one natural-language instruction ("open app X, do Y, check Z") and it drives a real Android device over adb on its own with a planner (Claude Haiku 5.5) and an element selector/verifier (TypeSafe Jev), then returns a pass/fail report with a one-page report image, observations, app-state changes, crash log and a final screenshot. Use this instead of driving adb tap-by-tap yourself whenever a QA step involves navigating or operating an Android app UI.
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
  fresh, so every run starts from the app's first screen. `--start resume` keeps the state the previous run left
  (use it only when chaining runs on purpose). Without `--app` the run starts from whatever is on screen.
- Safe mode is on by default: a tap that looks state-changing (delete, settings, view/sort/filter modes, toggles)
  is checked against the task and blocked when the task does not need it. `--no-safe` turns this off.
- `--device <serial>`: adb serial or host name. Default `$QUICKQA_DEVICE`, else the only connected device
  (with several devices connected, you must choose one).
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
| `unverified` | 3 | Planner said done twice but the verifier disagreed; look at the report image yourself |
| `failed` | 1 | See `FAILURE KIND`: `not_found` (searched and could not find it - not proof it is missing), `feature_missing` (the app positively shows it is absent), `app_error`, `blocked` |
| `max_steps` | 2 | Ran out of steps; usually a vague task or an app bug that loops |

**Start with `REPORT IMAGE`** (Read it): one page of numbered cards, each "purpose / operation /
screenshot with the tap point or scroll arrow marked", and a final card with the result and the values found.
That single image usually tells you what happened without opening anything else. Runs with more than
12 cards produce `report_1.png`, `report_2.png`, and so on.

Other lines:
- `SUMMARY` / `OBS`: what was seen (values, error dialogs, wrong text). Counts and positions come from the UI tree.
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
- quick-qa does not judge product intent: a `failed` or odd OBS is a lead for you to investigate, not a final bug verdict.
