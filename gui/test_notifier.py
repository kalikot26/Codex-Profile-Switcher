"""
Lightweight idempotency & anti-spam tests for the Kalikot reset notifier.

Run from the gui/ directory:
    python test_notifier.py

All tests use an isolated temp state file — your real notifier_state.json
is never touched.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Redirect NOTIFIER_STATE_FILE to a temp location BEFORE importing the module
# so none of these tests ever touch the real state file.
# ---------------------------------------------------------------------------
import notifier as _mod

_tmp_dir = Path(tempfile.mkdtemp())
_tmp_file = _tmp_dir / "test_state.json"
_real_state_file = _mod.NOTIFIER_STATE_FILE
_mod.NOTIFIER_STATE_FILE = _tmp_file

from notifier import ResetNotifier  # noqa: E402 — must come after redirect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS = "  [PASS]"
FAIL = "  [FAIL]"
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = PASS if cond else FAIL
    print(f"    {label}: {status}")
    if not cond:
        _failures.append(label)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_idempotent_same_reset_at() -> None:
    """Clicking Refresh 10× with the same reset_at must produce exactly one entry
    and must never clear or toggle the notified flag."""
    print("\n[1] Repeated Refresh with same reset_at (idempotency)")
    n = ResetNotifier()
    ts = int(time.time()) + 3600  # 1 hour in the future

    for _ in range(10):
        n.update_profile("p1", "Work", "w@example.com", ts)

    check("exactly one entry for p1", len(n.entries) == 1)
    check("reset_at is preserved", n.entries["p1"].five_hour_reset_at == ts)
    check("notified=False (never fired)", not n.entries["p1"].five_hour_notified)
    check("state file exists (was written once)", _tmp_file.exists())


def test_new_future_reset_clears_notified() -> None:
    """A genuinely new future reset_at (>5 min from stored) must clear notified
    so the user receives an alert for the new cycle."""
    print("\n[2] New future reset_at clears notified flag")
    n = ResetNotifier()
    old_ts = int(time.time()) + 3600
    n.update_profile("p2", "Personal", "p@example.com", old_ts)
    n.mark_notified("p2")
    check("notified=True after mark_notified", n.entries["p2"].five_hour_notified)

    # New reset cycle: 5 hours later
    new_ts = old_ts + 5 * 3600
    n.update_profile("p2", "Personal", "p@example.com", new_ts)
    check("notified cleared for new cycle", not n.entries["p2"].five_hour_notified)
    check("still only one entry for p2", len(n.entries) == 1)
    check("new reset_at stored", n.entries["p2"].five_hour_reset_at == new_ts)


def test_past_ts_keeps_notified() -> None:
    """After a notification fires, Refresh returning the same (now-past) reset_at
    must NOT clear notified — no re-notification for the same past event."""
    print("\n[3] Past reset_at keeps notified=True (no re-notification)")
    n = ResetNotifier()
    past_ts = int(time.time()) - 60  # 1 minute ago
    n.update_profile("p3", "API", "api@example.com", past_ts)
    n.mark_notified("p3")
    check("notified=True after mark", n.entries["p3"].five_hour_notified)

    # Simulate Refresh returning the same past timestamp 10 more times
    for _ in range(10):
        n.update_profile("p3", "API", "api@example.com", past_ts)
    check("notified stays True", n.entries["p3"].five_hour_notified)
    check("get_due() empty (already notified)", len(n.get_due()) == 0)


def test_past_jitter_keeps_notified() -> None:
    """If the API returns a slightly different past timestamp (clock jitter),
    notified must stay True — no re-notification."""
    print("\n[4] Past reset_at with minor jitter keeps notified=True")
    n = ResetNotifier()
    past_ts = int(time.time()) - 600  # 10 min ago
    n.update_profile("p4", "Jitter", "j@example.com", past_ts)
    n.mark_notified("p4")

    # Jitter: ±30 seconds — both still in the past, but different from stored value
    for delta in (-30, -15, 0, +15, +30):
        n.update_profile("p4", "Jitter", "j@example.com", past_ts + delta)
    check("notified stays True after jitter updates", n.entries["p4"].five_hour_notified)
    check("get_due() empty after jitter", len(n.get_due()) == 0)


def test_get_due_single_fire() -> None:
    """get_due() must return the entry only once; after mark_notified it must be empty
    regardless of how many more times the checker runs."""
    print("\n[5] get_due() + mark_notified() single-fire guarantee")
    n = ResetNotifier()
    past_ts = int(time.time()) - 5
    n.update_profile("p5", "Team", "t@example.com", past_ts)

    due1 = n.get_due()
    check("first get_due() returns 1 entry", len(due1) == 1)
    n.mark_notified("p5")

    for _ in range(20):
        due = n.get_due()
    check("get_due() empty after 20 more calls", len(due) == 0)


def test_multiple_profiles_independent() -> None:
    """Each profile is tracked independently — one notified should not affect others."""
    print("\n[6] Multiple profiles are independent")
    n = ResetNotifier()
    now = int(time.time())
    n.update_profile("pa", "Alpha", "a@example.com", now - 5)    # due
    n.update_profile("pb", "Beta",  "b@example.com", now + 3600) # not due
    n.update_profile("pc", "Gamma", "c@example.com", now - 1)    # due

    due = n.get_due()
    check("2 profiles due (pa + pc, not pb)", len(due) == 2)
    due_ids = {e.profile_id for e in due}
    check("correct profiles in due set", due_ids == {"pa", "pc"})

    n.mark_notified("pa")
    n.mark_notified("pc")
    check("get_due() empty after notifying both", len(n.get_due()) == 0)
    check("pb still tracked, not notified", not n.entries["pb"].five_hour_notified)


def test_profile_deletion() -> None:
    """Deleting a profile must remove its notifier entry completely."""
    print("\n[7] Profile deletion removes notifier entry")
    n = ResetNotifier()
    n.update_profile("p7", "ToDelete", "d@example.com", int(time.time()) + 100)
    check("entry created", "p7" in n.entries)
    n.remove_profile("p7")
    check("entry removed from memory", "p7" not in n.entries)
    check("get_due() empty after removal", len(n.get_due()) == 0)

    # Reload from disk and confirm entry is gone
    n2 = ResetNotifier()
    check("entry absent after reload from disk", "p7" not in n2.entries)


def test_none_reset_at_safe() -> None:
    """None reset_at must not create a due entry or cause errors."""
    print("\n[8] None reset_at is safe")
    n = ResetNotifier()
    n.update_profile("p8", "NoUsage", "n@example.com", None)
    check("entry created with None ts", n.entries["p8"].five_hour_reset_at is None)
    check("get_due() empty for None ts", len(n.get_due()) == 0)
    # Further updates with None should not crash
    for _ in range(5):
        n.update_profile("p8", "NoUsage", "n@example.com", None)
    check("still one entry, still None ts", n.entries["p8"].five_hour_reset_at is None)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 55)
    print("  Kalikot Notifier - Idempotency & Anti-Spam Tests")
    print("=" * 55)

    tests = [
        test_idempotent_same_reset_at,
        test_new_future_reset_clears_notified,
        test_past_ts_keeps_notified,
        test_past_jitter_keeps_notified,
        test_get_due_single_fire,
        test_multiple_profiles_independent,
        test_profile_deletion,
        test_none_reset_at_safe,
    ]

    for t in tests:
        # Each test gets a fresh notifier (temp file wiped between tests)
        _tmp_file.unlink(missing_ok=True)
        try:
            t()
        except Exception as exc:
            print(f"    [FAIL] EXCEPTION: {exc}")
            _failures.append(t.__name__)

    print("\n" + "=" * 55)
    if _failures:
        print(f"  [FAIL] {len(_failures)} test(s) FAILED:")
        for f in _failures:
            print(f"     - {f}")
        sys.exit(1)
    else:
        print(f"  [PASS] All {len(tests)} tests passed.")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    try:
        main()
    finally:
        # Always restore real state file path and clean up temp dir
        _mod.NOTIFIER_STATE_FILE = _real_state_file
        try:
            _tmp_file.unlink(missing_ok=True)
            _tmp_dir.rmdir()
        except Exception:
            pass
