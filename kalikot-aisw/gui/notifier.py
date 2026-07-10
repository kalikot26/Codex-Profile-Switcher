"""
Reset notification state tracking for Kalikot Profile Switcher.

Handles persistence of per-profile 5H reset targets, deduplication of
notifications, and sending native Windows toast alerts via PowerShell WinRT
(no extra Python packages required).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CACHE_DIR = Path.home() / ".kalikot-profile-switcher"
NOTIFIER_STATE_FILE = CACHE_DIR / "notifier_state.json"

# Seconds difference between old and new reset_at that counts as a fresh reset cycle.
# If the new timestamp is > this far from the old one AND is in the future, we clear the
# "already notified" flag so the user gets notified again for the new cycle.
NEW_CYCLE_THRESHOLD = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class ProfileResetEntry:
    profile_id: str
    label: Optional[str]
    email: Optional[str]
    five_hour_reset_at: Optional[int] = None       # unix timestamp
    five_hour_notified: bool = False
    five_hour_notified_at: Optional[float] = None  # time.time() when notification fired

    @property
    def display_name(self) -> str:
        return self.label or self.email or self.profile_id

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "label": self.label,
            "email": self.email,
            "five_hour_reset_at": self.five_hour_reset_at,
            "five_hour_notified": self.five_hour_notified,
            "five_hour_notified_at": self.five_hour_notified_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileResetEntry":
        return cls(
            profile_id=d.get("profile_id", ""),
            label=d.get("label"),
            email=d.get("email"),
            five_hour_reset_at=d.get("five_hour_reset_at"),
            five_hour_notified=bool(d.get("five_hour_notified", False)),
            five_hour_notified_at=d.get("five_hour_notified_at"),
        )


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


class ResetNotifier:
    """Tracks 5H reset times per profile and prevents duplicate notifications."""

    def __init__(self) -> None:
        self.enabled: bool = True
        self.entries: dict[str, ProfileResetEntry] = {}
        self.load_state()

    # ---- persistence ----

    def load_state(self) -> None:
        try:
            if NOTIFIER_STATE_FILE.exists():
                raw = json.loads(NOTIFIER_STATE_FILE.read_text(encoding="utf-8"))
                self.enabled = bool(raw.get("enabled", True))
                for pid, d in raw.get("profiles", {}).items():
                    self.entries[pid] = ProfileResetEntry.from_dict(d)
        except Exception:
            pass

    def save_state(self) -> None:
        try:
            NOTIFIER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            raw = {
                "enabled": self.enabled,
                "profiles": {pid: e.to_dict() for pid, e in self.entries.items()},
            }
            NOTIFIER_STATE_FILE.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---- update ----

    def update_profile(
        self,
        profile_id: str,
        label: Optional[str],
        email: Optional[str],
        five_hour_reset_at: Optional[int],
    ) -> None:
        """
        Idempotent update — safe to call on every Refresh, however many times.

        Rules (in order of priority):
        1. New profile_id → creates entry with five_hour_notified=False.
        2. Same profile_id + same reset_at → updates metadata only;
           five_hour_notified is never touched.  No disk write if nothing changed.
        3. Same profile_id + new future reset_at (differs by > NEW_CYCLE_THRESHOLD) →
           new reset cycle detected; five_hour_notified cleared so user gets a fresh alert.
        4. Same profile_id + reset_at is in the past (even if different) →
           five_hour_notified never cleared; prevents re-notifying a past event
           that arrived with slight API jitter.
        5. reset_at=None → stored as None; get_due() skips None entries safely.

        save_state() is called only when something actually changed (dirty flag),
        so clicking Refresh 50 times with identical data causes at most one disk write
        (on the first call that creates the entry).
        """
        now = time.time()
        existing = self.entries.get(profile_id)
        dirty = False

        if existing:
            old_ts = existing.five_hour_reset_at
            new_ts = five_hour_reset_at

            # New reset cycle: new timestamp is in the future AND meaningfully
            # different from the stored one.  Only future timestamps trigger this
            # so we never re-notify for a past event with minor API jitter.
            new_cycle = (
                new_ts is not None
                and old_ts is not None
                and new_ts > now
                and abs(new_ts - old_ts) > NEW_CYCLE_THRESHOLD
            )
            if new_cycle:
                existing.five_hour_notified = False
                existing.five_hour_notified_at = None
                dirty = True

            # Field-level dirty checks — avoid writing if nothing changed
            if existing.label != label:
                existing.label = label
                dirty = True
            if existing.email != email:
                existing.email = email
                dirty = True
            if existing.five_hour_reset_at != new_ts:
                existing.five_hour_reset_at = new_ts
                dirty = True
        else:
            self.entries[profile_id] = ProfileResetEntry(
                profile_id=profile_id,
                label=label,
                email=email,
                five_hour_reset_at=five_hour_reset_at,
            )
            dirty = True

        if dirty:
            self.save_state()

    def remove_profile(self, profile_id: str) -> None:
        self.entries.pop(profile_id, None)
        self.save_state()

    # ---- query ----

    def get_due(self) -> list[ProfileResetEntry]:
        """Return entries whose 5H reset time has passed and haven't been notified."""
        if not self.enabled:
            return []
        now = time.time()
        return [
            e for e in self.entries.values()
            if e.five_hour_reset_at
            and not e.five_hour_notified
            and now >= e.five_hour_reset_at
        ]

    def mark_notified(self, profile_id: str) -> None:
        e = self.entries.get(profile_id)
        if e:
            e.five_hour_notified = True
            e.five_hour_notified_at = time.time()
            self.save_state()

    def get_next_reset(self) -> Optional[tuple[str, int]]:
        """
        Return (display_name, reset_at_timestamp) for the soonest upcoming
        un-notified 5H reset, or None if nothing is scheduled.
        """
        now = time.time()
        upcoming = [
            (e.display_name, e.five_hour_reset_at)
            for e in self.entries.values()
            if e.five_hour_reset_at
            and e.five_hour_reset_at > now
            and not e.five_hour_notified
        ]
        if not upcoming:
            return None
        return min(upcoming, key=lambda x: x[1])


# ---------------------------------------------------------------------------
# Windows toast (no extra packages)
# ---------------------------------------------------------------------------


def send_windows_toast(title: str, message: str) -> bool:
    """
    Send a native Windows 10/11 toast notification via PowerShell WinRT.

    Uses the built-in PowerShell AUMID so no app registration is needed.
    The notification appears in the Action Center and works while the app
    is minimised to the tray.

    Returns True if the PowerShell process was spawned successfully.
    This is not a delivery guarantee — if Do Not Disturb is on, Windows
    may suppress the popup but still log it in the Action Center.

    On macOS it sends a native notification via osascript instead.
    """
    if sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 f"display notification {json.dumps(message)} "
                 f"with title {json.dumps(title)}"])
            return True
        except Exception:
            return False
    if os.name != "nt":
        return False
    try:
        # Escape for PowerShell single-quoted strings
        t = title.replace("`", "``").replace("'", "''")
        m = message.replace("`", "``").replace("'", "''")
        # Pre-registered AUMID on every Windows 10/11 machine
        app_id = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
        script = (
            "[void][Windows.UI.Notifications.ToastNotificationManager,"
            "Windows.UI.Notifications,ContentType=WindowsRuntime];"
            "[void][Windows.Data.Xml.Dom.XmlDocument,"
            "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
            "$tpl=[Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
            "$xml=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($tpl);"
            "$xd=[xml]$xml.GetXml();"
            f"$xd.GetElementsByTagName('text')[0].AppendChild($xd.CreateTextNode('{t}'))|Out-Null;"
            f"$xd.GetElementsByTagName('text')[1].AppendChild($xd.CreateTextNode('{m}'))|Out-Null;"
            "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$doc.LoadXml($xd.OuterXml);"
            "$toast=[Windows.UI.Notifications.ToastNotification]::new($doc);"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($toast)"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=0x08000000,
        )
        return True
    except Exception:
        return False
