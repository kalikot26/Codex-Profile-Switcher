# Kalikot Profile Switcher — Project Summary

## What This Is

A Windows GUI app for managing multiple OpenAI Codex accounts without getting session-killed. Built in Python/Tkinter, packaged as a standalone EXE via PyInstaller.

Two generations were built:

- **KalikotProfileSwitcher** (`gui/app.py`) — first version, uses `codex-profiles` CLI
- **KalikotAISW** (`kalikot-aisw/gui/app.py`) — current version, uses `aisw` CLI (v0.3.5+)

---

## The Core Problem We Solved

OpenAI Codex uses OAuth refresh token rotation — every token use issues a new refresh token and invalidates the old one. Switching accounts while Codex was running (or without clearing state) caused one of two fatal errors:

1. **"refresh token already used"** — multiple Codex processes or stale tokens fighting over the same auth.json
2. **"app_session_terminated" 400 Bad Request** — the real culprit

### Root Cause of Session Kills

When you switch Codex accounts, the previous account's **heartbeat thread IDs** remain in:
```
~/.codex/.codex-global-state.json
  → electron-persisted-atom-state
    → heartbeat-thread-permissions-by-id
```

On the next Codex launch, it tries to reconnect those threads using the **new account's token**. OpenAI rejects cross-account thread reconnection → instant session terminated.

**Fix:** `_clear_all_heartbeats()` wipes all thread IDs from `.codex-global-state.json` on every account switch and before every new login.

---

## KalikotAISW — Current App

### Key Files

| Path | Purpose |
|------|---------|
| `kalikot-aisw/gui/app.py` | Main app (~1900 lines) |
| `kalikot-aisw/dist/KalikotAISW.exe` | Primary build (deploy here) |
| `D:\My Apps\KalikotAISW.exe` | Secondary deployment |
| `E:\...` | **BACKUP — never overwrite** |
| `~/.kalikot-profile-switcher/aisw-meta.json` | Profile metadata (email, plan, note, codex_id) |
| `~/.kalikot-profile-switcher/token-activity.jsonl` | Token fingerprint change log |

### Features

- **Profile list** with active marker (●), status column (Codex / CLI / ⚠ Re-login), note, masked email
- **Switch profile** — stops Codex first, saves outgoing token, clears heartbeats, runs `aisw use codex <name>`, sanitizes config.toml
- **Launch / Stop Codex** buttons with running-state detection
- **Prepare New Login** — stops Codex, clears heartbeats, backs up auth.json, opens browser OAuth
- **5H usage bar** with countdown ("resets in ~2h 15m") — active profile uses live token, non-active uses cache only
- **Weekly usage bar** with countdown ("resets in ~3d 4h")
- **Note field** per profile (stored in aisw-meta.json)
- **Hide emails** toggle
- **Session Health** window — token expiry, account_id, fingerprint
- **Single-instance guard** — named Windows mutex + IPC on 127.0.0.1:47322
- **config.toml sanitizer** — runs every 60s and at 4/8/15s after Codex launch

### Critical Logic

**Switch profile work function (simplified):**
```python
# 1. Stop Codex if running
_kill_codex(); wait_for_stop()
# 2. Save outgoing account's live token back to its aisw store
run_aisw(["add", "codex", outgoing, "--from-live", "--yes"])
# 3. Wipe ALL heartbeat threads
_clear_all_heartbeats()
# 4. Switch
run_aisw(["use", "codex", new_profile])
# 5. Fix config.toml
_sanitize_codex_config()
```

**Active vs non-active refresh:**
- Active profile: `codex-profiles status --json` (live, no --id) — reads actual live auth.json
- Non-active: cache only — no live API calls (they rotate the refresh token and kill the account)

**config.toml sanitizer:** `aisw` injects `cli_auth_credentials_store = "file"` under `[features]` on every switch. Codex requires all `[features]` values to be boolean → crash. Sanitizer moves it to root level automatically.

---

## KalikotProfileSwitcher — Old App

Uses `codex-profiles` CLI. Still exists at `dist/KalikotProfileSwitcher.exe` (built June 3).

The old `KalikotProfileSwitcher_OLD_MAY26_DO_NOT_USE.exe` was renamed to prevent accidental launch — running both simultaneously caused double token refresh.

Key features added to old app before migration:
- Launch/Stop Codex buttons
- 5H reset notification system (`notifier.py` — ResetNotifier, Windows toast via PowerShell WinRT)
- Anti-spam/idempotency hardening for notifications
- App icon fix (`_resource_path` helper for PyInstaller)
- Single-instance guard (mutex + 127.0.0.1:47321)
- Refresh selected profile only (not all profiles)

---

## Bugs Fixed Along the Way

| Bug | Fix |
|-----|-----|
| Session killed on account switch | `_clear_all_heartbeats()` before every switch |
| 5-min periodic cleanup killing live sessions | Removed periodic `_clean_codex_state()` timer entirely |
| `⚠ Re-login` shown on healthy active profile | Active profile always uses `status --json` (live), never `--id` (stale) |
| Non-active refresh rotating tokens | Disabled live API calls for non-active accounts; show cache only |
| config.toml crash "expected boolean" | `_sanitize_codex_config()` runs every 60s and post-launch |
| Two Kalikot instances running | Renamed old EXE; single-instance mutex guard |
| CLI sessions killing accounts | Isolated CODEX_HOME approach abandoned; user stopped using CLI sessions |
| Unicode crash in test_notifier.py | Replaced emoji with ASCII `[PASS]`/`[FAIL]` |

---

## Safety Constraints (Permanent)

- Do NOT delete profiles
- Do NOT break switching profiles
- Do NOT break existing login/session/profile storage
- Do NOT change Codex auth behavior
- Do NOT expose or print raw tokens/secrets
- Do NOT kill unrelated processes automatically
- IPC socket uses 127.0.0.1 only — no external network
- E: drive is a backup — never write there

---

## Build Command

```powershell
cd C:\Projects\Codex-Profile-Switcher\kalikot-aisw
pyinstaller --onefile --windowed --icon=gui\icon.ico --name KalikotAISW gui\app.py
# Output: dist\KalikotAISW.exe
```

---

## Pending / Future

- **KaliClaude Profile Switcher** — separate app for Claude profiles, Claude-themed design, using `aisw`'s `claude` tool. Requires Claude Code CLI installed and on PATH first.
