# Kalikot Profile Switcher (Windows GUI)

A lightweight Tkinter desktop UI that wraps the existing `codex-profiles` CLI
(no Rust rewrite). All profile actions shell out to the CLI, so behavior matches
the proven command-line flow.

## Requirements

- Windows 10/11
- Python 3.10+ (tested with 3.14). Tkinter ships with the standard CPython installer.
- The `codex-profiles` CLI must be installed and on `PATH`
  (`npm install -g codex-profiles` or any other supported method).

## Run (dev mode)

From the repo root:

```cmd
python gui\app.py
```

Or double-click `gui\launch.bat` (runs with `pythonw`, no console window).

## Build a standalone .exe

Requires [PyInstaller](https://pyinstaller.org/) — installed automatically by the script.

```cmd
gui\build.bat
```

Output: `dist\KalikotProfileSwitcher.exe` with `app.ico` embedded.

## Where data lives

- **Codex auth / profiles:** managed by the `codex-profiles` CLI under
  `~/.codex/` and `~/.codex/profiles/` (untouched by this GUI).
- **Per-profile usage cache:** `~/.kalikot-profile-switcher/usage-cache.json`.
  Holds last-known 5h / weekly limit % and reset times so the UI can show them
  before a fresh refresh.
- **Prepare-new-login backups:** when you click *Prepare New Login*, the
  current `~/.codex/auth.json` is moved to `~/.codex/auth.json.bak.<timestamp>`
  rather than deleted, so you can recover it if needed.

## What each button does

| Button              | Maps to CLI                                                          |
| ------------------- | -------------------------------------------------------------------- |
| Switch Profile      | `codex-profiles load --id <id> --force`                              |
| Prepare New Login   | Moves `~/.codex/auth.json` to a timestamped backup (preserves saves) |
| Save Active Login   | `codex-profiles save [--label <name>]`                               |
| Rename              | `codex-profiles label rename --label OLD --to NEW` (or `label set`)  |
| Delete              | `codex-profiles delete --id <id> --yes` (after confirm dialog)       |
| Refresh             | `codex-profiles list --json` + `status --id <id> --json` (cached)    |

The list shows profile label, email, and one of: *Active account*, *Saved login*, *Unknown*.

## Refresh behavior

- Clicking **Refresh** with a profile selected re-fetches usage for that profile
  and writes it to the cache, so the same profile re-shows its last-known
  usage instantly the next time you click on it.
- On first launch, the app fetches usage for *all* profiles once (this can take
  a couple of seconds if you have many accounts).

## Hide emails

Checking *Hide emails* masks the local part in the list view only — saved
profile data on disk is untouched. Example: `cgptplus002@gmail.com` →
`c********002@gmail.com`.

## Known limitations

- "Codex State" is derived from CLI output + presence of `~/.codex/auth.json`.
  It does not (yet) detect a running `codex` process.
- Usage parsing relies on `codex-profiles status --json`. If a future CLI
  release changes that schema, the raw `usage` blob is still kept in cache
  under each profile's `raw` field for fallback.
- `Rename` uses `label rename` when the profile already has a label, otherwise
  it falls back to `label set --id`. Profiles with no label can be assigned one
  this way.
- The GUI requires `codex-profiles` on `PATH`. If the binary isn't found, all
  actions will show a clear "CLI not found" error.

## Safety

- The GUI never edits Codex auth files directly except for *Prepare New Login*,
  which moves the active `auth.json` to a backup (reversible).
- *Delete* always prompts for confirmation and only removes the saved profile
  selected in the list — it never touches the active Codex login config beyond
  what the CLI does.
- The usage cache lives in your home directory; you can delete it any time and
  it will be rebuilt on the next refresh.
