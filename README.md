# quick-qa-helper

Hand an Android UI QA task to a fast loop instead of driving `adb shell input tap` one turn at a time.
A coding agent (or you) gives one instruction, such as "open app X, do Y, check Z". quick-qa then drives a real
device on its own and returns a pass/fail report plus **one report image** that shows every step.

```
agent / you  ──▶  quickqa "<task>" --app <package>
                     │
   ┌─────────────────┴───────────────────────────────────────────────────┐
   │ loop (≈3–4 s per step)                                              │
   │  1. observe  QQServer on the device (UiAutomation, kept alive) 0.25 s│
   │  2. plan     Claude Haiku 5.5 picks the next action          2–3 s  │
   │  3. select   TypeSafe Jev picks the element (Box on screenshot      │
   │              when the UI tree has no node for it)          0.2–0.5 s│
   │  4. act      adb input                                       0.2 s  │
   │  "done"? → an independent Jev verifier must agree                   │
   └─────────────────────────────────────────────────────────────────────┘
```

## Requirements

- Python 3.10+
- `adb` on `PATH` (Android SDK platform-tools), or set `$ADB` to its path
- An Android device (Android 8.0+) with USB or wireless debugging on, listed by `adb devices`
- API keys in the environment:
  - `ANTHROPIC_API_KEY`: [Claude API](https://platform.claude.com) key for the planner
  - `TYPESAFE_API_KEY`: [TypeSafe](https://docs.typesafe.ai) key for element selection and verification

Screenshots and the screen's text are sent to both APIs while a run is in progress. Don't point quick-qa at
screens holding data you are not allowed to share with them.

## Install

### As a Claude Code plugin (skill)

```
/plugin marketplace add ouor/quick-qa-helper
/plugin install quick-qa@quick-qa-helper
```

The skill (`skills/quick-qa/SKILL.md`) tells the agent when and how to run quick-qa. On first use it installs
the Python dependencies with `python -m pip install "${CLAUDE_PLUGIN_ROOT}"`, or you can run that yourself.

### As a command-line tool

```bash
pip install git+https://github.com/ouor/quick-qa-helper
quickqa --help
```

From a clone without installing the package: `pip install anthropic requests Pillow`, then `python qa.py --help`.

## Use

```bash
quickqa "Calculate 12 × 34 using the buttons and check that the result is 408" --app com.android.calculator2
```

Write one concrete, checkable behavior per run, and include the expected result.

| option | default | |
|---|---|---|
| `--app PKG` | none | App to test (`package` or `package/.Activity`). Force-stopped and launched fresh. |
| `--start restart\|resume` | `restart` | `resume` keeps the state the previous run left. |
| `--device SERIAL` | `$QUICKQA_DEVICE`, else the only connected device | adb serial, or the host name of a wireless serial (`host:port`). |
| `--no-safe` | safe mode on | Safe mode blocks state-changing taps (delete, settings, view/sort/filter modes, toggles) that the task does not need. |
| `--effort none\|low\|medium\|high` | `medium` | Planner effort. `low` loses its way in long multi-page flows. |
| `--vision auto\|always\|never` | `auto` | Send screenshots to the planner. `auto` sends one when the UI tree cannot describe the screen (WebView, canvas, photo) or when the planner asks. |
| `--max-steps N` | 25 | |
| `--max-input-tokens N` | 100000 | Per-request planner prompt ceiling. Prompts are shrunk to stay under it. |
| `--report-lang LANG` | `$QUICKQA_REPORT_LANG`, else `English` | Language of the report image. Fixed labels exist in English and Korean; the planner writes step purposes in any language. |
| `--out DIR` | `$QUICKQA_OUT`, else `%LOCALAPPDATA%\quickqa\runs` / `~/.cache/quickqa/runs` | Kept outside your repo on purpose. |
| `--save-screens`, `--json`, `-q`, `--no-report-image`, `--image-side PX` | | |

### Output

```
STATUS: passed  (verifier p_done=0.97, 6 steps, 10.4s)
SUMMARY: ...
PLANNER: claude-haiku-5-5 6 calls, in 14k / out 2k tokens (peak prompt 2.6k), ~$0.003
REPORT IMAGE: .../report.png
REPORT: .../report.json
FINAL SCREENSHOT: .../final.jpg
```

| status | exit | meaning |
|---|---|---|
| `passed` | 0 | Planner finished and the verifier agreed (p ≥ 0.7). |
| `passed_weak` | 0 | The verifier only just agreed (0.5–0.7). Look at the report image. |
| `passed_with_crash_log` | 0 | Passed, but `logcat -b crash` has new entries. |
| `unverified` | 3 | The planner said done twice and the verifier disagreed. |
| `failed` | 1 | `FAILURE KIND`: `not_found` (searched, could not find it), `feature_missing` (the app shows it is absent), `app_error`, `blocked`. |
| `max_steps` | 2 | Ran out of steps. |

`report.png` is a one-page summary built for an agent to read. It holds numbered cards, each with the step's
purpose, the operation, and the screenshot with the tap point or scroll arrow marked, and ends with a result
card that lists the values found. Runs with more than 12 cards produce `report_1.png`, `report_2.png`, and so on.
For debuggable builds the run also lists the SharedPreferences keys and data files it changed
(`APP STATE CHANGES`).

## How it works

- **QQServer** (`quickqa/server/QQServer.java`, prebuilt as `qqserver.jar`) is a small `app_process` that
  keeps one UiAutomation connection open. A UI dump plus a screenshot takes about 0.25 s instead of
  about 2.3 s for `uiautomator dump` plus `screencap`. It is pushed to `/data/local/tmp` automatically and exits
  when the run ends. Set `QUICKQA_NO_SERVER=1` to use plain `uiautomator dump`.
- **Planner** (`claude-haiku-5-5`) sees the task, recent actions, notes from earlier steps, and the screen as
  a list of elements. It answers with one action as structured JSON. Prompts are 2–8K tokens, and a 30-step
  run costs about $0.01–0.04. `--planner cerebras --planner-model ...` uses Cerebras instead
  (`CEREBRAS_API_KEY`).
- **Selector** (TypeSafe Jev) turns the planner's description of a target into a concrete element (Choice
  plus a Noul "is it there"). When the tree has no such node, a Box question on the screenshot locates it.
- **Verifier** (two Jev Noul phrasings, averaged) checks a "done" claim against the screen and the notes.
- **Guards**:
  - A warning fires when the planner repeats one move 4 times in 6 steps.
  - A "not found" failure is sent back once if the planner has not yet scrolled to the top and gone back.
  - Safe mode runs a Jev check before a state-changing tap.

## Limits

- Before a run, quick-qa wakes the screen and dismisses a lock screen that has no PIN. A PIN, pattern or
  password lock stops the run at once (`failed`, `blocked`); unlock the device yourself, because quick-qa never
  enters credentials.

- Typing non-ASCII text (Korean, Japanese, ...) needs the [ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard)
  IME on the device.
- Run one task at a time per device. Two runs on one device fight over the screen, and UiAutomation allows
  only one client.
- Search terms are typed literally. On a device set to another language, ask for the term in that language.
- Shopping or heavily dynamic screens (e.g. Play Store search results with sponsored cards) succeed less often.

## Tests

`tests/suite.py` runs scenarios against stock apps and prints a scoreboard. The package names are those of
AOSP / LineageOS builds, so swap them for your vendor's apps.

```bash
python tests/suite.py --set basic            # short single-screen checks
python tests/suite.py --set complex          # multi-app, multi-stage scenarios (creates and deletes a contact / alarm)
```

## Rebuilding QQServer

Only needed after editing the Java. It needs a JDK and `$ANDROID_HOME` with a platform and build-tools.

```bash
bash quickqa/server/build.sh
```
