"""Kalikot Profile Switcher — aisw edition (v2)."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from notifier import ResetNotifier, send_windows_toast

try:
    import pystray as _pystray
    from PIL import Image as _PILImage
    _TRAY_AVAILABLE = True
except ImportError:
    _pystray = None   # type: ignore[assignment]
    _PILImage = None  # type: ignore[assignment]
    _TRAY_AVAILABLE = False


APP_TITLE  = "Kalikot Profile Switcher"
APP_VER    = "2.0 (aisw)"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _resource_path(name: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent.parent / name

ICON_PATH     = _resource_path("app.ico")
CACHE_DIR     = Path.home() / ".kalikot-profile-switcher"
CACHE_FILE    = CACHE_DIR / "usage-cache.json"
META_FILE     = CACHE_DIR / "aisw-meta.json"   # aisw-name → {email, codex_id, plan}
INSTANCES_DIR = CACHE_DIR / "instances"        # per-profile isolated CODEX_HOME dirs
TOKEN_LOG     = CACHE_DIR / "token-activity.jsonl"  # diagnostic: every token rotation

CODEX_HOME         = Path.home() / ".codex"
CODEX_AUTH         = CODEX_HOME / "auth.json"
CODEX_GLOBAL_STATE = CODEX_HOME / ".codex-global-state.json"
AISW_HOME          = Path.home() / ".aisw"
# The combined app: OpenAI merged Codex into the ChatGPT desktop app. The package
# is still OpenAI.Codex (so the launch AUMID is unchanged), but the main process
# is now ChatGPT.exe — the old Codex.exe is only a bundled child helper. Killing/
# detecting Codex.exe therefore missed the real app (stop/start mismatch) AND broke
# the "is it running" safety gate. Target the real main process. (The old ChatGPT
# desktop is now "ChatGPT Classic.exe", a different image, so no collision. The
# Codex CLI binary is codex.exe/node, also unaffected.)
CODEX_PROCESS_NAME = "ChatGPT"

SINGLE_INSTANCE_MUTEX = "Local\\KalikotAISWv2SingleInstance"
IPC_HOST    = "127.0.0.1"
IPC_PORT    = 47322
IPC_TIMEOUT = 1.0


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Single-instance helpers
# ---------------------------------------------------------------------------

def _try_acquire_mutex() -> Optional[int]:
    if os.name != "nt":
        return None
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX)
        if ctypes.windll.kernel32.GetLastError() == 183:
            if h: ctypes.windll.kernel32.CloseHandle(h)
            return None
        return int(h) if h else None
    except Exception:
        return None

def _send_show_signal() -> None:
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=IPC_TIMEOUT) as s:
            s.sendall(b"SHOW_WINDOW")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Codex state / heartbeat helpers
# ---------------------------------------------------------------------------

def _find_codex_family_name() -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage -Name 'OpenAI.Codex').PackageFamilyName"],
            capture_output=True, text=True, creationflags=0x08000000, timeout=10)
        n = r.stdout.strip()
        return n if n else None
    except Exception:
        return None

def _is_codex_running() -> bool:
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {CODEX_PROCESS_NAME}.exe", "/NH"],
            capture_output=True, text=True, creationflags=0x08000000, timeout=5)
        return f"{CODEX_PROCESS_NAME}.exe" in r.stdout
    except Exception:
        return False

def _kill_codex() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", f"{CODEX_PROCESS_NAME}.exe", "/T"],
        capture_output=True, creationflags=0x08000000)
    time.sleep(0.8)

def _sanitize_codex_config() -> bool:
    """Ensure `cli_auth_credentials_store` is a TOP-LEVEL key, not under [features].

    aisw re-injects this key when switching profiles, sometimes landing it under
    the [features] table.  Codex validates every [features] entry as a boolean, so
    a string value there crashes config loading with:
        'invalid type: string "file", expected a boolean'
    which blocks the whole app ("Failed to resume chat").

    This moves the key back to the top level.  Returns True if a fix was applied.
    """
    path = CODEX_HOME / "config.toml"
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    KEY = "cli_auth_credentials_store"
    section = ""              # "" = root/top-level
    out: list[str] = []
    moved_value: Optional[str] = None
    has_toplevel = False

    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s
            out.append(line)
            continue
        if "=" in s and s.split("=", 1)[0].strip() == KEY:
            value = s.split("=", 1)[1].strip()
            if section == "":
                has_toplevel = True
                out.append(line)          # already correct
            else:
                moved_value = value       # misplaced — drop here, re-add at top
            continue
        out.append(line)

    if moved_value is None:
        return False                      # nothing misplaced

    if not has_toplevel:
        # Insert just before the first [section] header (end of top-level block)
        insert_at = len(out)
        for i, l in enumerate(out):
            t = l.strip()
            if t.startswith("[") and t.endswith("]"):
                insert_at = i
                break
        out.insert(insert_at, f"{KEY} = {moved_value}")

    try:
        path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def _clean_codex_state() -> None:
    """Remove orphaned heartbeat-thread entries from Codex global state.

    CRITICAL SAFETY RULE: this must NEVER run while Codex is running.  Mutating
    .codex-global-state.json under a live Codex process makes Codex lose track of
    its active heartbeat threads → it re-authenticates → the session is killed.
    It is only safe to touch this file when Codex is fully stopped (e.g. right
    after we taskkill it during a profile switch / relaunch).
    """
    if _is_codex_running():
        return
    if not CODEX_GLOBAL_STATE.exists():
        return
    try:
        state = json.loads(CODEX_GLOBAL_STATE.read_text(encoding="utf-8"))
        atom  = state.get("electron-persisted-atom-state", {})
        hb: dict = atom.get("heartbeat-thread-permissions-by-id", {})
        if not hb:
            return
        sessions_root = CODEX_HOME / "sessions"
        dead = [
            tid for tid in list(hb)
            if not (list(sessions_root.rglob(f"*{tid}*.jsonl"))
                    if sessions_root.exists() else [])
        ]
        if dead:
            for tid in dead:
                hb.pop(tid, None)
            atom["heartbeat-thread-permissions-by-id"] = hb
            CODEX_GLOBAL_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
            bak = CODEX_GLOBAL_STATE.with_suffix(".json.bak")
            try: bak.write_text(json.dumps(state, indent=2), encoding="utf-8")
            except OSError: pass
    except Exception:
        pass


def _clear_all_heartbeats() -> None:
    """Wipe ALL heartbeat-thread-permissions when CHANGING accounts.

    Root cause of 'session has ended' on switch / new-login: the previous account's
    heartbeat threads stay in .codex-global-state.json.  On the next launch Codex
    tries to RECONNECT those threads — but under the NEW account's token.  That
    cross-account reconnect makes OpenAI reject the refresh → the new session dies.

    A same-account relaunch (Start Menu) is fine because the threads match the
    account; only an account *change* needs a full wipe.  Only ever runs while
    Codex is stopped (so it never disturbs a live session).
    """
    if _is_codex_running():
        return
    if not CODEX_GLOBAL_STATE.exists():
        return
    try:
        state = json.loads(CODEX_GLOBAL_STATE.read_text(encoding="utf-8"))
        atom  = state.get("electron-persisted-atom-state")
        if not isinstance(atom, dict):
            return
        if atom.get("heartbeat-thread-permissions-by-id"):
            atom["heartbeat-thread-permissions-by-id"] = {}
            CODEX_GLOBAL_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
            bak = CODEX_GLOBAL_STATE.with_suffix(".json.bak")
            try: bak.write_text(json.dumps(state, indent=2), encoding="utf-8")
            except OSError: pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI wrappers
# ---------------------------------------------------------------------------

def _account_id_of(auth_file: Path) -> Optional[str]:
    """Read the account_id from an auth.json, or None."""
    try:
        d = json.loads(auth_file.read_text(encoding="utf-8"))
        return (d.get("tokens") or {}).get("account_id") or None
    except Exception:
        return None


def _live_matches_profile(aisw_name: str) -> bool:
    """True if the LIVE ~/.codex/auth.json belongs to the same account as this
    aisw profile's stored credentials.  Used before saving live→store so we never
    overwrite a profile with a different account's tokens."""
    live  = _account_id_of(CODEX_AUTH)
    store = _account_id_of(AISW_HOME / "profiles" / "codex" / aisw_name / "auth.json")
    return bool(live) and live == store


def _b64pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def _jwt_exp(token: Optional[str]) -> Optional[int]:
    """Return the `exp` (unix ts) from a JWT access token, or None."""
    if not token:
        return None
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(base64.urlsafe_b64decode(_b64pad(parts[1])))
        return payload.get("exp")
    except Exception:
        return None


def _token_fingerprint(token: Optional[str]) -> str:
    """Short, non-reversible fingerprint of a token (never logs the token itself)."""
    if not token:
        return "—"
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def _read_auth(path: Path) -> dict:
    """Read an auth.json and return a safe summary (no raw tokens)."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        t = d.get("tokens") or {}
        return {
            "account_id":   (t.get("account_id") or "")[:12],
            "last_refresh": d.get("last_refresh"),
            "refresh_fp":   _token_fingerprint(t.get("refresh_token")),
            "access_fp":    _token_fingerprint(t.get("access_token")),
            "access_exp":   _jwt_exp(t.get("access_token")),
        }
    except Exception:
        return {}


def _status_is_dead_token(status: Any) -> bool:
    """Detect a dead/stale refresh token in a codex-profiles status JSON result.

    OpenAI returns 'app_session_terminated' / 'session has ended' when the stored
    refresh token was already rotated elsewhere (e.g. used in a CLI session) — the
    profile must be re-logged-in; no code can revive it.
    """
    try:
        prof = None
        if isinstance(status, dict):
            if status.get("profiles"):
                prof = status["profiles"][0]
            elif "id" in status or "error" in status:
                prof = status
        if not prof:
            return False
        err = prof.get("error") or {}
        blob = f"{err.get('summary', {}).get('message', '')} {err.get('detail', '')}"
        markers = ("app_session_terminated", "session has ended",
                   "session_terminated", "token_invalidated", "already been used")
        return any(m in blob for m in markers)
    except Exception:
        return False


def _resolve_codex_id(aisw_name: str) -> Optional[str]:
    """Map an aisw profile name → codex-profiles ID by matching account_id.

    Both tools store the same account_id inside their respective auth/profile
    files.  Comparing them lets us link aisw 'corrin' → codex-profiles
    'corinnegsadierh@outlook.com-plus' without any user input.

    The result is cached in META_FILE so this lookup only runs once per profile.
    """
    aisw_auth_file = AISW_HOME / "profiles" / "codex" / aisw_name / "auth.json"
    if not aisw_auth_file.exists():
        return None
    try:
        aisw_data  = json.loads(aisw_auth_file.read_text(encoding="utf-8"))
        account_id = (aisw_data.get("tokens") or {}).get("account_id", "")
        if not account_id:
            return None
    except Exception:
        return None

    cdp_dir = CODEX_HOME / "profiles"
    if not cdp_dir.exists():
        return None
    skip = {"profiles.json", "update.json", "profiles.lock"}
    for f in cdp_dir.glob("*.json"):
        if f.name in skip:
            continue
        try:
            cdp_data       = json.loads(f.read_text(encoding="utf-8"))
            cdp_account_id = (cdp_data.get("tokens") or {}).get("account_id", "")
            if cdp_account_id and cdp_account_id == account_id:
                return f.stem          # e.g. "corinnegsadierh@outlook.com-plus"
        except Exception:
            continue
    return None


def _find_cli(names: list[str]) -> list[str]:
    for n in names:
        found = shutil.which(n)
        if found:
            return [found]
    return [names[0]]

AISW_PREFIX = _find_cli(["aisw.exe", "aisw.cmd", "aisw"])
CDP_PREFIX  = _find_cli(["codex-profiles.exe", "codex-profiles.cmd", "codex-profiles"])

def _run(prefix: list[str], args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    cmd = prefix + args
    cf  = 0x08000000 if os.name == "nt" else 0
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=cf)
        return r.returncode, r.stdout or "", r.stderr or ""
    except FileNotFoundError:
        return -1, "", f"{prefix[0]} not found in PATH."
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s."
    except Exception as e:
        return -1, "", str(e)

def run_aisw(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    return _run(AISW_PREFIX, args, timeout)

def run_cdp(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    return _run(CDP_PREFIX, args, timeout)


# ---------------------------------------------------------------------------
# Isolated CLI session launcher
# ---------------------------------------------------------------------------
# Each profile can run as an independent Codex CLI session in its own terminal,
# using a private CODEX_HOME.  Because every session keeps its own auth.json,
# multiple accounts run side-by-side with ZERO token conflicts — they never
# touch ~/.codex/auth.json or each other.

def _codex_cli_command() -> Optional[str]:
    """Resolve the `codex` CLI launcher (the npm .cmd shim is fine)."""
    for n in ("codex.cmd", "codex.exe", "codex"):
        found = shutil.which(n)
        if found:
            return found
    return None


def _prepare_instance_home(aisw_name: str) -> Path:
    """Create / refresh an isolated CODEX_HOME for a profile and return its path.

    The instance home gets a copy of:
      • the profile's auth.json (from aisw storage) — so it logs in as that account
      • the base config.toml (from ~/.codex) — so model/plugins/trust settings carry

    The instance's auth.json is independent: when Codex refreshes the token inside
    this session, it writes back HERE, never to ~/.codex or the aisw profile.
    """
    home = INSTANCES_DIR / aisw_name
    home.mkdir(parents=True, exist_ok=True)

    # 1 — copy the account's credentials
    aisw_auth = AISW_HOME / "profiles" / "codex" / aisw_name / "auth.json"
    if aisw_auth.exists():
        shutil.copy2(aisw_auth, home / "auth.json")

    # 2 — carry over base config (model, plugins, trusted projects, mcp servers)
    base_config = CODEX_HOME / "config.toml"
    dest_config = home / "config.toml"
    if base_config.exists() and not dest_config.exists():
        try:
            shutil.copy2(base_config, dest_config)
        except OSError:
            pass

    return home


def _launch_cli_session(aisw_name: str, display: str, workspace: Optional[str] = None) -> str:
    """Open a new terminal running an isolated Codex CLI session for this profile.

    Each session:
      • runs under its own CODEX_HOME (no token conflicts)
      • gets a locked terminal title showing the account (so you always know which
        window belongs to which account)
      • writes its launcher PID to <home>/.cli.pid so the GUI can show a live
        'CLI' status and clear it when the window is closed.

    Prefers Windows Terminal (wt.exe); falls back to a standalone PowerShell window.
    """
    codex = _codex_cli_command()
    if not codex:
        raise RuntimeError("Codex CLI not found on PATH (expected 'codex.cmd').")

    home = _prepare_instance_home(aisw_name)
    ws   = workspace or str(Path.home())
    title = f"Codex: {display}"

    # Clear any stale pid from a previous run
    pid_file = home / ".cli.pid"
    try:
        pid_file.unlink()
    except OSError:
        pass

    # Write a per-session PowerShell launcher into the instance home.
    # PowerShell exposes $PID, letting us record the live window's process id.
    def _ps_quote(s: str) -> str:
        return s.replace("'", "''")

    script = home / "_session.ps1"
    script.write_text(
        "$Host.UI.RawUI.WindowTitle = '{title}'\n"
        "$env:CODEX_HOME = '{home}'\n"
        "$PID | Set-Content -Encoding ascii '{pid}'\n"
        "Set-Location '{ws}'\n"
        "& '{codex}'\n".format(
            title=_ps_quote(title),
            home=_ps_quote(str(home)),
            pid=_ps_quote(str(pid_file)),
            ws=_ps_quote(ws),
            codex=_ps_quote(codex),
        ),
        encoding="utf-8",
    )

    ps_args = ["powershell", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", str(script)]
    wt = shutil.which("wt.exe")
    try:
        if wt:
            # --suppressApplicationTitle keeps OUR title even if Codex tries to change it
            subprocess.Popen(
                [wt, "new-tab", "--title", title, "--suppressApplicationTitle", *ps_args],
                creationflags=0x08000000,
            )
        else:
            subprocess.Popen(
                ["cmd", "/c", "start", "", *ps_args],
                creationflags=0,
            )
        return f"Launched CLI session for '{display}'."
    except Exception as exc:
        raise RuntimeError(f"Could not launch terminal: {exc}")


# ---------------------------------------------------------------------------
# Live CLI-session detection (via per-instance PID files)
# ---------------------------------------------------------------------------

def _running_pids() -> set[int]:
    """Return the set of currently running process IDs (one tasklist call)."""
    pids: set[int] = set()
    if os.name != "nt":
        return pids
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True,
                           creationflags=0x08000000, timeout=8)
        for line in r.stdout.splitlines():
            # CSV: "image","PID","session","#","mem"
            parts = line.split('","')
            if len(parts) >= 2:
                try:
                    pids.add(int(parts[1].strip('"').strip()))
                except ValueError:
                    pass
    except Exception:
        pass
    return pids


def _cli_active_names(running: Optional[set[int]] = None) -> set[str]:
    """Profiles that currently have a LIVE CLI session window open.

    Reads each instance's .cli.pid and checks if that PowerShell window is still
    running.  Stale pid files (window closed) are removed and not counted.
    """
    names: set[str] = set()
    if not INSTANCES_DIR.exists():
        return names
    if running is None:
        running = _running_pids()
    for d in INSTANCES_DIR.iterdir():
        if not d.is_dir():
            continue
        pid_file = d / ".cli.pid"
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if pid in running:
            names.add(d.name)
        else:
            try:
                pid_file.unlink()   # stale — window was closed
            except OSError:
                pass
    return names


# ---------------------------------------------------------------------------
# Email masking
# ---------------------------------------------------------------------------

def fmt_relative(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return f"{int(delta // 86400)} d ago"


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if len(local) <= 4:
        return local[0] + "*" * max(0, len(local) - 1) + "@" + domain
    return local[0] + ("*" * (len(local) - 4)) + local[-3:] + "@" + domain


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    name:      str
    label:     Optional[str]
    auth:      str
    is_active: bool
    # filled from metadata cache / status call
    email:     Optional[str] = None
    plan:      Optional[str] = None
    five_hour: Optional[dict] = None   # {left_percent, reset_at}
    weekly:    Optional[dict] = None   # {left_percent, reset_at}
    usage_err: Optional[str]  = None

    @property
    def display_name(self) -> str:
        return self.label or self.name

    @property
    def is_api_key(self) -> bool:
        return self.auth == "api_key"


def _parse_aisw_profiles(raw: str) -> tuple[list[Profile], Optional[str]]:
    """Parse `aisw list --json` → (profiles, active_name)."""
    try:
        data   = json.loads(raw)
        codex  = data.get("codex", {})
        active = codex.get("active")
        profs  = [
            Profile(
                name=p.get("name", ""),
                label=p.get("label"),
                auth=p.get("auth", "oauth"),
                is_active=(p.get("name") == active),
            )
            for p in codex.get("profiles", [])
        ]
        return profs, active
    except Exception:
        return [], None


def _valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", name))


# ---------------------------------------------------------------------------
# Simple colored progress bar widget
# ---------------------------------------------------------------------------

class UsageBar(tk.Frame):
    """Horizontal colored bar — value 0-100 = percent remaining."""
    def __init__(self, parent: Any, **kwargs: Any) -> None:
        super().__init__(parent, **kwargs)
        self._canvas = tk.Canvas(self, height=14, bg="#e0e0e0",
                                 highlightthickness=1, highlightbackground="#bbb")
        self._canvas.pack(fill="x", expand=True)
        self._canvas.bind("<Configure>", lambda _: self._draw())
        self._value = 0.0
        self._text  = ""

    def set(self, value: float, text: str = "") -> None:
        self._value = max(0.0, min(100.0, value))
        self._text  = text
        self._draw()

    def _draw(self) -> None:
        c = self._canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1:
            return
        fw = int(w * self._value / 100)
        color = ("#4caf50" if self._value > 50
                 else "#ff9800" if self._value > 20
                 else "#f44336")
        if fw > 0:
            c.create_rectangle(0, 0, fw, h, fill=color, outline="")
        if self._text:
            c.create_text(w // 2, h // 2, text=self._text,
                          fill="white" if fw > w // 2 else "#444",
                          font=("Segoe UI", 8))


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class ProfileSwitcherApp:

    def __init__(self, root: tk.Tk, mutex_handle: int = -1) -> None:
        self.root = root
        self.root.title(f"{APP_TITLE}  •  {APP_VER}")
        self.root.geometry("760x620")
        self.root.minsize(680, 540)

        if ICON_PATH.exists():
            try: self.root.iconbitmap(str(ICON_PATH))
            except tk.TclError: pass

        self.profiles:   list[Profile] = []
        self.meta:       dict = _load_json(META_FILE)   # aisw-name → {email, codex_id, plan}
        self.usage_cache: dict = _load_json(CACHE_FILE)
        self.busy = False
        self.result_queue: queue.Queue = queue.Queue()
        self.codex_family_name: Optional[str] = None

        self._mutex_handle = mutex_handle
        self._ipc_stop     = threading.Event()
        self._tray: Any    = None
        self._cli_active: set[str] = set()   # profiles with a live CLI session

        # UI variables
        self.active_var      = tk.StringVar(value="-")
        self.codex_status_var= tk.StringVar(value="-")
        self.hide_emails     = tk.BooleanVar(value=False)
        self.status_var      = tk.StringVar(value="Ready.")
        self.next_reset_var  = tk.StringVar(value="")

        # Notifier
        self.notifier = ResetNotifier()
        self.notify_enabled = tk.BooleanVar(value=self.notifier.enabled)
        self.notify_enabled.trace_add("write", self._on_notify_toggle)

        # Token-activity watcher state
        self._last_auth_fp: Optional[str] = None
        self._health_win: Any = None

        self._build_ui()
        self._setup_tray()
        self._start_ipc_listener()
        self.root.after(50,   self._poll_queue)
        threading.Thread(target=self._resolve_codex_family, daemon=True).start()
        self.root.after(500,  self._check_and_notify)
        self.root.after(800,  self._periodic_config_sanitize)  # repair config early + every 60s
        self.root.after(1200, self._poll_token_activity)       # diagnostic token-rotation logger
        # NOTE: no periodic heartbeat cleanup — mutating Codex global state while
        # Codex runs kills the session. Cleanup happens ONLY during switch/launch,
        # after Codex has been stopped.
        self.refresh()

    # -----------------------------------------------------------------
    # UI build
    # -----------------------------------------------------------------

    def _build_ui(self) -> None:
        style = ttk.Style()
        try: style.theme_use("vista" if os.name == "nt" else style.theme_use())
        except tk.TclError: pass

        # ── 1. Top info bar ───────────────────────────────────────────
        top = ttk.Frame(self.root, padding=(10, 5))
        top.pack(fill="x", side="top")
        ttk.Label(top, text="Active:").pack(side="left")
        ttk.Label(top, textvariable=self.active_var,
                  foreground="#2962ff", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4, 16))
        ttk.Label(top, text="Codex:").pack(side="left")
        self._codex_lbl = ttk.Label(top, textvariable=self.codex_status_var)
        self._codex_lbl.pack(side="left", padx=(4, 0))
        ttk.Checkbutton(top, text="Hide emails", variable=self.hide_emails,
                        command=self._refresh_tree).pack(side="right")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", side="top")

        # ── 4. Status / next-reset bar (pack BEFORE middle so it anchors bottom) ──
        ttk.Label(self.root, textvariable=self.status_var,
                  relief="flat", anchor="w", padding=(8, 3)).pack(fill="x", side="bottom")
        ttk.Label(self.root, textvariable=self.next_reset_var,
                  foreground="#777", font=("Segoe UI", 8),
                  anchor="w", padding=(8, 1)).pack(fill="x", side="bottom")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", side="bottom")

        # ── 3. Detail panel (pack BEFORE paned so it anchors just above status) ──
        detail = ttk.LabelFrame(self.root, text="Selected Profile", padding=(10, 6))
        detail.pack(fill="x", side="bottom", padx=8, pady=(0, 4))

        # Row 1: email + plan
        r1 = ttk.Frame(detail)
        r1.pack(fill="x")
        ttk.Label(r1, text="Email:", width=9, anchor="w").grid(row=0, column=0, sticky="w")
        self._email_lbl = ttk.Label(r1, text="—", foreground="#333")
        self._email_lbl.grid(row=0, column=1, sticky="w", padx=(0, 24))
        ttk.Label(r1, text="Plan:", width=6, anchor="w").grid(row=0, column=2, sticky="w")
        self._plan_lbl = ttk.Label(r1, text="—", foreground="#333")
        self._plan_lbl.grid(row=0, column=3, sticky="w")
        r1.columnconfigure(1, weight=1)

        # Note row
        rn = ttk.Frame(detail)
        rn.pack(fill="x", pady=(2, 0))
        ttk.Label(rn, text="Note:", width=9, anchor="w").pack(side="left")
        self._note_lbl = ttk.Label(rn, text="—", foreground="#6a1b9a")
        self._note_lbl.pack(side="left", fill="x", expand=True)

        # Row 2: 5H bar
        r2 = ttk.Frame(detail)
        r2.pack(fill="x", pady=(5, 0))
        ttk.Label(r2, text="5H:", width=9, anchor="w").pack(side="left")
        self._5h_bar = UsageBar(r2)
        self._5h_bar.pack(side="left", fill="x", expand=True)
        self._5h_lbl = ttk.Label(r2, text="", foreground="#555", width=24, anchor="w")
        self._5h_lbl.pack(side="left", padx=(8, 0))

        # Row 3: weekly bar
        r3 = ttk.Frame(detail)
        r3.pack(fill="x", pady=(4, 0))
        ttk.Label(r3, text="Weekly:", width=9, anchor="w").pack(side="left")
        self._wk_bar = UsageBar(r3)
        self._wk_bar.pack(side="left", fill="x", expand=True)
        self._wk_lbl = ttk.Label(r3, text="", foreground="#555", width=24, anchor="w")
        self._wk_lbl.pack(side="left", padx=(8, 0))

        # ── 2. Middle: profile list (left) + actions (right) ─────────
        pw = ttk.PanedWindow(self.root, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=(6, 0), side="top")

        # Left pane — profile list (full height, no detail panel inside)
        left = ttk.Frame(pw)
        pw.add(left, weight=3)
        ttk.Label(left, text="Profiles", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))

        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True)
        cols = ("active", "name", "status", "note", "email")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                 selectmode="browse")
        self.tree.heading("active", text="")
        self.tree.heading("name",   text="Name")
        self.tree.heading("status", text="Active In")
        self.tree.heading("note",   text="Note")
        self.tree.heading("email",  text="Email")
        self.tree.column("active", width=24,  stretch=False, anchor="center")
        self.tree.column("name",   width=110, stretch=True)
        self.tree.column("status", width=95,  stretch=False, anchor="center")
        self.tree.column("note",   width=130, stretch=True)
        self.tree.column("email",  width=170, stretch=True)
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.tag_configure("active", foreground="#2962ff", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("cli", foreground="#2e7d32")
        self.tree.tag_configure("stale", foreground="#c62828")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _: self.switch_profile())

        # Right pane — action buttons
        right = ttk.Frame(pw, padding=(8, 0, 4, 0))
        pw.add(right, weight=1)

        W = {"width": 20}
        ttk.Label(right, text="Profile", foreground="#666").pack(anchor="w")
        self.btn_refresh  = ttk.Button(right, text="Refresh",           command=self.refresh,           **W)
        self.btn_refresh.pack(pady=2, fill="x")
        self.btn_switch   = ttk.Button(right, text="Switch Profile",    command=self.switch_profile,    **W)
        self.btn_switch.pack(pady=2, fill="x")
        self.btn_next     = ttk.Button(right, text="Switch to Next",    command=self.switch_to_next,    **W)
        self.btn_next.pack(pady=2, fill="x")
        self.btn_save     = ttk.Button(right, text="Save Current As…",  command=self.save_current,      **W)
        self.btn_save.pack(pady=2, fill="x")
        self.btn_newlogin = ttk.Button(right, text="Prepare New Login", command=self.prepare_new_login, **W)
        self.btn_newlogin.pack(pady=2, fill="x")
        self.btn_rename   = ttk.Button(right, text="Rename Profile",    command=self.rename_profile,    **W)
        self.btn_rename.pack(pady=2, fill="x")
        self.btn_note     = ttk.Button(right, text="Edit Note",         command=self.edit_note,         **W)
        self.btn_note.pack(pady=2, fill="x")
        self.btn_delete   = ttk.Button(right, text="Delete Profile",    command=self.delete_profile,    **W)
        self.btn_delete.pack(pady=2, fill="x")

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(8, 4))
        ttk.Label(right, text="Codex App", foreground="#666").pack(anchor="w")
        self.btn_launch   = ttk.Button(right, text="Launch Codex",      command=self.launch_codex,      **W)
        self.btn_launch.pack(pady=2, fill="x")
        self.btn_stop     = ttk.Button(right, text="Stop Codex",        command=self.stop_codex,        **W)
        self.btn_stop.pack(pady=2, fill="x")

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(8, 4))
        ttk.Label(right, text="Multi-Session (CLI)", foreground="#666").pack(anchor="w")
        self.btn_cli      = ttk.Button(right, text="Launch CLI Session", command=self.launch_cli_session, **W)
        self.btn_cli.pack(pady=2, fill="x")

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(8, 4))
        ttk.Label(right, text="Notifications", foreground="#666").pack(anchor="w")
        ttk.Checkbutton(right, text="5H Reset Alerts",
                        variable=self.notify_enabled).pack(anchor="w", pady=2)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(8, 4))
        ttk.Label(right, text="Tools", foreground="#666").pack(anchor="w")
        self.btn_health   = ttk.Button(right, text="Session Health",    command=self.show_session_health, **W)
        self.btn_health.pack(pady=2, fill="x")
        self.btn_doctor   = ttk.Button(right, text="Run Doctor",        command=self.run_doctor,        **W)
        self.btn_doctor.pack(pady=2, fill="x")

    # -----------------------------------------------------------------
    # Busy / helpers
    # -----------------------------------------------------------------

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "!disabled"
        for btn in (self.btn_refresh, self.btn_switch, self.btn_next, self.btn_save,
                    self.btn_newlogin, self.btn_rename, self.btn_note, self.btn_delete,
                    self.btn_launch, self.btn_stop, self.btn_cli):
            try: btn.state([state])
            except (tk.TclError, AttributeError): pass
        if msg:
            self.status_var.set(msg)

    def _selected_profile(self) -> Optional[Profile]:
        sel = self.tree.selection()
        if not sel:
            return None
        name = self.tree.item(sel[0], "values")[1]
        return next((p for p in self.profiles if p.name == name), None)

    def _status_text(self, p: Profile) -> str:
        """Where this account is currently active: Codex GUI and/or CLI session(s)."""
        if self.meta.get(p.name, {}).get("stale"):
            return "⚠ Re-login"
        parts = []
        if p.is_active:
            parts.append("Codex")
        if p.name in self._cli_active:
            parts.append("CLI")
        return " + ".join(parts)

    def _row_tag(self, p: Profile) -> str:
        if self.meta.get(p.name, {}).get("stale"):
            return "stale"
        if p.is_active:
            return "active"
        if p.name in self._cli_active:
            return "cli"
        return ""

    def _row_marker(self, p: Profile) -> str:
        if self.meta.get(p.name, {}).get("stale"):
            return "⚠"
        if p.is_active:
            return "●"
        if p.name in self._cli_active:
            return "▸"
        return ""

    def _refresh_tree(self) -> None:
        sel = self.tree.selection()
        sel_name = self.tree.item(sel[0], "values")[1] if sel else None

        self.tree.delete(*self.tree.get_children())
        active = next((p.name for p in self.profiles if p.is_active), None)
        self.active_var.set(active or "-")

        new_sel = None
        for p in self.profiles:
            email_raw = p.email or self.meta.get(p.name, {}).get("email", "")
            email_disp = (mask_email(email_raw) if self.hide_emails.get()
                          else email_raw) if email_raw else ""
            tag = self._row_tag(p)
            note = self.meta.get(p.name, {}).get("note", "")
            iid = self.tree.insert("", "end",
                                   values=(self._row_marker(p), p.name, self._status_text(p),
                                           note, email_disp),
                                   tags=((tag,) if tag else ()))
            if p.name == sel_name:
                new_sel = iid

        if new_sel:
            self.tree.selection_set(new_sel)
            self.tree.see(new_sel)

    def _refresh_cli_status(self) -> None:
        """Recompute live CLI sessions in a worker, then update the Status column in place."""
        def _work() -> None:
            names = _cli_active_names()
            self.root.after(0, lambda: self._apply_cli_status(names))
        threading.Thread(target=_work, daemon=True).start()

    def _apply_cli_status(self, names: set[str]) -> None:
        if names == self._cli_active:
            return
        self._cli_active = names
        # Update each row's Status cell + tag without a full rebuild
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            name = vals[1]
            p = next((pp for pp in self.profiles if pp.name == name), None)
            if not p:
                continue
            self.tree.set(iid, "status", self._status_text(p))
            self.tree.set(iid, "active", self._row_marker(p))
            tag = self._row_tag(p)
            self.tree.item(iid, tags=((tag,) if tag else ()))

    def _on_select(self, _event: Any = None) -> None:
        sel = self._selected_profile()
        if not sel:
            self._clear_detail()
            return
        self._render_detail(sel)

    def _clear_detail(self) -> None:
        self._email_lbl.configure(text="-")
        self._plan_lbl.configure(text="-")
        self._note_lbl.configure(text="—")
        self._5h_bar.set(0, "")
        self._5h_lbl.configure(text="")
        self._wk_bar.set(0, "")
        self._wk_lbl.configure(text="")

    def _render_detail(self, p: Profile) -> None:
        # Email
        email = p.email or self.meta.get(p.name, {}).get("email", "")
        if email:
            disp = mask_email(email) if self.hide_emails.get() else email
            self._email_lbl.configure(text=disp)
        else:
            self._email_lbl.configure(text="—  (refresh to load)")

        # Plan
        plan = p.plan or self.meta.get(p.name, {}).get("plan", "")
        self._plan_lbl.configure(text=plan or "-")

        # Note
        note = self.meta.get(p.name, {}).get("note", "")
        self._note_lbl.configure(text=note or "—  (click 'Edit Note' to add)")

        # "(cached)" suffix for non-active profiles (their usage is last-known, not live)
        cached_tag = "" if p.is_active else " · cached"

        # 5H usage
        fh = p.five_hour
        if fh and fh.get("left_percent") is not None:
            pct = float(fh["left_percent"])
            reset_at = fh.get("reset_at")
            reset_str = ""
            if reset_at:
                try:
                    mins = max(0, int((int(reset_at) - time.time()) / 60))
                    if mins < 60:
                        reset_str = f"resets in ~{mins}m"
                    else:
                        reset_str = f"resets in ~{mins // 60}h {mins % 60}m"
                except Exception:
                    pass
            self._5h_bar.set(pct, f"{int(pct)}%")
            self._5h_lbl.configure(text=(reset_str + cached_tag).strip(" ·"))
        else:
            self._5h_bar.set(0, "—")
            self._5h_lbl.configure(
                text=("switch to see live usage" if not p.is_active else ""))

        # Weekly usage
        wk = p.weekly
        if wk and wk.get("left_percent") is not None:
            pct = float(wk["left_percent"])
            reset_at = wk.get("reset_at")
            reset_str = ""
            if reset_at:
                try:
                    secs = int(reset_at) - time.time()
                    mins = max(0, int(secs / 60))
                    if mins < 60:
                        reset_str = f"resets in ~{mins}m"
                    elif mins < 1440:                       # < 1 day
                        reset_str = f"resets in ~{mins // 60}h {mins % 60}m"
                    else:                                    # days + hours
                        days = mins // 1440
                        hrs  = (mins % 1440) // 60
                        reset_str = f"resets in ~{days}d {hrs}h"
                except Exception:
                    pass
            self._wk_bar.set(pct, f"{int(pct)}%")
            self._wk_lbl.configure(text=(reset_str + cached_tag).strip(" ·"))
        else:
            self._wk_bar.set(0, "—")
            self._wk_lbl.configure(
                text=("switch to see live usage" if not p.is_active else ""))

    # -----------------------------------------------------------------
    # Async runner
    # -----------------------------------------------------------------

    def _run_async(self, work: Any, done: Any, busy_msg: str = "Working…") -> None:
        self._set_busy(True, busy_msg)
        def _worker() -> None:
            try:
                self.result_queue.put(("ok", work(), done))
            except Exception as exc:
                self.result_queue.put(("err", str(exc), None))
        threading.Thread(target=_worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload, cb = self.result_queue.get_nowait()
                self._set_busy(False)
                if kind == "err":
                    self.status_var.set(f"Error: {payload}")
                    messagebox.showerror(APP_TITLE, payload)
                elif cb:
                    cb(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    # -----------------------------------------------------------------
    # Refresh  (list all + usage for SELECTED ONLY)
    # -----------------------------------------------------------------

    def refresh(self) -> None:
        """List all profiles (local, no API) + fetch usage for selected profile only.

        Never touches non-selected profiles' tokens — doing so causes the
        'app_session_terminated' / 'refresh token already used' errors.
        """
        sel = self._selected_profile()
        sel_name = sel.name if sel else None

        def work() -> dict:
            # 1 — list aisw profiles (local config read, zero API calls)
            rc, out, _ = run_aisw(["list", "--json"])
            if rc != 0:
                raise RuntimeError(out or "aisw list failed")
            profs, active = _parse_aisw_profiles(out)

            # 2 — usage only for selected profile
            usage_data: Optional[dict] = None
            if sel_name:
                # find the selected profile in aisw; if it's active we can get
                # live usage via codex-profiles (which reads the same auth.json)
                target = next((p for p in profs if p.name == sel_name), None)
                if target and target.is_active:
                    # active profile: fetch live usage via codex-profiles status
                    rc2, out2, _ = run_cdp(["list", "--json"])
                    if rc2 == 0 and out2.strip():
                        try:
                            cdp_data = json.loads(out2)
                            cur = next((p for p in cdp_data.get("profiles", [])
                                        if p.get("is_current")), None)
                            if cur:
                                # store email/plan in meta
                                meta = _load_json(META_FILE)
                                meta.setdefault(sel_name, {})
                                meta[sel_name]["email"]     = cur.get("email", "")
                                meta[sel_name]["codex_id"]  = cur.get("id", "")
                                meta[sel_name]["plan"]      = cur.get("plan", "")
                                _save_json(META_FILE, meta)

                                # fetch usage — use --id when saved, plain status when not
                                # IMPORTANT: for the ACTIVE profile always use the
                                # bare `status --json` (no --id).  That reads the LIVE
                                # ~/.codex/auth.json — the true current token.  Using
                                # --id would read codex-profiles' STORED copy, which can
                                # be a stale/dead snapshot and falsely report
                                # 'app_session_terminated' even right after a fresh login.
                                rc3, out3, _ = run_cdp(["status", "--json"])
                                if rc3 == 0 and out3.strip():
                                    usage_data = json.loads(out3)
                        except Exception:
                            pass
                elif target and not target.is_active:
                    # SAFETY: do NOT make a live API call for a non-active account.
                    #
                    # The only way to query a non-active account's usage is with its
                    # token — and if that token's short-lived access JWT has expired,
                    # the query triggers a REFRESH that rotates the refresh token
                    # server-side.  Because aisw and codex-profiles keep SEPARATE
                    # token stores, that rotation desyncs aisw's copy → the account
                    # dies on the next switch.  In other words, "refresh to peek at a
                    # non-active account" could silently kill it.
                    #
                    # Instead we show the last CACHED usage (captured while it WAS
                    # active) plus a local read of the aisw access-token expiry — no
                    # network call, no rotation, no risk.  Live usage is shown the
                    # moment you switch to the account.
                    usage_data = None  # signal: use cache + token-health only

            return {"profs": profs, "active": active,
                    "usage": usage_data, "sel_name": sel_name,
                    "non_active": bool(sel_name and target and not target.is_active)}

        def done(r: dict) -> None:
            self.meta = _load_json(META_FILE)
            self.profiles = r["profs"]

            # Merge usage into the selected profile object
            sel_name_ = r["sel_name"]
            usage     = r["usage"]

            # Record whether the selected profile's token is dead (needs re-login)
            if sel_name_ and usage is not None:
                dead = _status_is_dead_token(usage)
                meta = _load_json(META_FILE)
                meta.setdefault(sel_name_, {})["stale"] = dead
                _save_json(META_FILE, meta)
                self.meta = meta

            if sel_name_ and usage:
                for p in self.profiles:
                    if p.name == sel_name_:
                        raw_prof = None
                        if isinstance(usage, dict):
                            if "profiles" in usage and usage["profiles"]:
                                raw_prof = usage["profiles"][0]
                            elif "id" in usage:
                                raw_prof = usage
                        if raw_prof:
                            u = raw_prof.get("usage") or {}
                            p.email = raw_prof.get("email")
                            p.plan  = raw_prof.get("plan")
                            for bkt in u.get("buckets", []) or []:
                                if bkt.get("five_hour") and p.five_hour is None:
                                    p.five_hour = bkt["five_hour"]
                                if bkt.get("weekly") and p.weekly is None:
                                    p.weekly = bkt["weekly"]
                            # update notifier
                            reset_at = (p.five_hour or {}).get("reset_at")
                            self.notifier.update_profile(
                                profile_id=sel_name_,
                                label=p.label,
                                email=p.email,
                                five_hour_reset_at=int(reset_at) if reset_at else None,
                            )
                            # cache it
                            self.usage_cache[sel_name_] = {
                                "refreshed_at": time.time(), "raw": raw_prof,
                                "five_hour": p.five_hour, "weekly": p.weekly,
                            }
                            _save_json(CACHE_FILE, self.usage_cache)
                        break

            # Apply cached usage to ALL profiles (incl. the selected non-active one),
            # filling only fields not already set by the live active check.
            for p in self.profiles:
                if p.name in self.usage_cache:
                    cached = self.usage_cache[p.name]
                    if p.five_hour is None:
                        p.five_hour = cached.get("five_hour")
                    if p.weekly is None:
                        p.weekly = cached.get("weekly")
                meta_entry = self.meta.get(p.name, {})
                if not p.email and meta_entry.get("email"):
                    p.email = meta_entry["email"]
                if not p.plan and meta_entry.get("plan"):
                    p.plan = meta_entry["plan"]

            self._refresh_tree()
            self._check_codex_state_async()
            self._update_next_reset_label()

            sel_p = next((p for p in self.profiles if p.name == sel_name_), None)
            if sel_p:
                self._render_detail(sel_p)
            if r.get("non_active"):
                cached = self.usage_cache.get(sel_name_, {})
                when = cached.get("refreshed_at")
                if when:
                    self.status_var.set(
                        f"'{sel_name_}' is not active — showing CACHED usage "
                        f"(from {fmt_relative(when)}). Switch to it for live usage.")
                else:
                    self.status_var.set(
                        f"'{sel_name_}' is not active — no cached usage yet. "
                        "Switch to it once to capture live usage (safely).")
            elif sel_name_:
                self.status_var.set(
                    f"Refreshed '{sel_name_}' live usage + {len(self.profiles)} profiles.")
            else:
                self.status_var.set(
                    f"{len(self.profiles)} profile(s). Select one & Refresh for usage.")

        self._run_async(work, done, busy_msg="Refreshing…")

    # -----------------------------------------------------------------
    # Profile actions
    # -----------------------------------------------------------------

    def switch_profile(self) -> None:
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile first.")
            return
        if sel.is_active:
            messagebox.showinfo(APP_TITLE, f"'{sel.display_name}' is already active.")
            return

        # Warn if this profile's stored token is known-dead — switching to it will fail.
        if self.meta.get(sel.name, {}).get("stale"):
            if not messagebox.askyesno(
                APP_TITLE,
                f"⚠️  '{sel.display_name}' has a DEAD token (needs re-login).\n\n"
                "Switching to it will fail with 'session has ended'.\n\n"
                "Recommended instead:\n"
                "  1. Click 'Prepare New Login'\n"
                "  2. Log into this account in Codex\n"
                "  3. Click 'Save Current As…' with the same name\n\n"
                "Switch anyway?",
                icon="warning"):
                return

        stop_first = False
        if _is_codex_running():
            if not messagebox.askyesno(
                APP_TITLE,
                "⚠️  Codex is running.\n\n"
                "Switching while Codex is open causes token errors\n"
                "(refresh token reuse / app_session_terminated).\n\n"
                "Stop Codex, switch profiles, then relaunch?\n\n"
                "[Yes] = Stop & Switch    [No] = Cancel",
                icon="warning"):
                return
            stop_first = True

        # The profile we're leaving — its live tokens may have been rotated by Codex
        # since it was last saved; capture them so its snapshot doesn't go stale.
        outgoing = next((p.name for p in self.profiles if p.is_active), None)

        def work() -> str:
            if stop_first:
                _kill_codex()
                # Make sure Codex is FULLY stopped before we touch auth.json,
                # so we never swap credentials under a live process.
                for _ in range(12):
                    if not _is_codex_running():
                        break
                    time.sleep(0.3)

            # ── KEY FIX ──────────────────────────────────────────────────
            # Save the OUTGOING profile's current live tokens back into its aisw
            # store.  OpenAI rotates refresh tokens on every use, so the live
            # auth.json holds the only valid token.  If we don't capture it now,
            # the stored snapshot becomes stale and switching back to this profile
            # later loads a dead token → "session has ended" → kill.
            if outgoing and outgoing != sel.name and _live_matches_profile(outgoing):
                run_aisw(["add", "codex", outgoing, "--from-live", "--yes",
                          "--non-interactive", "--quiet"])

            # Account is CHANGING — wipe ALL heartbeat threads so the new account
            # doesn't try to reconnect the previous account's threads (which kills
            # the new session). Safe: Codex confirmed stopped above.
            _clear_all_heartbeats()

            rc, out, err = run_aisw(["use", "codex", sel.name, "--non-interactive", "--quiet"])
            if rc != 0:
                raise RuntimeError(err or out or "aisw use failed")
            # aisw may re-inject cli_auth_credentials_store under [features] — repair it
            _sanitize_codex_config()
            return out

        def done(_: str) -> None:
            self.refresh()
            if stop_first and self.codex_family_name:
                if messagebox.askyesno(APP_TITLE,
                                       f"Switched to '{sel.display_name}'.\n\nRelaunch Codex now?"):
                    self._do_launch_codex()
            else:
                messagebox.showinfo(APP_TITLE, f"Switched to '{sel.display_name}'.")

        self._run_async(work, done, busy_msg=f"Switching to '{sel.display_name}'…")

    def switch_to_next(self) -> None:
        """Switch to the NEXT profile in the list — fully automatic, no prompts.

        Picks the profile after the current active one (wrapping back to the top
        after the last), then runs the same safe token handling as Switch Profile
        but without the 'Codex is running?' / 'Relaunch?' confirmations:

            fully close Codex → (delay) → switch account → (delay) → relaunch Codex

        Delays let each step settle so we never swap credentials under a live
        process or relaunch before the switch has landed.
        """
        if len(self.profiles) < 2:
            messagebox.showinfo(APP_TITLE, "Need at least two profiles to cycle through.")
            return

        active_idx = next((i for i, p in enumerate(self.profiles) if p.is_active), None)
        target = (self.profiles[0] if active_idx is None
                  else self.profiles[(active_idx + 1) % len(self.profiles)])

        # The profile we're leaving — capture its rotated live tokens before switching.
        outgoing = next((p.name for p in self.profiles if p.is_active), None)
        relaunch = bool(self.codex_family_name)
        target_name, target_display = target.name, target.display_name

        def work() -> str:
            # 1 — fully close Codex, then wait for it to actually exit
            if _is_codex_running():
                _kill_codex()
                for _ in range(12):
                    if not _is_codex_running():
                        break
                    time.sleep(0.3)
            time.sleep(2)   # settle

            # 2 — switch account (same safe handling as Switch Profile)
            #   • save outgoing profile's live (rotated) tokens back to its store
            #   • wipe heartbeat threads so the new account doesn't reconnect old ones
            #   • aisw use → switch; then repair config.toml if aisw misplaced the key
            if outgoing and outgoing != target_name and _live_matches_profile(outgoing):
                run_aisw(["add", "codex", outgoing, "--from-live", "--yes",
                          "--non-interactive", "--quiet"])
            _clear_all_heartbeats()
            rc, out, err = run_aisw(["use", "codex", target_name, "--non-interactive", "--quiet"])
            if rc != 0:
                raise RuntimeError(err or out or "aisw use failed")
            _sanitize_codex_config()
            time.sleep(2)   # settle

            # 3 — relaunch Codex (no prompt)
            if relaunch:
                _clean_codex_state()
                _sanitize_codex_config()
                subprocess.Popen(
                    ["explorer.exe", f"shell:AppsFolder\\{self.codex_family_name}!App"],
                    creationflags=0x08000000)
                time.sleep(2)   # let it come up
            return target_display

        def done(name: str) -> None:
            self._check_codex_state_async()
            self.refresh()
            if relaunch:
                self.status_var.set(f"Switched to '{name}' and relaunched Codex.")
                # Codex re-serializes config.toml a few seconds after startup; re-sanitize
                # then in case it relocates the auth-store key back under [features].
                for delay in (4000, 8000, 15000):
                    self.root.after(delay,
                        lambda: threading.Thread(target=_sanitize_codex_config, daemon=True).start())
            else:
                self.status_var.set(f"Switched to '{name}'. (Codex app not found to relaunch.)")

        self._run_async(work, done, busy_msg=f"Switching to next account '{target_display}'…")

    def save_current(self) -> None:
        """Capture live Codex credentials as a new aisw profile."""
        name = simpledialog.askstring(
            APP_TITLE,
            "Profile name (letters, numbers, hyphens, underscores — max 32):\n\n"
            "Log in to the account you want to save in Codex first,\n"
            "then click OK — aisw captures those live credentials.",
            parent=self.root,
        )
        if not name:
            return
        name = name.strip()
        if not _valid_name(name):
            messagebox.showerror(APP_TITLE, "Invalid name. Use only A-Z, 0-9, - and _ (max 32 chars).")
            return

        def work() -> str:
            rc, out, err = run_aisw(["add", "codex", name,
                                     "--from-live", "--yes",
                                     "--non-interactive", "--quiet"])
            if rc != 0:
                raise RuntimeError(err or out or "aisw add failed")
            # Capture email from current auth state and store in meta
            try:
                rc2, out2, _ = run_cdp(["list", "--json"])
                if rc2 == 0:
                    cur = next((p for p in json.loads(out2).get("profiles", [])
                                if p.get("is_current")), None)
                    if cur:
                        meta = _load_json(META_FILE)
                        meta[name] = {
                            "email":    cur.get("email", ""),
                            "codex_id": cur.get("id", ""),
                            "plan":     cur.get("plan", ""),
                            "stale":    False,   # fresh credentials just captured
                        }
                        _save_json(META_FILE, meta)
            except Exception:
                pass
            return out

        def done(_: str) -> None:
            self.meta = _load_json(META_FILE)
            messagebox.showinfo(APP_TITLE, f"Profile '{name}' saved.")
            self.refresh()

        self._run_async(work, done, busy_msg=f"Saving profile '{name}'…")

    def prepare_new_login(self) -> None:
        """Clear the active Codex auth so the user can log in with a new account.

        Workflow:
          1. This backs up auth.json (reversible).
          2. User launches Codex (or it's launched automatically) and logs in
             with the new account.
          3. User clicks 'Save Current As…' to save the new credentials to aisw.

        Saved aisw profiles are NOT affected — only the live auth.json is cleared.
        """
        if not CODEX_AUTH.exists():
            messagebox.showinfo(
                APP_TITLE,
                "No active Codex login found.\n\n"
                "Launch Codex, log in to the new account, then click\n"
                "'Save Current As…' to save it as an aisw profile.",
            )
            return

        # Warn if active aisw profile hasn't been saved yet
        active = next((p for p in self.profiles if p.is_active), None)
        extra = ""
        if active:
            extra = (
                f"\n\nNote: '{active.display_name}' is your current active profile "
                "and is already saved in aisw — it will be safe."
            )

        if not messagebox.askyesno(
            APP_TITLE,
            "This clears the active Codex login (auth.json) so you can\n"
            "sign in to a different account.\n\n"
            "All saved aisw profiles are preserved — nothing is deleted." +
            extra +
            "\n\nContinue?",
        ):
            return

        def work() -> str:
            # Stop Codex first so it doesn't hold the file open
            if _is_codex_running():
                _kill_codex()
                for _ in range(12):
                    if not _is_codex_running():
                        break
                    time.sleep(0.3)
            # You're about to log into a DIFFERENT account — wipe the old account's
            # heartbeat threads so the fresh login doesn't inherit + reconnect them
            # (cross-account reconnect = the 'session has ended' kill on fresh logins).
            _clear_all_heartbeats()
            backup = CODEX_HOME / f"auth.json.bak.{int(time.time())}"
            try:
                shutil.move(str(CODEX_AUTH), str(backup))
            except OSError as exc:
                raise RuntimeError(f"Could not move auth.json: {exc}")
            return str(backup)

        def done(backup: str) -> None:
            self._check_codex_state_async()
            launch_now = messagebox.askyesno(
                APP_TITLE,
                "Ready for new Codex login.\n\n"
                "Launch Codex now to sign in to the new account?\n\n"
                "After signing in, return here and click 'Save Current As…'\n"
                f"(Previous auth backed up to: …{Path(backup).name})",
            )
            if launch_now and self.codex_family_name:
                self._do_launch_codex()
            self.refresh()

        self._run_async(work, done, busy_msg="Preparing new login…")

    def edit_note(self) -> None:
        """Attach a free-text note to a profile (e.g. the linked phone number).

        Stored in Kalikot's own metadata (aisw-meta.json) — does NOT touch the
        profile name or aisw credentials. Survives renames.
        """
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile first.")
            return
        current = self.meta.get(sel.name, {}).get("note", "")
        note = simpledialog.askstring(
            APP_TITLE,
            f"Note for '{sel.display_name}':\n"
            "(e.g. linked phone +63 9xx…, plan owner, anything you want to remember)",
            initialvalue=current, parent=self.root,
        )
        if note is None:   # cancelled
            return
        meta = _load_json(META_FILE)
        meta.setdefault(sel.name, {})["note"] = note.strip()
        _save_json(META_FILE, meta)
        self.meta = meta
        self._refresh_tree()
        self._on_select()
        self.status_var.set(f"Note updated for '{sel.display_name}'.")

    def rename_profile(self) -> None:
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile to rename.")
            return
        new_name = simpledialog.askstring(APP_TITLE, f"New name for '{sel.name}':",
                                          initialvalue=sel.name, parent=self.root)
        if not new_name:
            return
        new_name = new_name.strip()
        if new_name == sel.name:
            return
        if not _valid_name(new_name):
            messagebox.showerror(APP_TITLE, "Invalid name. Use only A-Z, 0-9, - and _ (max 32).")
            return

        def work() -> str:
            rc, out, err = run_aisw(["rename", "codex", sel.name, new_name, "--non-interactive"])
            if rc != 0:
                raise RuntimeError(err or out or "aisw rename failed")
            # migrate metadata key
            meta = _load_json(META_FILE)
            if sel.name in meta:
                meta[new_name] = meta.pop(sel.name)
                _save_json(META_FILE, meta)
            return out

        def done(_: str) -> None:
            messagebox.showinfo(APP_TITLE, f"Renamed '{sel.name}' → '{new_name}'.")
            self.refresh()

        self._run_async(work, done, busy_msg="Renaming…")

    def delete_profile(self) -> None:
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile to delete.")
            return
        if sel.is_active:
            messagebox.showerror(APP_TITLE,
                                 f"'{sel.display_name}' is currently active.\n"
                                 "Switch to another profile first.")
            return
        if not messagebox.askyesno(APP_TITLE,
                                   f"Delete '{sel.display_name}'? This cannot be undone."):
            return

        def work() -> str:
            rc, out, err = run_aisw(["remove", "codex", sel.name, "--yes", "--non-interactive"])
            if rc != 0:
                raise RuntimeError(err or out or "aisw remove failed")
            meta = _load_json(META_FILE)
            meta.pop(sel.name, None)
            _save_json(META_FILE, meta)
            self.notifier.remove_profile(sel.name)
            return out

        def done(_: str) -> None:
            messagebox.showinfo(APP_TITLE, f"Deleted '{sel.display_name}'.")
            self.refresh()

        self._run_async(work, done, busy_msg="Deleting…")

    # -----------------------------------------------------------------
    # Codex app management
    # -----------------------------------------------------------------

    def _resolve_codex_family(self) -> None:
        self.codex_family_name = _find_codex_family_name()
        self.root.after(0, self._check_codex_state_async)
        self.root.after(3000, self._schedule_codex_check)

    def _schedule_codex_check(self) -> None:
        self._check_codex_state_async()
        self._refresh_cli_status()   # keep CLI status column live
        self.root.after(3000, self._schedule_codex_check)

    def _check_codex_state_async(self) -> None:
        def _check() -> None:
            running = _is_codex_running()
            self.root.after(0, lambda: self._update_codex_ui(running))
        threading.Thread(target=_check, daemon=True).start()

    def _update_codex_ui(self, running: bool) -> None:
        self.codex_status_var.set("Running" if running else "Not Running")
        try:
            self._codex_lbl.configure(foreground="#2e7d32" if running else "#c62828")
        except (tk.TclError, AttributeError):
            pass
        if not self.busy:
            try:
                self.btn_launch.state(["disabled" if running else "!disabled"])
                self.btn_stop.state(["!disabled" if running else "disabled"])
            except (tk.TclError, AttributeError):
                pass

    def launch_cli_session(self) -> None:
        """Open the selected profile as an isolated Codex CLI session in a terminal.

        Each session uses its own CODEX_HOME, so several accounts can run at once
        with zero token conflicts — independent of the GUI Codex app and each other.
        """
        sel = self._selected_profile()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a profile first.")
            return

        aisw_auth = AISW_HOME / "profiles" / "codex" / sel.name / "auth.json"
        if not aisw_auth.exists():
            messagebox.showerror(
                APP_TITLE,
                f"No saved credentials found for '{sel.display_name}'.\n\n"
                "Use 'Save Current As…' to capture this account first.")
            return

        # Optional: let the user pick a workspace folder for this session
        workspace = None
        if messagebox.askyesno(
            APP_TITLE,
            f"Launch an isolated Codex CLI session for '{sel.display_name}'?\n\n"
            "This opens a new terminal logged into THIS account only.\n"
            "It runs independently — you can open several accounts at once.\n\n"
            "Pick a workspace folder for this session?\n"
            "[Yes] = choose a folder   [No] = use your home folder"):
            from tkinter import filedialog
            chosen = filedialog.askdirectory(title="Choose workspace folder for this Codex session")
            if chosen:
                workspace = chosen

        # Build a clear display label: name + email (so the terminal title is unambiguous)
        email = sel.email or self.meta.get(sel.name, {}).get("email", "")
        display = f"{sel.name} — {email}" if email else sel.name

        def work() -> str:
            return _launch_cli_session(sel.name, display, workspace)

        def done(msg: str) -> None:
            self.status_var.set(msg)
            # Mark immediately + refresh status column shortly after the window spawns
            self._cli_active.add(sel.name)
            self.root.after(1500, self._refresh_cli_status)
            messagebox.showinfo(
                APP_TITLE,
                f"{msg}\n\n"
                f"The terminal title shows: 'Codex: {display}'\n"
                "Type your request and press Enter to start.\n\n"
                "Tip: open another profile the same way to multitask across accounts.")

        self._run_async(work, done, busy_msg=f"Launching CLI session for '{sel.display_name}'…")

    def launch_codex(self) -> None:
        if not self.codex_family_name:
            messagebox.showerror(APP_TITLE, "Codex app not found. Is it installed from the Store?")
            return
        self._do_launch_codex()

    def _do_launch_codex(self) -> None:
        def work() -> None:
            if _is_codex_running():
                _kill_codex()
            _clean_codex_state()      # ← prevents heartbeat token-refresh storm
            _sanitize_codex_config()  # ← prevents [features] boolean crash
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{self.codex_family_name}!App"],
                creationflags=0x08000000)
            time.sleep(2)

        def done(_: None) -> None:
            self._check_codex_state_async()
            self.status_var.set("Codex launched.")
            # Codex re-serializes config.toml a few seconds after startup; re-sanitize
            # then in case it relocates the auth-store key back under [features].
            for delay in (4000, 8000, 15000):
                self.root.after(delay,
                    lambda: threading.Thread(target=_sanitize_codex_config, daemon=True).start())

        self._run_async(work, done, busy_msg="Launching Codex…")

    def stop_codex(self) -> None:
        if not messagebox.askyesno(APP_TITLE, "Terminate all Codex processes?"):
            return

        def work() -> None:
            _kill_codex()

        def done(_: None) -> None:
            self._check_codex_state_async()
            self.status_var.set("Codex stopped.")

        self._run_async(work, done, busy_msg="Stopping Codex…")

    # -----------------------------------------------------------------
    # Periodic maintenance
    # -----------------------------------------------------------------

    def _periodic_config_sanitize(self) -> None:
        # Cheap config repair every 60s — catches manual Codex launches / external
        # 'aisw use' calls that could relocate the auth-store key under [features].
        threading.Thread(target=_sanitize_codex_config, daemon=True).start()
        self.root.after(60 * 1000, self._periodic_config_sanitize)

    # -----------------------------------------------------------------
    # Token-activity diagnostics
    # -----------------------------------------------------------------

    def _poll_token_activity(self) -> None:
        """Every ~4s, read the LIVE auth.json (read-only) and log any change.

        This captures the exact moment a token rotates or an account swaps, with
        how many Codex processes were alive — so we can finally see *why* a fresh
        session dies (refresh storm, kill-mid-refresh, stale restore, etc.).
        Pure read of auth.json — never writes Codex files.
        """
        def _work() -> None:
            info = _read_auth(CODEX_AUTH)
            fp = info.get("refresh_fp")
            if not fp:
                return
            if fp != self._last_auth_fp:
                prev = self._last_auth_fp
                self._last_auth_fp = fp
                # Skip logging the very first observation (no transition yet)
                if prev is not None:
                    entry = {
                        "ts": int(time.time()),
                        "account": info.get("account_id"),
                        "refresh_fp": fp,
                        "prev_fp": prev,
                        "last_refresh": info.get("last_refresh"),
                        "access_exp": info.get("access_exp"),
                        "codex_running": _is_codex_running(),
                    }
                    try:
                        with open(TOKEN_LOG, "a", encoding="utf-8") as f:
                            f.write(json.dumps(entry) + "\n")
                    except OSError:
                        pass
        threading.Thread(target=_work, daemon=True).start()
        self.root.after(4000, self._poll_token_activity)

    def _read_token_log(self, limit: int = 40) -> list[dict]:
        if not TOKEN_LOG.exists():
            return []
        try:
            lines = TOKEN_LOG.read_text(encoding="utf-8").splitlines()
            out = []
            for ln in lines[-limit:]:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
            return out
        except OSError:
            return []

    def show_session_health(self) -> None:
        """Open a window showing each profile's token health + recent token activity."""
        if self._health_win is not None and tk.Toplevel.winfo_exists(self._health_win):
            self._health_win.lift()
            self._health_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._health_win = win
        win.title(f"{APP_TITLE} — Session Health")
        win.geometry("780x520")
        try: win.iconbitmap(str(ICON_PATH))
        except Exception: pass

        def _on_close() -> None:
            self._health_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

        # ── profile health table ──────────────────────────────────────
        ttk.Label(win, text="Profile token health", font=("Segoe UI", 10, "bold"),
                  padding=(10, 6)).pack(anchor="w")
        cols = ("name", "account", "expires", "lastref", "sync")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=10)
        for c, txt, w in (("name", "Profile", 130), ("account", "Account ID", 110),
                          ("expires", "Access token expires", 160),
                          ("lastref", "Last refresh", 150), ("sync", "Status", 140)):
            tv.heading(c, text=txt)
            tv.column(c, width=w, anchor="w")
        tv.pack(fill="x", padx=10)
        tv.tag_configure("ok", foreground="#2e7d32")
        tv.tag_configure("warn", foreground="#ef6c00")
        tv.tag_configure("bad", foreground="#c62828")

        # ── activity log ──────────────────────────────────────────────
        ttk.Label(win, text="Recent token activity (rotations / account swaps)",
                  font=("Segoe UI", 10, "bold"), padding=(10, 6)).pack(anchor="w")
        log = tk.Text(win, height=10, wrap="none", font=("Consolas", 8), padx=8, pady=6)
        log.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        def _fmt_ts(ts: Optional[int]) -> str:
            if not ts:
                return "—"
            return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")

        def _refresh_health() -> None:
            if self._health_win is None:
                return
            # Profiles table
            tv.delete(*tv.get_children())
            live = _read_auth(CODEX_AUTH)
            live_acct = live.get("account_id")
            live_fp   = live.get("refresh_fp")
            aisw_codex = AISW_HOME / "profiles" / "codex"
            now = time.time()
            for p in self.profiles:
                store = _read_auth(aisw_codex / p.name / "auth.json")
                exp = store.get("access_exp")
                if exp:
                    hrs = (exp - now) / 3600
                    exp_str = datetime.fromtimestamp(exp).strftime("%m-%d %H:%M") + f"  ({int(hrs)}h)"
                else:
                    exp_str = "—"
                is_active = store.get("account_id") and store.get("account_id") == live_acct
                if self.meta.get(p.name, {}).get("stale"):
                    sync, tag = "⚠ DEAD — re-login", "bad"
                elif is_active:
                    sync = "ACTIVE / in-sync" if store.get("refresh_fp") == live_fp else "ACTIVE / DESYNC"
                    tag = "ok" if store.get("refresh_fp") == live_fp else "warn"
                elif exp and (exp - now) < 0:
                    sync, tag = "token expired", "bad"
                else:
                    sync, tag = "stored", ""
                tv.insert("", "end",
                          values=(p.name, store.get("account_id", "—"), exp_str,
                                  (store.get("last_refresh") or "—")[:19], sync),
                          tags=(tag,) if tag else ())

            # Activity log
            log.configure(state="normal")
            log.delete("1.0", "end")
            entries = self._read_token_log(60)
            if not entries:
                log.insert("end", "No token activity recorded yet.\n"
                                  "Leave Kalikot open while you work — rotations will appear here,\n"
                                  "and the line right before a death tells us the cause.\n")
            else:
                log.insert("end", f"{'time':<16}{'account':<14}{'fp':<10}{'codex':<7}note\n")
                log.insert("end", "-" * 70 + "\n")
                for e in entries:
                    note = ""
                    if e.get("prev_fp") and e.get("prev_fp") != e.get("refresh_fp"):
                        note = f"rotated from {e.get('prev_fp')}"
                    running = "yes" if e.get("codex_running") else "no"
                    log.insert("end",
                        f"{_fmt_ts(e.get('ts')):<16}{(e.get('account') or '—'):<14}"
                        f"{(e.get('refresh_fp') or '—'):<10}{running:<7}{note}\n")
            log.configure(state="disabled")

            if self._health_win is not None:
                win.after(3000, _refresh_health)

        _refresh_health()
        ttk.Button(win, text="Open log folder",
                   command=lambda: subprocess.Popen(["explorer.exe", str(CACHE_DIR)])
                   ).pack(side="left", padx=10, pady=(0, 8))
        ttk.Button(win, text="Close", command=_on_close).pack(side="right", padx=10, pady=(0, 8))

    # -----------------------------------------------------------------
    # Doctor
    # -----------------------------------------------------------------

    def run_doctor(self) -> None:
        def work() -> str:
            _, out, err = run_aisw(["doctor"])
            return out + err

        def done(output: str) -> None:
            win = tk.Toplevel(self.root)
            win.title(f"{APP_TITLE} — doctor")
            win.geometry("560x320")
            try: win.iconbitmap(str(ICON_PATH))
            except Exception: pass
            txt = tk.Text(win, wrap="word", font=("Consolas", 9), padx=8, pady=8)
            sb  = ttk.Scrollbar(win, command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            txt.pack(fill="both", expand=True)
            txt.insert("1.0", re.sub(r"\x1b\[[0-9;]*m", "", output))
            txt.configure(state="disabled")
            ttk.Button(win, text="Close", command=win.destroy).pack(pady=6)
            self.status_var.set("Doctor complete.")

        self._run_async(work, done, busy_msg="Running doctor…")

    # -----------------------------------------------------------------
    # Notifier
    # -----------------------------------------------------------------

    def _on_notify_toggle(self, *_: Any) -> None:
        self.notifier.enabled = self.notify_enabled.get()
        self.notifier.save_state()
        self._update_next_reset_label()

    def _check_and_notify(self) -> None:
        due = self.notifier.get_due()
        for entry in due:
            name  = entry.display_name
            title = f"5H Reset Ready — {APP_TITLE}"
            msg   = f"5H limit likely reset for '{name}'. Refresh to confirm."
            send_windows_toast(title, msg)
            self._show_in_app_toast(title, msg)
            self.notifier.mark_notified(entry.profile_id)
            self.status_var.set(f"⏰ 5H reset ready for '{name}' — click Refresh to confirm.")
        self._update_next_reset_label()
        self.root.after(60_000, self._check_and_notify)

    def _update_next_reset_label(self) -> None:
        if not self.notify_enabled.get():
            self.next_reset_var.set("")
            return
        nxt = self.notifier.get_next_reset()
        if nxt:
            name, ts = nxt
            dt   = datetime.fromtimestamp(ts)
            tstr = dt.strftime("%H:%M")
            mins = max(0, int((ts - time.time()) / 60))
            if mins < 60:
                self.next_reset_var.set(f"⏰  Next 5H reset: '{name}' in ~{mins} min (at {tstr})")
            else:
                h, m = divmod(mins, 60)
                self.next_reset_var.set(f"⏰  Next 5H reset: '{name}' in ~{h}h {m}m (at {tstr})")
        else:
            self.next_reset_var.set("")

    def _show_in_app_toast(self, title: str, message: str) -> None:
        try:
            toast = tk.Toplevel()
            toast.withdraw()
            toast.wm_overrideredirect(True)
            toast.attributes("-topmost", True)
            sw, sh = toast.winfo_screenwidth(), toast.winfo_screenheight()
            w, h = 360, 80
            toast.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 60}")
            toast.configure(bg="#1a1a2e")
            tk.Label(toast, text=title, bg="#1a1a2e", fg="white",
                     font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=12, pady=(10, 0))
            tk.Label(toast, text=message, bg="#1a1a2e", fg="#aaaacc",
                     font=("Segoe UI", 9), anchor="w", wraplength=336).pack(fill="x", padx=12)
            toast.deiconify()
            toast.after(7000, toast.destroy)
            toast.bind("<Button-1>", lambda _: toast.destroy())
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Tray / lifecycle
    # -----------------------------------------------------------------

    def _setup_tray(self) -> None:
        if not _TRAY_AVAILABLE:
            self.root.protocol("WM_DELETE_WINDOW", self._quit_app)
            return
        try:
            img = (_PILImage.open(str(ICON_PATH)).convert("RGBA").resize((64, 64))
                   if ICON_PATH.exists()
                   else _PILImage.new("RGBA", (64, 64), (41, 98, 255, 255)))
            menu = _pystray.Menu(
                _pystray.MenuItem("Show Kalikot", self._tray_show, default=True),
                _pystray.Menu.SEPARATOR,
                _pystray.MenuItem("Quit", self._tray_quit),
            )
            self._tray = _pystray.Icon("KalikotAISW", img, APP_TITLE, menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
            self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        except Exception:
            self.root.protocol("WM_DELETE_WINDOW", self._quit_app)

    def _hide_to_tray(self) -> None:
        self.root.withdraw()

    def _start_ipc_listener(self) -> None:
        def _listen() -> None:
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.settimeout(1.0)
                srv.bind((IPC_HOST, IPC_PORT))
                srv.listen(1)
            except OSError:
                return
            try:
                while not self._ipc_stop.is_set():
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        if conn.recv(64).strip() == b"SHOW_WINDOW":
                            self.root.after(0, self._show_from_tray)
                    except OSError:
                        pass
                    finally:
                        try: conn.close()
                        except OSError: pass
            finally:
                try: srv.close()
                except OSError: pass
        threading.Thread(target=_listen, daemon=True, name="ipc-listener").start()

    def _tray_show(self, *_: Any) -> None:
        self.root.after(0, self._show_from_tray)

    def _show_from_tray(self) -> None:
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.root.after(100, lambda: self.root.attributes("-topmost", False))

    def _tray_quit(self, *_: Any) -> None:
        self.root.after(0, self._quit_app)

    def _quit_app(self) -> None:
        self.notifier.save_state()
        self._ipc_stop.set()
        if self._tray:
            try: self._tray.stop()
            except Exception: pass
        if self._mutex_handle and self._mutex_handle > 0 and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Kalikot.AISWProfileSwitcher.v2")
        except Exception:
            pass

    mutex_handle = _try_acquire_mutex()
    if mutex_handle is None and os.name == "nt":
        _send_show_signal()
        sys.exit(0)

    root = tk.Tk()
    ProfileSwitcherApp(root, mutex_handle=mutex_handle if mutex_handle is not None else -1)
    root.mainloop()


if __name__ == "__main__":
    main()
