# Notifier & Tray Update Notes

## Files changed

| File | Change |
|------|--------|
| `gui/app.py` | Added `_resource_path()` helper; replaced broken `PROJECT_ROOT/ICON_PATH`; added `SetCurrentProcessExplicitAppUserModelID` in `main()` |
| `gui/build.bat` | Switched from bare `pyinstaller` to `python -m PyInstaller`; combined dep install into one step |
| `KalikotProfileSwitcher.spec` | Updated `datas` to include `app.ico` (was stale `[]`) |
| `gui/notifier.py` | **New** — state tracking, persistence, Windows WinRT toast |
| `gui/app.py` | Added tray support, in-app toast, notifier wiring, 5H alert checkbox, next-reset label |
| `gui/build.bat` | Install pystray + Pillow before build; pass `--collect-submodules pystray` |
| `gui/requirements-gui.txt` | **New** — documents optional GUI deps |
| `gui/app.py` | Added single-instance guard: named mutex + localhost IPC restore signal |

---

## How 5H reset notifications work

1. **On every Refresh** (`_store_usage` inside `refresh()`), the app extracts  
   `five_hour.reset_at` (a unix timestamp) from the CLI's JSON output and passes  
   it to `ResetNotifier.update_profile()`.

2. The notifier stores a per-profile record in  
   `~/.kalikot-profile-switcher/notifier_state.json`.

3. A `root.after(60_000, _check_and_notify)` callback fires on the main thread  
   every **60 seconds** while the app is running.  
   It calls `notifier.get_due()` — profiles whose `reset_at ≤ now` and  
   `five_hour_notified == False` — and fires a notification for each.

4. After firing, `notifier.mark_notified(profile_id)` sets `five_hour_notified = True`  
   and writes the state file. The same reset event will **never fire again** unless  
   Refresh returns a new future `reset_at` that differs by > 5 minutes.

5. The first `_check_and_notify` call fires 500 ms after app start, so any reset  
   that happened while the app was closed is caught immediately on next launch.

---

## Where notifier state is stored

```
~/.kalikot-profile-switcher/notifier_state.json
```

Example:
```json
{
  "enabled": true,
  "profiles": {
    "user@example.com-team": {
      "profile_id": "user@example.com-team",
      "label": "Work",
      "email": "user@example.com",
      "five_hour_reset_at": 1716999600,
      "five_hour_notified": true,
      "five_hour_notified_at": 1716999665.3
    }
  }
}
```

---

## How duplicate notifications are prevented

- Once a notification fires, `five_hour_notified = true` is persisted.  
- The `get_due()` query skips entries with `five_hour_notified == true`.  
- Only when a **new future** `reset_at` arrives that differs by > 300 s from the  
  stored one does the flag reset — because that means a fresh 5-hour cycle started.

---

## What works while the app is minimised to the tray

- `root.after()` callbacks continue firing → the 60-second checker keeps running.  
- **Windows native toast** (`send_windows_toast` via PowerShell WinRT) appears in  
  the Action Center and as a banner, regardless of whether the app window is visible.  
- **In-app floating overlay** (`_show_in_app_toast`) creates a `tk.Toplevel` with  
  `wm_overrideredirect(True)` and `-topmost True`.  
  On Windows, Toplevel windows appear even when the root is withdrawn.

### Tray behaviour (requires `pystray` + `Pillow`)

| Action | Result |
|--------|--------|
| Click **×** (close button) | Hides to tray — app keeps running |
| Double-click tray icon | Restores window |
| Right-click → Show Kalikot | Restores window |
| Right-click → Quit | Saves notifier state and exits |

If `pystray`/`Pillow` are **not** installed, clicking **×** quits the app normally  
(no tray), but Windows native toasts still work for any session where the app  
is running.

---

## What happens if the app is fully killed / exits

- In-process timers stop — no further checks until the app is reopened.  
- On next launch, `_check_and_notify` fires within 500 ms of startup and  
  immediately notifies for any reset that passed while the app was closed.  
- Notifier state is saved to disk on quit (and after every Refresh), so no  
  timer knowledge is lost.

### Future enhancement (not implemented)

For notifications after a **full process exit** (without tray running), a  
Windows Task Scheduler job could periodically run a small headless helper script  
that reads `notifier_state.json` and calls `send_windows_toast` directly.  
This is safe to add later without changing the app architecture.

---

## UI additions

- **"5H Reset Alerts" checkbox** in the Actions panel (below Stop Codex).  
  Toggles `notifier.enabled` and persists the preference.  
- **Next-reset countdown** — small grey line just above the status bar, e.g.:  
  `⏰  Next 5H reset: 'Work' in ~42 min (at 20:05)`  
  Hidden when alerts are off or no upcoming reset is tracked.

---

## Checks run

| Check | Result |
|-------|--------|
| `notifier.py` — `ast.parse` syntax check | ✅ OK |
| `app.py` — `ast.parse` syntax check (UTF-8) | ✅ OK |
| `import notifier; ResetNotifier()` live import | ✅ OK |
| `send_windows_toast` callable | ✅ OK |

---

## How to test the notifier manually

**Quick test — trigger a notification in ~1 minute:**

1. Open `~/.kalikot-profile-switcher/notifier_state.json`.  
2. For one profile, set `five_hour_reset_at` to `<current unix timestamp + 60>`  
   and `five_hour_notified` to `false`.  
3. Save the file and wait; the next 60-second check will fire the notification.

**Or use the Python shell:**
```python
# From the gui/ directory
import time
from notifier import ResetNotifier
n = ResetNotifier()
pid = list(n.entries.keys())[0]   # first profile
n.entries[pid].five_hour_reset_at = int(time.time()) + 30
n.entries[pid].five_hour_notified = False
n.save_state()
# Restart the app — notification fires within ~60 s
```

**Test Windows toast directly:**
```python
from notifier import send_windows_toast
send_windows_toast("Test", "This is a test toast from Kalikot.")
```

---

## Anti-Spam / Idempotency Behavior

### How duplicate notifier entries are prevented

`notifier_state.json` stores profiles under a `dict` keyed by `profile_id`
(the stable Codex identifier, e.g. `"user@example.com-team"`).
`update_profile()` always does a **lookup-then-update-in-place** — it is
structurally impossible for the same `profile_id` to produce two entries.
Clicking Refresh 50 times produces exactly one entry per profile.

### How the same `reset_at` is handled

If `update_profile()` is called repeatedly with an identical `reset_at`:

```
abs(new_ts - old_ts) = abs(0) = 0 — NOT > NEW_CYCLE_THRESHOLD (300 s)
→ five_hour_notified is never touched
→ no disk write (dirty flag stays False after the first call)
```

Result: the entry is confirmed/refreshed in memory, the notified flag is
untouched, and no unnecessary disk I/O occurs.

### How a genuinely new `reset_at` is handled

A new reset cycle is detected when **all** of the following are true:

| Condition | Reason |
|-----------|--------|
| `new_ts is not None` | ignore missing data |
| `old_ts is not None` | need something to compare |
| `new_ts > now` | only future timestamps count as a new cycle |
| `abs(new_ts - old_ts) > 300 s` | filters out minor API jitter (±30 s) |

When a new cycle is detected: `five_hour_notified` is reset to `False` and the
new `reset_at` is stored.  The next `get_due()` call will include this profile
and fire one fresh notification.

### How already-notified events are handled

`get_due()` filters with `and not e.five_hour_notified`.
Once `mark_notified()` flips that flag to `True`, the entry is **permanently
excluded** from future `get_due()` calls unless a new cycle is detected (see
above).

Even if `_check_and_notify()` runs 1000 times, it will never fire again for the
same event.

### How to manually verify by clicking Refresh many times

1. Open the app and click **Refresh** 10 times quickly.
2. Inspect `~/.kalikot-profile-switcher/notifier_state.json`.
3. Confirm each profile appears **exactly once** under `"profiles"`.
4. Confirm `five_hour_notified` is `false` (not yet due) or `true` (already
   notified) — but never duplicated.

### Automated test coverage

Run `python gui/test_notifier.py` (uses an isolated temp file, never touches
real state):

| Test | Scenario |
|------|----------|
| 1 | Repeated Refresh with same `reset_at` → single entry, notified unchanged |
| 2 | New future `reset_at` → notified cleared for fresh cycle |
| 3 | Past `reset_at` after notification → notified stays True |
| 4 | Past `reset_at` with ±30 s jitter → notified stays True |
| 5 | `get_due()` + `mark_notified()` → fires exactly once, empty on all retries |
| 6 | Multiple profiles → each tracked independently |
| 7 | Profile deletion → entry removed from memory and disk |
| 8 | `None` reset_at → no due entry, no crash |

### Profile rename limitation

When a profile label is renamed, `update_profile()` receives the new label and
stores it on the existing entry (matched by the stable `profile_id`).  No
duplicate is created.  The next-reset countdown label will show the updated name
on the next Refresh or periodic check.

The `profile_id` itself (e.g. `"user@example.com-team"`) never changes on
rename, so notifier state survives renames without any special handling.

---

## App Icon Fix

### Why the icon regressed

The original `ICON_PATH` was computed as:

```python
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = PROJECT_ROOT / "app.ico"
```

This works when running from source because `__file__` = `gui/app.py` and
`parent.parent` = project root.

In a PyInstaller `--onefile` EXE, all scripts are extracted to a temporary
directory (`sys._MEIPASS`).  `__file__` becomes `<_MEIPASS>/app.py`, so
`parent.parent` resolves to the **parent of the temp dir** — a path that
never contains `app.ico`.  The `if ICON_PATH.exists()` guard silently falls
through and both the window titlebar and the tray icon show the default
Tkinter feather icon.

### Fix: `_resource_path()` helper

```python
def _resource_path(name: str) -> Path:
    if hasattr(sys, "_MEIPASS"):          # running from PyInstaller EXE
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent.parent / name   # running from source

ICON_PATH = _resource_path("app.ico")
```

`app.ico` is bundled into the EXE via `--add-data "app.ico;."` in `build.bat`,
so `<_MEIPASS>/app.ico` is a valid file path while the EXE is running.

### Icon coverage

| Surface | Mechanism | Source run | Built EXE |
|---------|-----------|-----------|-----------|
| EXE file (Explorer/taskbar pin) | `--icon "app.ico"` in PyInstaller | n/a | ✅ embedded |
| Window titlebar | `root.iconbitmap(str(ICON_PATH))` | ✅ | ✅ (after fix) |
| Tray icon | `_PILImage.open(str(ICON_PATH))` | ✅ | ✅ (after fix) |
| Taskbar (source run) | `SetCurrentProcessExplicitAppUserModelID` | ✅ | ✅ inherits EXE icon |

### AppUserModelID

Added in `main()` before the Tk window is created:

```python
if os.name == "nt":
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "Kalikot.ProfileSwitcher"
    )
```

This tells Windows to group the process under a stable identity so the
correct icon appears in the taskbar and Alt-Tab even when launched via
`python.exe` rather than the built EXE.  Wrapped in `try/except` — safe
to fail silently on any Windows version that doesn't support the call.

### Build command

`build.bat` was changed from bare `pyinstaller` (not always in PATH) to:

```bat
python -m PyInstaller --noconfirm --clean --onefile --windowed \
    --name KalikotProfileSwitcher \
    --icon "app.ico" \
    --add-data "app.ico;." \
    --collect-submodules pystray \
    --hidden-import pystray._win32 \
    "gui\app.py"
```

`python -m PyInstaller` works as long as Python is in PATH, regardless of
whether the `pyinstaller` script shim is on PATH.

### Where the rebuilt EXE lives

```
C:\Projects\Codex-Profile-Switcher\dist\KalikotProfileSwitcher.exe
```

---

## Single-Instance / Restore Existing Window

### Problem

Without a guard, launching the EXE a second time while the app is in the tray
creates a **duplicate tray icon** and a second hidden `Tk` window.  Both instances
share no state and the user has no way to know which icon belongs to which.

### Solution: Named Mutex + localhost IPC

Two mechanisms work together:

| Mechanism | Role |
|-----------|------|
| Windows named mutex `Local\KalikotProfileSwitcherSingleInstance` | Determines whether *this* launch is the first or a duplicate |
| TCP socket on `127.0.0.1:47321` | Lets the second launch signal the first to restore its window |

### Startup flow

```
main()
  │
  ├─ _try_acquire_mutex()
  │     ├─ CreateMutexW(..., bInitialOwner=True, "Local\KalikotProfileSwitcher...")
  │     ├─ GetLastError() == ERROR_ALREADY_EXISTS?
  │     │     └─ YES → return None   (we are the second instance)
  │     └─ NO  → return handle int   (we are the first instance)
  │
  ├─ mutex_handle is None?  (second instance)
  │     ├─ _send_show_signal()   →  connects to 127.0.0.1:47321, sends b"SHOW_WINDOW"
  │     └─ sys.exit(0)           →  no window created, no tray icon
  │
  └─ mutex_handle is int?  (first instance)
        └─ ProfileSwitcherApp(root, mutex_handle=handle)
              └─ _start_ipc_listener()  →  background thread accepting on :47321
```

### What `_start_ipc_listener()` does

- Binds `127.0.0.1:47321` with `SO_REUSEADDR` (so a quick restart doesn't hit
  "address already in use").
- Sets a 1-second `settimeout` so the loop checks `_ipc_stop` once per second.
- On each accepted connection: reads up to 64 bytes; if payload is `b"SHOW_WINDOW"`
  calls `self.root.after(0, self._show_from_tray)` — all Tkinter work stays on the
  main thread.
- Stops cleanly when `_ipc_stop` is set (see `_quit_app`).
- If the port is unavailable it exits silently — the app still works, it just won't
  auto-restore on a second launch attempt.

### Window restore (`_show_from_tray`)

```python
def _show_from_tray(self) -> None:
    self.root.deiconify()
    self.root.attributes("-topmost", True)   # flash topmost to force foreground
    self.root.lift()
    self.root.focus_force()
    self.root.after(100, lambda: self.root.attributes("-topmost", False))
```

The 100 ms `-topmost` flash is the most reliable way to bring a hidden window to
the foreground on Windows when another application currently owns focus.

### Cleanup on quit (`_quit_app`)

1. `self._ipc_stop.set()` — signals the listener thread's loop to exit.
2. `CloseHandle(self._mutex_handle)` — releases the named mutex so the *next*
   launch can claim it immediately (no leftover handle from the dead process).

### Security

- The IPC socket binds to `127.0.0.1` only — not `0.0.0.0`, not a named pipe
  accessible across the network.
- The only accepted payload is the exact bytes `b"SHOW_WINDOW"` (64-byte read
  limit).  Any other data is silently dropped.
- No user data, tokens, or secrets pass through the socket.

### Non-Windows behaviour

`_try_acquire_mutex()` returns `None` on non-Windows.  `main()` only calls
`sys.exit(0)` when both `mutex_handle is None` **and** `os.name == "nt"`.  On
macOS/Linux the app starts normally (no mutex, no IPC listener, no single-instance
restriction).

### Files changed

| File | Change |
|------|--------|
| `gui/app.py` | `import socket`; constants `SINGLE_INSTANCE_MUTEX`, `IPC_HOST`, `IPC_PORT`, `IPC_TIMEOUT`; `_try_acquire_mutex()`; `_send_show_signal()`; `__init__` stores `_mutex_handle` + `_ipc_stop`, calls `_start_ipc_listener()`; `_show_from_tray` topmost flash; `_start_ipc_listener()` method; `_quit_app` stops IPC + closes handle; `main()` single-instance check |
