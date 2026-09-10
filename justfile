# chezmoi-autosync task runner
# Run `just` or `just --list` to see available recipes.

# Show available recipes
default:
    @just --list

# Run the daemon (watch mode) with debug logging
watch *args:
    uv run chezmoi-autosync -v {{ args }}

# Perform a single sync-and-push cycle and exit
once *args:
    uv run chezmoi-autosync --once {{ args }}

# Preview what would be synced without re-adding, committing, or pushing
preview *args:
    uv run chezmoi-autosync --once --dry-run {{ args }}

# Run the test suite
test *args:
    uv run pytest {{ args }}

# Lint with ruff
lint:
    uvx ruff check .

# Format with ruff
fmt:
    uvx ruff format --line-length 160 .

# Check formatting without modifying files
fmt-check:
    uvx ruff format --line-length 160 --check .

# Type-check with ty
typecheck:
    uvx ty check

# Run all checks: lint, format check, type check, tests
check: lint fmt-check typecheck test

# Install as a user tool and enable the systemd user service
install:
    uv tool install --force .
    mkdir -p ~/.config/systemd/user
    cp systemd/chezmoi-autosync.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now chezmoi-autosync
    systemctl --user status --no-pager chezmoi-autosync
