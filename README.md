# 🔄 Kalikot Profile Switcher — Multi-Account Codex Manager

**Switch between multiple OpenAI Codex accounts with one click — without the
dreaded *"session has ended"* kill — then rotate through them automatically.**

*kalikot* (Tagalog for *tinkering / fiddling with something*) is a Windows
desktop app for anyone juggling several Codex accounts. It saves each account as
a profile, switches the live login safely (stopping Codex, rescuing the rotated
token, wiping stale session state, then relaunching), shows live 5-hour & weekly
usage with countdowns, and can cycle to the next account on its own. Everything
stays on your machine, and a standalone `.exe` means there's nothing to install.

## 🛠️ Built With

- Python 3
- [Tkinter](https://docs.python.org/3/library/tkinter.html) — the GUI (ttk widgets)
- [pystray](https://pypi.org/project/pystray/) + [Pillow](https://pypi.org/project/Pillow/) — system-tray icon
- Win32 API (`ctypes`) — single-instance mutex, toast notifications, process control
- PyInstaller — standalone `.exe` packaging
- [`aisw`](https://www.npmjs.com/package/aisw) CLI — the account/credential backend
- [`codex-profiles`](https://github.com/midhunmonachan/codex-profiles) CLI — queried at runtime for live usage stats

> Switching **Claude** accounts? That's a separate app — [KaliClaude](https://github.com/kalikot26/KaliClaude-Claude-Profile-Switcher).

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

## 🔧 Setup

**Easiest — no install needed:** download the standalone **`KalikotAISW.exe`**
from [Releases](https://github.com/kalikot26/Codex-Profile-Switcher/releases/latest)
and run it. It bundles Python and every dependency.

> **Prerequisites:** the [`aisw`](https://www.npmjs.com/package/aisw) CLI (and the
> `codex` CLI for multi-session features) must be installed and on your `PATH`,
> and the Codex desktop app installed from the Microsoft Store.

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
- Live usage stats are read via the external [`codex-profiles`](https://github.com/midhunmonachan/codex-profiles)
  CLI (MIT © Midhun Monachan) — install it to see the 5H/weekly numbers; switching
  itself only needs `aisw`.

## 👨‍💻 Author

**John Venice Almazan** — [@kalikot26](https://github.com/kalikot26)
