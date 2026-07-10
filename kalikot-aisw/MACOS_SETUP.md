# KalikotAISW on macOS

The switching engine (`aisw`) is fully cross-platform, so only this GUI needed a
port. It now runs on Windows **and** macOS from one `gui/app.py`. This guide is
written so a Codex/AI agent (or you) can finish setup on the Mac.

## 1. Install prerequisites

```bash
# the switching engine
brew tap burakdede/tap && brew install aisw

# python (if not already present)
brew install python

# the Codex CLI you want to switch between (if used)
#   e.g. npm i -g @openai/codex   (or however Codex CLI is installed on this Mac)
```

## 2. Confirm two macOS values (IMPORTANT)

The desktop-app control (Stop / Launch / Switch) needs the Mac app's **process
name** and **bundle id**. These were set to best-guess defaults on Windows and
must be verified here. Run:

```bash
# process name — look at the NAME column for the Codex/ChatGPT desktop app
pgrep -l -i chatgpt ; pgrep -l -i codex

# bundle id — try whichever app name is correct
osascript -e 'id of app "ChatGPT"'
osascript -e 'id of app "Codex"'
```

Then open `gui/app.py`, find the block near the top marked **`Platform (Windows +
macOS)`**, and set these to what you found (only if they differ from the defaults):

```python
MACOS_APP_NAME      = "ChatGPT"           # the app's display name (for `open -a`)
MACOS_APP_BUNDLE_ID = "com.openai.chat"   # from `osascript -e 'id of app ...'`
MACOS_PROC_NAME     = "ChatGPT"           # the NAME from `pgrep -l`
```

## 3. Build

```bash
bash build_macos.sh
```

Produces `dist/KalikotAISW.app`.

## 4. Run

```bash
open dist/KalikotAISW.app
```

First launch: the build is unsigned, so **right-click the app → Open** once to get
past Gatekeeper (or `xattr -dr com.apple.quarantine dist/KalikotAISW.app`).

## What works vs. what to verify

Cross-platform already (same code paths, engine-driven):
- Profile list / add / **Switch** / status — all via the `aisw` CLI, same
  `~/.codex/auth.json` and `~/.aisw` paths as Windows.
- **Stop / Launch** the desktop app → `pkill`/`open` (once step 2 is confirmed).
- **Launch CLI session** → opens Terminal with an isolated `CODEX_HOME`.
- Reset notifications → native macOS notification via `osascript`.

Verify on the Mac (couldn't be tested from Windows):
- **Does the Mac desktop app read `~/.codex/auth.json`?** The engine writes there;
  if this Mac app keeps its login elsewhere, *CLI* switching still works but the
  *desktop-app* login may not follow a switch. (Same caveat we hit on Windows.)
- Single-instance guard is Windows-only; on macOS you can open two windows. Minor.
- The tray/menu-bar icon uses `pystray` — works on macOS but behaves slightly
  differently; it degrades gracefully if unavailable.

## Notes for the AI agent finishing this

- All OS-specific logic is guarded by `IS_WIN` / `IS_MAC` in `gui/app.py`; the
  Windows paths are untouched. If a macOS command behaves differently, the
  functions to adjust are `_is_codex_running`, `_kill_codex`, `_launch_desktop_app`,
  `_open_path`, `_running_pids`, and the `IS_MAC` branch of `_launch_cli_session`.
- If the desktop app's login store isn't `~/.codex/auth.json`, that's an engine
  (`aisw`) concern, not this GUI — check `aisw` docs / `aisw status`.
