"""Kalikot Profile Switcher - Tkinter GUI wrapping the codex-profiles CLI."""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from notifier import ResetNotifier, send_windows_toast

# pystray + Pillow are optional — tray degrades gracefully if not installed.
try:
    import pystray as _pystray
    from PIL import Image as _PILImage
    _TRAY_AVAILABLE = True
except ImportError:
    _pystray = None   # type: ignore[assignment]
    _PILImage = None  # type: ignore[assignment]
    _TRAY_AVAILABLE = False


APP_TITLE = "Kalikot Profile Switcher"


def _resource_path(name: str) -> Path:
    """Resolve a resource file that works both from source and from a PyInstaller EXE.

    PyInstaller --onefile extracts bundled files to sys._MEIPASS at runtime.
    __file__ inside the EXE lives inside that same temp dir, so the old
    'parent.parent' trick resolves to the *parent of the temp dir* — wrong.
    This helper detects the EXE case and uses _MEIPASS directly.

    From source:   <repo-root>/app.ico  (gui/../app.ico)
    From EXE:      <_MEIPASS>/app.ico   (bundled via --add-data)
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    # Source: file is in gui/, resource is one level up in the project root
    return Path(__file__).resolve().parent.parent / name


ICON_PATH = _resource_path("app.ico")

CACHE_DIR = Path.home() / ".kalikot-profile-switcher"
CACHE_FILE = CACHE_DIR / "usage-cache.json"

CODEX_HOME = Path.home() / ".codex"
CODEX_AUTH = CODEX_HOME / "auth.json"
CODEX_GLOBAL_STATE = CODEX_HOME / ".codex-global-state.json"

# Single-instance guard
SINGLE_INSTANCE_MUTEX = "Local\\KalikotProfileSwitcherSingleInstance"
IPC_HOST = "127.0.0.1"
IPC_PORT = 47321
IPC_TIMEOUT = 1.0


def _try_acquire_mutex() -> Optional[int]:
    """Attempt to create a named Windows mutex to claim the single-instance slot.

    Returns the mutex handle (> 0) when this is the FIRST instance.
    Returns None when another instance already holds the mutex (second launch),
    or when not on Windows.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX)
        last_err = ctypes.windll.kernel32.GetLastError()
        if last_err == 183:  # ERROR_ALREADY_EXISTS — another instance owns the mutex
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            return None
        return int(handle) if handle else None
    except Exception:
        return None


def _send_show_signal() -> bool:
    """Send a SHOW_WINDOW signal to the already-running instance over localhost TCP.

    Returns True if the signal was delivered, False otherwise.
    """
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=IPC_TIMEOUT) as s:
            s.sendall(b"SHOW_WINDOW")
        return True
    except OSError:
        return False


def find_cli() -> list[str]:
    """Locate the codex-profiles CLI. Returns the argv prefix."""
    # Prefer a real .exe / .cmd shim so we don't need shell=True.
    candidates = ["codex-profiles.exe", "codex-profiles.cmd", "codex-profiles.bat", "codex-profiles"]
    for name in candidates:
        found = shutil.which(name)
        if found:
            return [found]
    # Last resort: rely on shell to resolve (PowerShell shim, etc.)
    return ["codex-profiles"]


CLI_PREFIX = find_cli()

CODEX_PROCESS_NAME = "Codex"


def _find_codex_family_name() -> Optional[str]:
    """Return the PackageFamilyName for the Codex Store app, or None if not found."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage -Name 'OpenAI.Codex').PackageFamilyName"],
            capture_output=True, text=True, creationflags=0x08000000, timeout=10,
        )
        name = result.stdout.strip()
        return name if name else None
    except Exception:
        return None


def _is_codex_app_running() -> bool:
    """Return True if the Codex GUI process is currently running."""
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {CODEX_PROCESS_NAME}.exe", "/NH"],
            capture_output=True, text=True, creationflags=0x08000000, timeout=5,
        )
        return f"{CODEX_PROCESS_NAME}.exe" in r.stdout
    except Exception:
        return False


def _clean_codex_state() -> None:
    """Clear stuck heartbeat-thread references from Codex's global state file.

    When Codex is killed mid-session it leaves thread IDs in
    'heartbeat-thread-permissions-by-id'.  On every subsequent launch Codex
    tries to reconnect those threads to the backend, triggering token-refresh
    calls.  With multiple instances this causes 'refresh token already used'
    errors.  Clearing the map before launching gives Codex a clean slate.

    Safe to call even when the file doesn't exist or is malformed — all errors
    are swallowed silently.
    """
    if not CODEX_GLOBAL_STATE.exists():
        return
    try:
        state = json.loads(CODEX_GLOBAL_STATE.read_text(encoding="utf-8"))
        atom = state.get("electron-persisted-atom-state")
        if isinstance(atom, dict) and atom.get("heartbeat-thread-permissions-by-id"):
            atom["heartbeat-thread-permissions-by-id"] = {}
            CODEX_GLOBAL_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
            # Keep the .bak in sync
            bak = CODEX_GLOBAL_STATE.with_suffix(".json.bak")
            try:
                bak.write_text(json.dumps(state, indent=2), encoding="utf-8")
            except OSError:
                pass
    except Exception:
        pass  # never crash the caller over a housekeeping step


def run_cli(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run codex-profiles with args. Returns (returncode, stdout, stderr)."""
    cmd = CLI_PREFIX + args
    creationflags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW so spawned CLI does not flash a console.
        creationflags = 0x08000000
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except FileNotFoundError:
        return -1, "", f"codex-profiles CLI not found in PATH (tried {CLI_PREFIX[0]})."
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s: {' '.join(args)}"
    except Exception as exc:  # pragma: no cover
        return -1, "", f"Error running CLI: {exc}"


# ---------------------------------------------------------------------------
# Usage cache
# ---------------------------------------------------------------------------


def load_cache() -> dict[str, Any]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if len(local) <= 4:
        return local[0] + "*" * max(0, len(local) - 1) + "@" + domain
    return local[0] + ("*" * (len(local) - 4)) + local[-3:] + "@" + domain


def fmt_reset(reset_at: Optional[int], include_date: bool) -> str:
    if not reset_at:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(int(reset_at))
    except (OSError, ValueError, OverflowError):
        return "unknown"
    if include_date:
        return dt.strftime("%H:%M on %-d %b") if sys.platform != "win32" else dt.strftime("%H:%M on %#d %b")
    return dt.strftime("%H:%M")


def fmt_relative(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return f"{int(delta // 86400)} d ago"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    id: str
    label: Optional[str]
    email: Optional[str]
    plan: Optional[str]
    is_current: bool
    is_saved: bool
    is_api_key: bool
    error: Optional[str]

    @property
    def display_name(self) -> str:
        return self.label or self.id

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Profile":
        return cls(
            id=raw.get("id", ""),
            label=raw.get("label"),
            email=raw.get("email"),
            plan=raw.get("plan"),
            is_current=bool(raw.get("is_current")),
            is_saved=bool(raw.get("is_saved")),
            is_api_key=bool(raw.get("is_api_key")),
            error=raw.get("error"),
        )


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class ProfileSwitcherApp:
    def __init__(self, root: tk.Tk, mutex_handle: int = -1) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("700x600")
        self.root.minsize(640, 540)

        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass

        self.profiles: list[Profile] = []
        self.cache: dict[str, Any] = load_cache()
        self.hide_emails = tk.BooleanVar(value=False)
        self.active_profile_text = tk.StringVar(value="-")
        self.codex_state_text = tk.StringVar(value="-")
        self.codex_app_status_text = tk.StringVar(value="-")
        self.next_reset_var = tk.StringVar(value="")
        self.busy = False
        self.result_queue: queue.Queue = queue.Queue()
        self.codex_family_name: Optional[str] = None

        # Single-instance: hold onto the mutex handle so we can close it on exit
        self._mutex_handle: int = mutex_handle
        # Event to stop the IPC listener thread cleanly
        self._ipc_stop = threading.Event()

        # Reset notifier — load persisted state immediately
        self.notifier = ResetNotifier()
        self.notify_enabled = tk.BooleanVar(value=self.notifier.enabled)
        self.notify_enabled.trace_add("write", self._on_notify_toggle)

        self._tray: Any = None

        self._build_ui()
        self._setup_tray()
        self._start_ipc_listener()
        self.root.after(50, self._poll_queue)
        threading.Thread(target=self._resolve_codex_app, daemon=True).start()
        # First notifier check fires after UI is ready; also catches any missed resets
        self.root.after(500, self._check_and_notify)
        # Clean stale Codex heartbeat threads immediately on startup, then every 5 min.
        # This prevents the "refresh token already used" storm that happens when multiple
        # dead heartbeat threads all try to renew the OAuth token at the same time.
        self.root.after(1000, self._periodic_codex_cleanup)
        self.refresh()

    # ----- UI layout -----

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista" if os.name == "nt" else style.theme_use())
        except tk.TclError:
            pass

        # Title
        title = ttk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 18, "bold"))
        title.pack(anchor="w", padx=14, pady=(12, 6))

        # Status box
        status_frame = ttk.LabelFrame(self.root, text="")
        status_frame.pack(fill="x", padx=14, pady=(0, 10))

        inner = ttk.Frame(status_frame)
        inner.pack(fill="x", padx=10, pady=10)

        left_status = ttk.Frame(inner)
        left_status.pack(side="left", expand=True, fill="x")
        ttk.Label(left_status, text="Active Profile", foreground="#666").pack(anchor="w")
        ttk.Label(left_status, textvariable=self.active_profile_text, font=("Segoe UI", 11, "bold")).pack(anchor="w")

        mid_status = ttk.Frame(inner)
        mid_status.pack(side="left", expand=True, fill="x")
        ttk.Label(mid_status, text="Codex State", foreground="#666").pack(anchor="w")
        self.codex_state_label = ttk.Label(mid_status, textvariable=self.codex_state_text, font=("Segoe UI", 11, "bold"))
        self.codex_state_label.pack(anchor="w")

        right_status = ttk.Frame(inner)
        right_status.pack(side="right", expand=True, fill="x")
        ttk.Label(right_status, text="App", foreground="#666").pack(anchor="w")
        self.app_status_label = ttk.Label(right_status, textvariable=self.codex_app_status_text, font=("Segoe UI", 11, "bold"))
        self.app_status_label.pack(anchor="w")

        # Middle area: profiles list + actions
        middle = ttk.Frame(self.root)
        middle.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # Profiles list
        list_frame = ttk.Frame(middle)
        list_frame.pack(side="left", fill="both", expand=True)
        ttk.Label(list_frame, text="Profiles").pack(anchor="w")

        list_container = ttk.Frame(list_frame)
        list_container.pack(fill="both", expand=True, pady=(2, 4))

        self.listbox = tk.Listbox(list_container, exportselection=False, activestyle="dotbox")
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        ttk.Checkbutton(list_frame, text="Hide emails", variable=self.hide_emails, command=self._render_list).pack(anchor="w")

        # Action buttons
        actions = ttk.Frame(middle)
        actions.pack(side="right", fill="y", padx=(12, 0))
        ttk.Label(actions, text="Actions").pack(anchor="w")

        btn_opts = {"width": 18}
        self.btn_switch = ttk.Button(actions, text="Switch Profile", command=self.switch_profile, **btn_opts)
        self.btn_switch.pack(pady=4, fill="x")
        self.btn_prepare = ttk.Button(actions, text="Prepare New Login", command=self.prepare_new_login, **btn_opts)
        self.btn_prepare.pack(pady=4, fill="x")
        self.btn_save = ttk.Button(actions, text="Save Active Login", command=self.save_active_login, **btn_opts)
        self.btn_save.pack(pady=4, fill="x")

        rd_frame = ttk.Frame(actions)
        rd_frame.pack(pady=4, fill="x")
        self.btn_rename = ttk.Button(rd_frame, text="Rename", command=self.rename_profile, width=8)
        self.btn_rename.pack(side="left", expand=True, fill="x", padx=(0, 2))
        self.btn_delete = ttk.Button(rd_frame, text="Delete", command=self.delete_profile, width=8)
        self.btn_delete.pack(side="right", expand=True, fill="x", padx=(2, 0))

        self.btn_refresh = ttk.Button(actions, text="Refresh", command=self.refresh, **btn_opts)
        self.btn_refresh.pack(pady=(12, 4), fill="x")

        ttk.Separator(actions, orient="horizontal").pack(fill="x", pady=(8, 4))
        ttk.Label(actions, text="Codex App", foreground="#666").pack(anchor="w")
        self.btn_launch = ttk.Button(actions, text="Launch Codex", command=self.launch_codex_app, **btn_opts)
        self.btn_launch.pack(pady=4, fill="x")
        self.btn_stop = ttk.Button(actions, text="Stop Codex", command=self.stop_codex_app, **btn_opts)
        self.btn_stop.pack(pady=4, fill="x")

        ttk.Separator(actions, orient="horizontal").pack(fill="x", pady=(8, 4))
        ttk.Checkbutton(
            actions, text="5H Reset Alerts", variable=self.notify_enabled,
        ).pack(anchor="w", pady=2)

        # Usage bottom
        usage_frame = ttk.LabelFrame(self.root, text="Usage")
        usage_frame.pack(fill="x", padx=14, pady=(0, 6))

        self.five_h_text = tk.StringVar(value="5h Limit: -")
        self.weekly_text = tk.StringVar(value="Weekly Limit: -")
        self.usage_meta_text = tk.StringVar(value="")

        ttk.Label(usage_frame, textvariable=self.five_h_text).pack(anchor="w", padx=8, pady=(6, 0))
        self.bar_five_h = ttk.Progressbar(usage_frame, mode="determinate", maximum=100, length=200)
        self.bar_five_h.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Label(usage_frame, textvariable=self.weekly_text).pack(anchor="w", padx=8)
        self.bar_weekly = ttk.Progressbar(usage_frame, mode="determinate", maximum=100, length=200)
        self.bar_weekly.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Label(usage_frame, textvariable=self.usage_meta_text, foreground="#666").pack(anchor="w", padx=8, pady=(0, 6))

        # Status bar (pinned to very bottom first so next-reset label stacks above it)
        self.status_var = tk.StringVar(value="Ready.")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken")
        status_bar.pack(fill="x", side="bottom")

        # Next-reset countdown — appears just above the status bar
        ttk.Label(
            self.root, textvariable=self.next_reset_var,
            anchor="w", foreground="#555", font=("Segoe UI", 8),
        ).pack(fill="x", side="bottom", padx=6)

    # ----- threading helpers -----

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "!disabled"
        for btn in (
            self.btn_switch,
            self.btn_prepare,
            self.btn_save,
            self.btn_rename,
            self.btn_delete,
            self.btn_refresh,
        ):
            try:
                btn.state([state])
            except tk.TclError:
                pass
        try:
            if busy:
                self.btn_launch.state(["disabled"])
                self.btn_stop.state(["disabled"])
            else:
                self._check_codex_app_state()
        except (tk.TclError, AttributeError):
            pass
        if message:
            self.status_var.set(message)
        self.root.update_idletasks()

    def _run_async(self, fn: Callable[[], Any], on_done: Callable[[Any], None], busy_msg: str = "Working...") -> None:
        if self.busy:
            return
        self._set_busy(True, busy_msg)

        def worker() -> None:
            try:
                result = fn()
                self.result_queue.put(("ok", result, on_done))
            except Exception as exc:  # pragma: no cover
                self.result_queue.put(("err", exc, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload, on_done = self.result_queue.get_nowait()
                self._set_busy(False, "Ready.")
                if kind == "ok":
                    try:
                        on_done(payload)
                    except Exception as exc:  # pragma: no cover
                        messagebox.showerror(APP_TITLE, f"Unhandled error: {exc}")
                else:
                    messagebox.showerror(APP_TITLE, f"Error: {payload}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # ----- selection / rendering -----

    def _on_select(self, _event: Any = None) -> None:
        self._render_usage()

    def _selected_profile(self) -> Optional[Profile]:
        sel = self.listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        if idx >= len(self.profiles):
            return None
        return self.profiles[idx]

    def _selected_index_for_id(self, profile_id: str) -> Optional[int]:
        for i, p in enumerate(self.profiles):
            if p.id == profile_id:
                return i
        return None

    def _render_list(self) -> None:
        prev = self._selected_profile()
        prev_id = prev.id if prev else None
        self.listbox.delete(0, tk.END)
        for p in self.profiles:
            name = p.label or "(no label)"
            email = mask_email(p.email or "") if self.hide_emails.get() else (p.email or "")
            status = "Active account" if p.is_current else ("Saved login" if p.is_saved else "Unknown")
            row = f"{name} - {email} - {status}" if email else f"{name} - {status}"
            self.listbox.insert(tk.END, row)
        # Restore selection
        if prev_id is not None:
            idx = self._selected_index_for_id(prev_id)
            if idx is not None:
                self.listbox.selection_set(idx)
                self.listbox.see(idx)
        # Active profile text
        active = next((p for p in self.profiles if p.is_current), None)
        if active:
            email_disp = mask_email(active.email or "") if self.hide_emails.get() else (active.email or "")
            self.active_profile_text.set(active.label or email_disp or active.id)
        else:
            self.active_profile_text.set("(none)")
        self._render_usage()

    def _render_usage(self) -> None:
        sel = self._selected_profile()
        if not sel:
            self.five_h_text.set("5h Limit: select a profile")
            self.weekly_text.set("Weekly Limit: select a profile")
            self.usage_meta_text.set("")
            self.bar_five_h["value"] = 0
            self.bar_weekly["value"] = 0
            return

        entry = self.cache.get(sel.id)
        if not entry:
            self.five_h_text.set("5h Limit: no cached data yet — click Refresh")
            self.weekly_text.set("Weekly Limit: no cached data yet")
            self.usage_meta_text.set("")
            self.bar_five_h["value"] = 0
            self.bar_weekly["value"] = 0
            return

        ts = entry.get("refreshed_at")
        meta_bits = []
        if ts:
            meta_bits.append(f"Last refreshed {fmt_relative(ts)}")

        state = entry.get("state")
        if state and state != "ok":
            self.five_h_text.set(f"5h Limit: ({state})")
            self.weekly_text.set(f"Weekly Limit: ({state})")
            self.bar_five_h["value"] = 0
            self.bar_weekly["value"] = 0
            err = entry.get("error")
            if err:
                meta_bits.append(f"Error: {err}")
            self.usage_meta_text.set(" | ".join(meta_bits))
            return

        five = entry.get("five_hour") or {}
        weekly = entry.get("weekly") or {}

        if five:
            pct = five.get("left_percent")
            reset = fmt_reset(five.get("reset_at"), include_date=False)
            if pct is not None:
                self.five_h_text.set(f"5h Limit: Last known: {pct}% left - resets {reset}")
                self.bar_five_h["value"] = pct
            else:
                self.five_h_text.set("5h Limit: unknown")
                self.bar_five_h["value"] = 0
        else:
            self.five_h_text.set("5h Limit: unavailable")
            self.bar_five_h["value"] = 0

        if weekly:
            pct = weekly.get("left_percent")
            reset = fmt_reset(weekly.get("reset_at"), include_date=True)
            if pct is not None:
                self.weekly_text.set(f"Weekly Limit: Last known: {pct}% left - resets {reset}")
                self.bar_weekly["value"] = pct
            else:
                self.weekly_text.set("Weekly Limit: unknown")
                self.bar_weekly["value"] = 0
        else:
            self.weekly_text.set("Weekly Limit: unavailable")
            self.bar_weekly["value"] = 0

        self.usage_meta_text.set(" | ".join(meta_bits))

    # ----- actions -----

    def refresh(self) -> None:
        """Refresh the profile list, and fetch usage only for the selected profile.

        Never fetches usage for all profiles at once — doing so would fire
        simultaneous token-refresh calls for every account, which is what causes
        the 'app_session_terminated' / 'refresh token already used' errors.
        """
        selected_id = None
        sel = self._selected_profile()
        if sel:
            selected_id = sel.id

        def work() -> dict[str, Any]:
            # Step 1 — always list all profiles (local read, no token refresh)
            rc, out, err = run_cli(["list", "--json"])
            if rc != 0:
                raise RuntimeError(err or out or "list failed")
            list_data = json.loads(out)

            # Step 2 — fetch usage only for the ONE selected profile
            usage_map: dict[str, dict[str, Any]] = {}
            if selected_id:
                rc2, out2, err2 = run_cli(["status", "--id", selected_id, "--json"])
                if rc2 == 0 and out2.strip():
                    try:
                        data = json.loads(out2)
                        prof = None
                        if isinstance(data, dict):
                            if "profiles" in data and data["profiles"]:
                                prof = data["profiles"][0]
                            elif "id" in data:
                                prof = data
                        if prof:
                            usage_map[selected_id] = prof
                    except json.JSONDecodeError:
                        pass
            return {"list": list_data, "usage": usage_map}

        def done(result: dict[str, Any]) -> None:
            self.profiles = [Profile.from_json(p) for p in result["list"].get("profiles", [])]
            for pid, prof in result["usage"].items():
                self._store_usage(pid, prof)
            save_cache(self.cache)
            self._update_codex_state()
            self._render_list()
            self._update_next_reset_label()
            if selected_id and selected_id in result["usage"]:
                self.status_var.set(f"Refreshed '{selected_id}' usage + profile list.")
            else:
                self.status_var.set(f"Refreshed {len(self.profiles)} profile(s). Select one and refresh to see usage.")

        self._run_async(work, done, busy_msg="Refreshing...")

    def _store_usage(self, pid: str, prof: dict[str, Any]) -> None:
        usage = prof.get("usage") or {}
        entry: dict[str, Any] = {
            "refreshed_at": time.time(),
            "label": prof.get("label"),
            "email": prof.get("email"),
            "state": usage.get("state"),
            "error": prof.get("error"),
            "raw": prof,
        }
        five_hour = None
        weekly = None
        for bucket in usage.get("buckets", []) or []:
            if bucket.get("five_hour") and five_hour is None:
                five_hour = bucket["five_hour"]
            if bucket.get("weekly") and weekly is None:
                weekly = bucket["weekly"]
        entry["five_hour"] = five_hour
        entry["weekly"] = weekly
        self.cache[pid] = entry

        # Register / update the reset target in the notifier
        reset_at = (five_hour or {}).get("reset_at")
        self.notifier.update_profile(
            profile_id=pid,
            label=prof.get("label"),
            email=prof.get("email"),
            five_hour_reset_at=int(reset_at) if reset_at else None,
        )

    def _update_codex_state(self) -> None:
        active = next((p for p in self.profiles if p.is_current), None)
        if active and not active.is_api_key:
            self.codex_state_text.set("Codex is Logged In")
        elif active and active.is_api_key:
            self.codex_state_text.set("API key active")
        elif CODEX_AUTH.exists():
            self.codex_state_text.set("Logged in (unsaved)")
        else:
            self.codex_state_text.set("Not logged in")

    def switch_profile(self) -> None:
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile first.")
            return
        if sel.is_current:
            messagebox.showinfo(APP_TITLE, f"'{sel.display_name}' is already active.")
            return

        # Guard: switching while Codex is running causes "refresh token already used" errors
        # because Codex holds the old profile's tokens in memory while the auth file is replaced.
        stop_codex_first = False
        if _is_codex_app_running():
            answer = messagebox.askyesno(
                APP_TITLE,
                "⚠️  Codex is currently running.\n\n"
                "Switching profiles while Codex is open causes:\n"
                "  \"Your access token could not be refreshed\"\n"
                "  \"because your refresh token was already used.\"\n\n"
                "Stop Codex now, switch profiles, then relaunch manually?\n\n"
                "[Yes] = Stop Codex & Switch\n"
                "[No]  = Cancel",
                icon="warning",
            )
            if not answer:
                return
            stop_codex_first = True

        def work() -> str:
            if stop_codex_first:
                subprocess.run(
                    ["taskkill", "/F", "/IM", f"{CODEX_PROCESS_NAME}.exe", "/T"],
                    capture_output=True, creationflags=0x08000000,
                )
                time.sleep(0.8)   # let the process fully exit before auth file is replaced
            # Always clear stuck heartbeat threads so the next Codex launch
            # doesn't trigger a token-refresh storm on startup.
            _clean_codex_state()
            rc, out, err = run_cli(["load", "--id", sel.id, "--force"])
            if rc != 0:
                raise RuntimeError(err or out or "load failed")
            return out

        def done(_: str) -> None:
            self.refresh()
            if stop_codex_first and self.codex_family_name:
                relaunch = messagebox.askyesno(
                    APP_TITLE,
                    f"Switched to '{sel.display_name}'.\n\n"
                    "Codex was stopped for the switch.\n"
                    "Relaunch Codex now with the new profile?",
                )
                if relaunch:
                    def _launch() -> None:
                        _clean_codex_state()
                        subprocess.Popen(
                            ["explorer.exe", f"shell:AppsFolder\\{self.codex_family_name}!App"],
                            creationflags=0x08000000,
                        )
                        time.sleep(2)
                    self._run_async(_launch, lambda _: (
                        self._check_codex_app_state(),
                        self.status_var.set(f"Codex relaunched with '{sel.display_name}'."),
                    ), busy_msg="Relaunching Codex...")
            else:
                messagebox.showinfo(APP_TITLE, f"Switched to '{sel.display_name}'.")

        self._run_async(work, done, busy_msg=f"Switching to {sel.display_name}...")

    def prepare_new_login(self) -> None:
        if not CODEX_AUTH.exists():
            messagebox.showinfo(
                APP_TITLE,
                "No active Codex login found. You're ready to run `codex login` in CMD.",
            )
            self.refresh()
            return

        active = next((p for p in self.profiles if p.is_current), None)
        warn = ""
        if active and not active.is_saved:
            warn = (
                "\n\nWARNING: the current active login does NOT appear to be saved as a profile. "
                "Click 'Save Active Login' first if you want to keep it."
            )

        confirm = messagebox.askyesno(
            APP_TITLE,
            "This will clear the active Codex login (~/.codex/auth.json) so you can "
            "log into a new account. Saved profiles are preserved." + warn + "\n\nContinue?",
        )
        if not confirm:
            return

        def work() -> str:
            # Move auth.json aside (reversible) instead of deleting outright.
            backup = CODEX_HOME / f"auth.json.bak.{int(time.time())}"
            try:
                shutil.move(str(CODEX_AUTH), str(backup))
            except OSError as exc:
                raise RuntimeError(f"Failed to move auth.json: {exc}")
            return str(backup)

        def done(backup: str) -> None:
            messagebox.showinfo(
                APP_TITLE,
                "Ready for new Codex login.\n\n"
                "Open CMD/PowerShell and run:\n    codex login\n\n"
                "After logging in, return here and click 'Save Active Login'.\n\n"
                f"(Previous auth backed up to: {backup})",
            )
            self.refresh()

        self._run_async(work, done, busy_msg="Preparing for new login...")

    def save_active_login(self) -> None:
        if not CODEX_AUTH.exists():
            messagebox.showwarning(
                APP_TITLE,
                "No active Codex login found at ~/.codex/auth.json. Run `codex login` first.",
            )
            return
        label = simpledialog.askstring(
            APP_TITLE,
            "Enter a label for this profile (leave blank for auto-label):",
            parent=self.root,
        )
        if label is None:
            return
        label = label.strip()

        def work() -> str:
            args = ["save"]
            if label:
                args += ["--label", label]
            rc, out, err = run_cli(args)
            if rc != 0:
                raise RuntimeError(err or out or "save failed")
            return out

        def done(_: str) -> None:
            messagebox.showinfo(APP_TITLE, "Active login saved.")
            self.refresh()

        self._run_async(work, done, busy_msg="Saving active login...")

    def rename_profile(self) -> None:
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile first.")
            return
        new_label = simpledialog.askstring(
            APP_TITLE,
            f"Rename '{sel.display_name}' to:",
            parent=self.root,
            initialvalue=sel.label or "",
        )
        if new_label is None:
            return
        new_label = new_label.strip()
        if not new_label:
            messagebox.showwarning(APP_TITLE, "Label cannot be empty.")
            return
        if new_label == sel.label:
            return

        def work() -> str:
            if sel.label:
                args = ["label", "rename", "--label", sel.label, "--to", new_label]
            else:
                args = ["label", "set", "--id", sel.id, "--to", new_label]
            rc, out, err = run_cli(args)
            if rc != 0:
                raise RuntimeError(err or out or "rename failed")
            return out

        def done(_: str) -> None:
            messagebox.showinfo(APP_TITLE, f"Renamed to '{new_label}'.")
            self.refresh()

        self._run_async(work, done, busy_msg="Renaming profile...")

    def _resolve_codex_app(self) -> None:
        self.codex_family_name = _find_codex_family_name()
        self.root.after(0, self._check_codex_app_state)
        self.root.after(3000, self._schedule_codex_app_check)

    def _update_codex_app_state(self, running: bool) -> None:
        self.codex_app_status_text.set("Running" if running else "Not Running")
        try:
            color = "#2e7d32" if running else "#c62828"
            self.app_status_label.configure(foreground=color)
        except (tk.TclError, AttributeError):
            pass
        if not self.busy:
            try:
                self.btn_launch.state(["disabled" if running else "!disabled"])
                self.btn_stop.state(["!disabled" if running else "disabled"])
            except (tk.TclError, AttributeError):
                pass

    def _check_codex_app_state(self) -> None:
        def check() -> None:
            running = _is_codex_app_running()
            self.root.after(0, lambda: self._update_codex_app_state(running))
        threading.Thread(target=check, daemon=True).start()

    def _schedule_codex_app_check(self) -> None:
        self._check_codex_app_state()
        self.root.after(3000, self._schedule_codex_app_check)

    def _periodic_codex_cleanup(self) -> None:
        """Run in background every 5 minutes to clear stale Codex heartbeat threads.

        Codex writes each session's thread ID into heartbeat-thread-permissions-by-id
        in .codex-global-state.json.  When a session ends (or Codex is killed) it
        leaves that entry behind.  On the next launch Codex tries to reconnect every
        stale thread, firing simultaneous OAuth token-refresh calls that trigger the
        'refresh token already used' rotation error and kill the new session.

        Only clears threads whose session file no longer exists — so any currently
        active session is left untouched.
        """
        def _cleanup() -> None:
            try:
                if not CODEX_GLOBAL_STATE.exists():
                    return
                state = json.loads(CODEX_GLOBAL_STATE.read_text(encoding="utf-8"))
                atom = state.get("electron-persisted-atom-state", {})
                heartbeats: dict = atom.get("heartbeat-thread-permissions-by-id", {})
                if not heartbeats:
                    return

                sessions_root = CODEX_HOME / "sessions"
                dead: list[str] = []
                for tid in list(heartbeats.keys()):
                    # Each session file is named rollout-<timestamp>-<thread-id>.jsonl
                    # under sessions/YYYY/MM/DD/
                    matches = list(sessions_root.rglob(f"*{tid}*.jsonl")) if sessions_root.exists() else []
                    if not matches:
                        dead.append(tid)

                if dead:
                    for tid in dead:
                        heartbeats.pop(tid, None)
                    atom["heartbeat-thread-permissions-by-id"] = heartbeats
                    CODEX_GLOBAL_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    bak = CODEX_GLOBAL_STATE.with_suffix(".json.bak")
                    try:
                        bak.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    except OSError:
                        pass
            except Exception:
                pass  # never crash on housekeeping

        threading.Thread(target=_cleanup, daemon=True).start()
        # Reschedule every 5 minutes
        self.root.after(5 * 60 * 1000, self._periodic_codex_cleanup)

    def launch_codex_app(self) -> None:
        if not self.codex_family_name:
            messagebox.showerror(APP_TITLE, "Codex app not found. Is it installed from the Microsoft Store?")
            return

        def work() -> None:
            # Kill any stale instances so we never have two Codex processes
            # fighting over the same refresh token.
            if _is_codex_app_running():
                subprocess.run(
                    ["taskkill", "/F", "/IM", f"{CODEX_PROCESS_NAME}.exe", "/T"],
                    capture_output=True, creationflags=0x08000000,
                )
                time.sleep(0.8)
            # Clear stuck heartbeat threads so Codex doesn't trigger a token-
            # refresh storm trying to reconnect dead sessions on startup.
            _clean_codex_state()
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{self.codex_family_name}!App"],
                creationflags=0x08000000,
            )
            time.sleep(2)

        def done(_: None) -> None:
            self._check_codex_app_state()
            self.status_var.set("Codex launched.")

        self._run_async(work, done, busy_msg="Launching Codex...")

    def stop_codex_app(self) -> None:
        if not messagebox.askyesno(APP_TITLE, "Terminate all Codex processes?"):
            return

        def work() -> None:
            subprocess.run(
                ["taskkill", "/F", "/IM", f"{CODEX_PROCESS_NAME}.exe", "/T"],
                capture_output=True, creationflags=0x08000000,
            )
            time.sleep(0.5)

        def done(_: None) -> None:
            self._check_codex_app_state()
            self.status_var.set("Codex stopped.")

        self._run_async(work, done, busy_msg="Stopping Codex...")

    def delete_profile(self) -> None:
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile first.")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"Delete profile '{sel.display_name}'?\n\n"
            "This removes the saved profile data. If it's currently active, the active "
            "Codex login is unaffected unless the CLI removes it.",
        ):
            return

        def work() -> str:
            rc, out, err = run_cli(["delete", "--id", sel.id, "--yes"])
            if rc != 0:
                raise RuntimeError(err or out or "delete failed")
            return out

        def done(_: str) -> None:
            # Clean cache + notifier entry
            self.cache.pop(sel.id, None)
            save_cache(self.cache)
            self.notifier.remove_profile(sel.id)
            self._update_next_reset_label()
            messagebox.showinfo(APP_TITLE, f"Deleted '{sel.display_name}'.")
            self.refresh()

        self._run_async(work, done, busy_msg="Deleting profile...")


    # ----- tray -----

    def _setup_tray(self) -> None:
        """Wire up WM_DELETE_WINDOW.  Adds a tray icon when pystray + Pillow are available."""
        if not _TRAY_AVAILABLE:
            # No tray: X closes the app normally
            self.root.protocol("WM_DELETE_WINDOW", self._quit_app)
            return
        try:
            if ICON_PATH.exists():
                img = _PILImage.open(str(ICON_PATH)).convert("RGBA").resize((64, 64))
            else:
                img = _PILImage.new("RGBA", (64, 64), (41, 98, 255, 255))
            menu = _pystray.Menu(
                _pystray.MenuItem("Show Kalikot", self._tray_show, default=True),
                _pystray.Menu.SEPARATOR,
                _pystray.MenuItem("Quit", self._tray_quit),
            )
            self._tray = _pystray.Icon("KalikotProfileSwitcher", img, APP_TITLE, menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
            self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        except Exception:
            # pystray failed to initialise (e.g., no display) — fall back
            self.root.protocol("WM_DELETE_WINDOW", self._quit_app)

    def _hide_to_tray(self) -> None:
        self.root.withdraw()

    def _start_ipc_listener(self) -> None:
        """Start a background thread that listens on localhost for SHOW_WINDOW signals.

        A second instance calls _send_show_signal() which connects here, sends
        b"SHOW_WINDOW", then exits.  We marshal the restore back to the main thread
        via root.after() so Tkinter is only touched from the main thread.

        The server socket uses SO_REUSEADDR so a quick restart doesn't get
        "address already in use".  If the port is unavailable (very unlikely on
        127.0.0.1:47321) the listener simply doesn't start — the app still works,
        it just won't auto-restore on second-instance launch.
        """
        def _listen() -> None:
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.settimeout(1.0)           # allows periodic stop-event checks
                srv.bind((IPC_HOST, IPC_PORT))
                srv.listen(1)
            except OSError:
                return                        # port unavailable — silent fallback

            try:
                while not self._ipc_stop.is_set():
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        data = conn.recv(64)
                        if data.strip() == b"SHOW_WINDOW":
                            self.root.after(0, self._show_from_tray)
                    except OSError:
                        pass
                    finally:
                        try:
                            conn.close()
                        except OSError:
                            pass
            finally:
                try:
                    srv.close()
                except OSError:
                    pass

        threading.Thread(target=_listen, daemon=True, name="ipc-listener").start()

    def _tray_show(self, icon: Any = None, item: Any = None) -> None:
        self.root.after(0, self._show_from_tray)

    def _show_from_tray(self) -> None:
        self.root.deiconify()
        # Brief -topmost flash forces Windows to bring the window to the front
        # even when another application currently owns focus.
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.root.after(100, lambda: self.root.attributes("-topmost", False))

    def _tray_quit(self, icon: Any = None, item: Any = None) -> None:
        self.root.after(0, self._quit_app)

    def _quit_app(self) -> None:
        self.notifier.save_state()
        # Signal the IPC listener thread to stop
        self._ipc_stop.set()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        # Release the named mutex so a new instance can claim it immediately
        if self._mutex_handle and self._mutex_handle > 0 and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
        self.root.destroy()

    # ----- notifications -----

    def _on_notify_toggle(self, *_: Any) -> None:
        self.notifier.enabled = self.notify_enabled.get()
        self.notifier.save_state()
        self._update_next_reset_label()

    def _show_in_app_toast(self, title: str, message: str) -> None:
        """Show a small floating overlay in the bottom-right corner of the screen.
        Works even when the main window is hidden in the tray."""
        try:
            toast = tk.Toplevel()
            toast.withdraw()                        # hide during setup to prevent flicker
            toast.wm_overrideredirect(True)         # no title bar / taskbar button
            toast.attributes("-topmost", True)

            sw = toast.winfo_screenwidth()
            sh = toast.winfo_screenheight()
            w, h = 360, 80
            toast.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 60}")
            toast.configure(bg="#1a1a2e")

            tk.Label(
                toast, text=title, bg="#1a1a2e", fg="white",
                font=("Segoe UI", 10, "bold"), anchor="w",
            ).pack(fill="x", padx=12, pady=(10, 0))
            tk.Label(
                toast, text=message, bg="#1a1a2e", fg="#aaaacc",
                font=("Segoe UI", 9), anchor="w", wraplength=336,
            ).pack(fill="x", padx=12)

            toast.deiconify()   # show regardless of whether root is withdrawn
            toast.after(7000, toast.destroy)
            toast.bind("<Button-1>", lambda _e: toast.destroy())
        except Exception:
            pass

    def _notify_reset(self, title: str, message: str) -> None:
        """Fire both a Windows native toast (persistent, works in tray) and an in-app overlay."""
        send_windows_toast(title, message)   # best-effort; silent on failure
        self._show_in_app_toast(title, message)

    def _check_and_notify(self) -> None:
        """
        Periodic callback (runs on the main thread via root.after).
        Fires notifications for any 5H resets that have passed since the last check,
        including any missed while the app was closed.
        Reschedules itself every 60 seconds.
        """
        due = self.notifier.get_due()
        for entry in due:
            name = entry.display_name
            title = f"5H Reset Ready — {APP_TITLE}"
            msg = f"5H limit has likely reset for '{name}'. Usage should be refreshed now."
            self._notify_reset(title, msg)
            self.notifier.mark_notified(entry.profile_id)
            self.status_var.set(f"⏰ 5H reset ready for '{name}' — click Refresh to confirm.")
        self._update_next_reset_label()
        self.root.after(60_000, self._check_and_notify)

    def _update_next_reset_label(self) -> None:
        """Update the subtle countdown line shown just above the status bar."""
        if not self.notify_enabled.get():
            self.next_reset_var.set("")
            return
        nxt = self.notifier.get_next_reset()
        if nxt:
            name, ts = nxt
            dt = datetime.fromtimestamp(ts)
            time_str = dt.strftime("%H:%M")
            mins = max(0, int((ts - time.time()) / 60))
            if mins < 60:
                self.next_reset_var.set(f"⏰  Next 5H reset: '{name}' in ~{mins} min (at {time_str})")
            else:
                hrs = mins // 60
                rem = mins % 60
                self.next_reset_var.set(
                    f"⏰  Next 5H reset: '{name}' in ~{hrs}h {rem}m (at {time_str})"
                )
        else:
            self.next_reset_var.set("")


def main() -> None:
    # Tell Windows which AppUserModelID represents this process so the taskbar
    # and Alt-Tab switcher show the correct icon, even when launched via
    # python.exe rather than the built EXE.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Kalikot.ProfileSwitcher"
            )
        except Exception:
            pass

    # Single-instance guard — only one window/tray icon may run at a time.
    mutex_handle = _try_acquire_mutex()
    if mutex_handle is None and os.name == "nt":
        # Another instance is already running — signal it to restore its window,
        # then exit cleanly without creating a second tray icon.
        _send_show_signal()
        sys.exit(0)

    root = tk.Tk()
    ProfileSwitcherApp(root, mutex_handle=mutex_handle if mutex_handle is not None else -1)
    root.mainloop()


if __name__ == "__main__":
    main()
