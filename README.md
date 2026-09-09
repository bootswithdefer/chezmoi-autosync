# chezmoi-autosync

Daemon that watches your chezmoi-managed dotfiles for local edits and automatically pushes them to a hostname-based branch for review. Never lose a dotfile change because you forgot to commit.

## How it works

1. On startup, queries `chezmoi managed` to discover which files in `$HOME` chezmoi tracks
2. Watches those files for changes using inotify (via [watchdog](https://github.com/gorakhargosh/watchdog))
3. When a change is detected, debounces for a few seconds (coalesces rapid edits)
4. Runs `chezmoi re-add` to capture the change into the chezmoi source directory
5. Commits and pushes to a branch named `auto/<hostname>` on the remote

You then review and merge the branch at your convenience — the daemon never touches `main` or `master`.

## Install

```bash
uv tool install chezmoi-autosync
```

Or with pip:

```bash
pip install chezmoi-autosync
```

## Usage

```bash
# Start with defaults (watches ~/.local/share/chezmoi, pushes to auto/<hostname>)
chezmoi-autosync

# Custom source directory
chezmoi-autosync --source-dir ~/dotfiles

# Custom branch name
chezmoi-autosync --branch auto/my-laptop

# Custom remote and debounce interval
chezmoi-autosync --remote upstream --debounce 10

# Debug logging
chezmoi-autosync -v
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--source-dir` | `~/.local/share/chezmoi` | Path to chezmoi source directory |
| `--remote` | `origin` | Git remote name |
| `--branch` | `auto/<hostname>` | Branch to push to |
| `--debounce` | `5.0` | Seconds to wait after last change before syncing |
| `-v` / `--verbose` | off | Enable debug logging |

## Systemd user service

A systemd user service unit is included for running chezmoi-autosync as a persistent background service.

### Install the service

```bash
# Copy the unit file
mkdir -p ~/.config/systemd/user
cp systemd/chezmoi-autosync.service ~/.config/systemd/user/

# Or if installed via uv tool install, link from the package:
# The service file expects chezmoi-autosync to be in ~/.local/bin/

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now chezmoi-autosync

# Check status
systemctl --user status chezmoi-autosync

# View logs
journalctl --user -u chezmoi-autosync -f
```

### Override settings

```bash
systemctl --user edit chezmoi-autosync
```

Then add overrides:

```ini
[Service]
ExecStart=
ExecStart=%h/.local/bin/chezmoi-autosync --debounce 10 --branch auto/work-laptop -v
```

## Branch safety

The daemon has multiple layers of protection against accidentally modifying `main` or `master`:

- **Startup validation** — refuses to start if the target branch is `main` or `master`
- **Pre-push validation** — checks the target branch name before every push
- **Local branch check** — verifies the local repo isn't checked out on a protected branch before committing
- **Explicit refspec** — pushes use `HEAD:refs/heads/<branch>` to be explicit about the target
- **Force-push to auto branches** — the `auto/<hostname>` branch is a per-machine scratch branch. The local state is always authoritative, so the daemon force-pushes. This means if the remote branch diverges (e.g., from a prior session or a GitHub edit), the local version wins. This is intentional — the branch exists to capture the latest state of *this machine's* dotfiles.

If any safety check fails, the daemon logs a critical error but stays running — it will retry on the next file change once the issue is resolved.

## Error handling

The daemon is designed to stay alive through transient failures:

- **chezmoi re-add fails** — logged, sync skipped, retries on next change
- **git push fails** — logged (e.g., network down), retries on next change
- **Initial sync fails** — logged, daemon continues watching
- **Watch directory doesn't exist** — skipped with a warning

Only critical startup errors cause the daemon to exit:
- Source directory doesn't exist
- Source directory isn't a git repo
- Target branch is protected (`main`/`master`)
- chezmoi reports no managed files

## Development

```bash
# Clone
git clone https://github.com/bootswithdefer/chezmoi-autosync
cd chezmoi-autosync

# Install dev dependencies
uv sync --group dev

# Run tests
uv run pytest

# Lint and format
uvx ruff check .
uvx ruff format --line-length 160 .

# Type check
uvx ty check
```

## License

MIT
