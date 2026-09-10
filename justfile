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

# Tag and push a release for the current pyproject.toml version (bump first, e.g. `uv version 0.2.0`)
release:
    #!/usr/bin/env bash
    set -euo pipefail
    version="$(uv version --short)"
    tag="v${version}"
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$branch" != "main" ]; then
        echo "error: releases must be cut from 'main' (on '$branch')" >&2
        exit 1
    fi
    if [ -n "$(git status --porcelain)" ]; then
        echo "error: working tree is dirty; commit or stash first" >&2
        exit 1
    fi
    git fetch --quiet origin
    if [ -n "$(git rev-list "origin/main..HEAD")" ] || [ -n "$(git rev-list "HEAD..origin/main")" ]; then
        echo "error: local main is not in sync with origin/main" >&2
        exit 1
    fi
    if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
        echo "error: tag ${tag} already exists" >&2
        exit 1
    fi
    echo "Tagging ${tag} at $(git rev-parse --short HEAD) and pushing..."
    git tag -a "${tag}" -m "${tag}"
    git push origin "${tag}"
    echo "Pushed ${tag}. Approve the 'pypi' environment in the Actions run to publish."

