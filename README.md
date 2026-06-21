# 🔄 Kalikot Profile Switcher — Multi-Account Codex Manager

**Switch between multiple OpenAI Codex accounts with one click — without the
dreaded *"session has ended"* kill — then rotate through them automatically.**

*kalikot* (Tagalog for *tinkering / fiddling with something*) is a Windows
desktop app for anyone juggling several Codex accounts. It saves each account as
a profile, switches the live login safely (stopping Codex, rescuing the rotated
token, wiping stale session state, then relaunching), shows live 5-hour & weekly
usage with countdowns, and can cycle to the next account on its own. Everything
stays on your machine, and a standalone `.exe` means there's nothing to install.

### What this project adds

The account-switching engine is the third-party [`aisw`](https://github.com/burakdede/aisw)
CLI (MIT, by burakdede) — all credit for the underlying switching/credential
storage goes there. **This project is the GUI layer on top of it**, and the
original work here is:

- 🖼️ **A real GUI** — a Windows desktop app around the `aisw` CLI, instead of typing commands.
- 🛡️ **Session-kill fix on the GUI** — stops Codex, rescues the rotated token, and
  wipes stale heartbeat state in the right order so switching never triggers the
  `app_session_terminated` / "session has ended" kill.
- 🔁 **Clean switching, not session stacking** — each switch cleanly closes the
  prior session before activating the next, instead of piling up live sessions
  that knock each other out.
- 💾 **History persistence** — per-profile notes, email/plan metadata, cached
  usage, and a token-rotation activity log persist across runs (under
  `~/.kalikot-profile-switcher`).

> The same `aisw` engine powers the sibling app for Claude —
> [KaliClaude](https://github.com/kalikot26/KaliClaude-Claude-Profile-Switcher).

## 🛠️ Built With

- Python 3
- [Tkinter](https://docs.python.org/3/library/tkinter.html) — the GUI (ttk widgets)
- [pystray](https://pypi.org/project/pystray/) + [Pillow](https://pypi.org/project/Pillow/) — system-tray icon
- Win32 API (`ctypes`) — single-instance mutex, toast notifications, process control
- PyInstaller — standalone `.exe` packaging
- **[`aisw`](https://github.com/burakdede/aisw)** by burakdede — the account-switching engine (Rust, MIT) · **required**
- [`codex-profiles`](https://github.com/midhunmonachan/codex-profiles) by Midhun Monachan — *optional*, queried at runtime for the live usage bars

## ✨ Features

- 👥 **Profile list** with at-a-glance status — active marker (●), where each
  account is live (Codex app / CLI session / ⚠ needs re-login), a free-text note,
  and a maskable email column.
- 🔀 **Switch Profile** — the *safe* switch: stop Codex → rescue the outgoing
  account's freshly-rotated token → wipe stale heartbeat threads → switch
  credentials → repair `config.toml` → relaunch.
- 🔁 **Switch to Next** — cycles to the next account in the list **automatically,
  no prompts**: *fully close Codex → switch → relaunch*, with built-in settle
  delays. Great for rotating accounts to spread out usage.
- 📊 **Live usage bars** — 5-hour and weekly quota remaining with countdowns
  ("resets in ~2h 15m"). The active account shows live numbers; others show the
  last cached values (querying a non-active account would rotate and risk killing
  its token).
- 🔔 **5H reset notifications** — native Windows toasts when an account's 5-hour
  limit is likely back, plus an in-app banner.
- ▶️ **Launch / Stop Codex** with live running-state detection.
- 🆕 **Prepare New Login** — safely clears the active login (with backup) so you
  can sign into a fresh account, then **Save Current As…** captures it.
- 🧩 **Multi-session CLI launcher** — open isolated Codex CLI sessions, each in
  its own `CODEX_HOME`, so several accounts run side-by-side with zero conflicts.
- 🩺 **Session Health** window — per-profile token expiry, account ID, refresh
  fingerprint, and a live token-rotation activity log.
- 💾 **Portable** — the standalone `dist\KalikotAISW.exe` needs no Python; it
  minimizes to the tray and enforces a single running instance.

## 🧠 How it avoids session kills

OpenAI Codex uses **OAuth refresh-token rotation**: every token use issues a new
refresh token and invalidates the old one. Switching accounts by hand triggers
fatal errors — `refresh token already used`, or `app_session_terminated` when
Codex tries to reconnect the *previous* account's heartbeat threads under the
*new* account's token.

The safe switch sequence:

```python
_kill_codex(); wait_for_stop()                              # 1. fully stop Codex
run_aisw(["add", "codex", outgoing, "--from-live", "--yes"]) # 2. rescue rotated token
_clear_all_heartbeats()                                      # 3. wipe stale session state
run_aisw(["use", "codex", new_profile])                      # 4. switch credentials
_sanitize_codex_config()                                     # 5. repair config.toml
```

Two rules are inviolable: **never mutate Codex global state while Codex is
running** (it forces a re-auth and kills the session), and **never make a live
API call for a non-active account** (it rotates that account's token and desyncs
the stored copy).

## ✅ Requirements

| Requirement | Needed for | How to get it |
|-------------|-----------|---------------|
| **[`aisw`](https://github.com/burakdede/aisw) CLI** on your `PATH` | **Everything** — listing & switching accounts. The app does nothing without it. | [Installing aisw](#installing-aisw) ↓ |
| **Codex desktop app** (Microsoft Store) | The account you switch + Launch / Stop Codex | Microsoft Store |
| `codex-profiles` CLI — *optional* | Live 5H / weekly **usage bars** (active profile) | `cargo install codex-profiles` |
| `codex` CLI — *optional* | The **multi-session CLI launcher** | Your Codex CLI install |

### Installing aisw

KalikotAISW drives the third-party [`aisw`](https://github.com/burakdede/aisw) CLI
(Rust, MIT — by burakdede); it must be installed and on your `PATH`.

**Windows** (this app is Windows-only):

1. Download **`aisw-x86_64-pc-windows-msvc.exe`** from the [aisw releases](https://github.com/burakdede/aisw/releases/latest).
2. Rename it to **`aisw.exe`** and put it in a folder that's on your `PATH`.
3. Run `aisw init` once to set up its integration.

*Or*, if you have Rust installed: `cargo install aisw`.

Verify with `aisw --version` (this app is built against aisw 0.3.x).

## 🔧 Setup

**Easiest — no install needed:** download the standalone **`KalikotAISW.exe`**
from [Releases](https://github.com/kalikot26/Codex-Profile-Switcher/releases/latest)
and run it. It bundles Python and every dependency — but you still need the
[`aisw` CLI](#installing-aisw) and the Codex desktop app (see Requirements above).

**To run from source** (Python 3 required):

```bash
cd kalikot-aisw/gui
python app.py
```

**To build the `.exe` yourself:**

```bash
cd kalikot-aisw/gui
build.bat            # installs pyinstaller/pystray/Pillow, outputs dist\KalikotAISW.exe
```

## 🚀 Usage

1. Launch the app — your saved profiles appear with the active one marked ●.
2. **Save Current As…** captures whatever account is currently logged into Codex
   as a new profile.
3. Select a profile and **Switch Profile** to make it active (it stops Codex,
   switches safely, and offers to relaunch).
4. Click **Switch to Next** to auto-rotate to the following account in the list —
   close, switch, relaunch, hands-free.
5. Watch the **5H / Weekly** bars to see remaining quota, and let the tray toasts
   tell you when a limit resets.

## 📝 Notes

- **Windows only** — it relies on the Microsoft Store Codex app, Windows toast
  notifications, and Windows process management.
- A profile marked **⚠ Re-login** has a dead stored token (rotated elsewhere) —
  use *Prepare New Login*, sign in to that account, then *Save Current As…* with
  the same name.
- **Privacy:** everything stays on your machine. The app copies local files and
  talks to local CLIs only; it never uploads your `auth.json` or tokens. The
  single-instance IPC socket binds to `127.0.0.1` exclusively.
- **Credits:** account switching is powered by [`aisw`](https://github.com/burakdede/aisw)
  (MIT © burakdede) — *required*. The optional live 5H/weekly usage readout uses
  [`codex-profiles`](https://github.com/midhunmonachan/codex-profiles) (MIT © Midhun
  Monachan). Neither is bundled or forked — both stay on your machine as separate
  CLIs; switching itself only needs `aisw`.

## 👨‍💻 Author

**John Venice Almazan** — [@kalikot26](https://github.com/kalikot26)
