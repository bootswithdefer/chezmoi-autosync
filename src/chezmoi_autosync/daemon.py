"""Core watcher daemon: monitors chezmoi-managed files, re-adds changes, commits and pushes."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_DIR = Path.home() / ".local" / "share" / "chezmoi"
DEFAULT_DEBOUNCE_SECONDS = 5.0
DEFAULT_REMOTE = "origin"

# Branches that must never be pushed to, regardless of configuration.
PROTECTED_BRANCHES = frozenset({"main", "master"})


class BranchSafetyError(Exception):
    """Raised when an operation would affect a protected branch."""


def get_branch_name() -> str:
    """Return the branch name to push to, based on the machine hostname."""
    return f"auto/{socket.gethostname()}"


def validate_branch(branch: str) -> None:
    """Raise BranchSafetyError if the branch is protected.

    Checks the full branch name and its final component to catch both
    'main' and 'refs/heads/main' style names.
    """
    name = branch.strip()
    final_component = name.rsplit("/", 1)[-1] if "/" in name else name
    if name in PROTECTED_BRANCHES or final_component in PROTECTED_BRANCHES:
        raise BranchSafetyError(f"Refusing to target protected branch: {branch!r}. Use a non-protected branch like 'auto/<hostname>'.")


def git_run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory."""
    cmd = ["git", *args]
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


def chezmoi_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a chezmoi command."""
    cmd = ["chezmoi", *args]
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def get_managed_paths() -> set[str]:
    """Get the set of absolute paths that chezmoi manages."""
    result = chezmoi_run(["managed", "--path-style", "absolute"])
    return {line for line in result.stdout.strip().splitlines() if line}


def get_current_branch(source_dir: Path) -> str:
    """Return the current branch name of the git repo, or empty string if detached."""
    result = git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=source_dir)
    return result.stdout.strip()


def has_changes(source_dir: Path) -> bool:
    """Check if the chezmoi source dir has uncommitted changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=source_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def sync_and_push(source_dir: Path, remote: str, branch: str) -> bool:
    """Re-add changed files via chezmoi, then commit and push.

    Returns True if changes were committed and pushed, False if there was nothing to do.
    Raises BranchSafetyError if the target branch is protected.
    """
    # Safety: re-validate every sync cycle in case something changed
    validate_branch(branch)

    # Safety: verify the local checkout isn't on a protected branch
    current = get_current_branch(source_dir)
    if current in PROTECTED_BRANCHES:
        raise BranchSafetyError(f"Local repo is checked out on protected branch {current!r}. Refusing to commit. Check out a different branch.")

    # Re-add all managed files to capture any local edits
    try:
        chezmoi_run(["re-add"])
    except subprocess.CalledProcessError as exc:
        logger.error("chezmoi re-add failed (exit %d): %s", exc.returncode, exc.stderr.strip() if exc.stderr else "no details")
        return False

    if not has_changes(source_dir):
        logger.debug("No changes to commit.")
        return False

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    commit_msg = f"auto: {timestamp}"

    git_run(["add", "-A"], cwd=source_dir)

    # Re-check after staging — git add -A might resolve to no diff
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=source_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        logger.debug("No staged changes after git add.")
        return False

    git_run(["commit", "-m", commit_msg], cwd=source_dir)
    logger.info("Committed: %s", commit_msg)

    # Force-push to the remote branch. The auto/<hostname> branch is a
    # per-machine scratch branch — the local state is always authoritative.
    # Without --force, divergence (e.g., from a prior session or a GitHub edit)
    # would cause every subsequent push to fail permanently.
    git_run(["push", "--force", remote, f"HEAD:refs/heads/{branch}"], cwd=source_dir)
    logger.info("Pushed to %s/%s", remote, branch)

    return True


class _ManagedFileHandler(FileSystemEventHandler):
    """Filesystem event handler that triggers sync when chezmoi-managed files change."""

    def __init__(self, managed_paths: set[str], source_dir: Path, remote: str, branch: str, debounce_seconds: float) -> None:
        super().__init__()
        self.managed_paths = managed_paths
        self.source_dir = source_dir
        self.remote = remote
        self.branch = branch
        self.debounce_seconds = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _is_managed(self, path: str | bytes) -> bool:
        """Check if the changed path is a chezmoi-managed file."""
        if isinstance(path, bytes):
            path = path.decode("utf-8", errors="replace")
        return path in self.managed_paths

    def on_any_event(self, event: FileSystemEvent) -> None:
        if not self._is_managed(event.src_path):
            return

        logger.debug("Managed file changed: %s %s", event.event_type, event.src_path)
        self._schedule_sync()

    def _schedule_sync(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._do_sync)
            self._timer.daemon = True
            self._timer.start()

    def _do_sync(self) -> None:
        try:
            sync_and_push(self.source_dir, self.remote, self.branch)
        except BranchSafetyError:
            # This is a critical misconfiguration — log loudly but don't crash
            # the daemon. The user needs to fix the config or the repo state.
            logger.critical("BRANCH SAFETY VIOLATION — sync aborted. Fix the branch configuration or local checkout before changes will be pushed.")
        except subprocess.CalledProcessError as exc:
            logger.error("Git operation failed (exit %d): %s", exc.returncode, exc.stderr.strip() if exc.stderr else "no details")
        except OSError as exc:
            logger.error("OS error during sync: %s", exc)
        except Exception:
            logger.exception("Unexpected error during sync")


def _get_watch_dirs(managed_paths: set[str]) -> set[str]:
    """Derive the minimal set of parent directories to watch from managed paths."""
    dirs: set[str] = set()
    for p in managed_paths:
        dirs.add(os.path.dirname(p))
    return dirs


class Daemon:
    """Main daemon that watches chezmoi-managed files and auto-pushes changes."""

    def __init__(
        self,
        source_dir: Path | None = None,
        remote: str = DEFAULT_REMOTE,
        branch: str | None = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self.source_dir = source_dir or DEFAULT_SOURCE_DIR
        self.remote = remote
        self.branch = branch or get_branch_name()
        self.debounce_seconds = debounce_seconds
        self._observer: Any = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Start watching and block until stopped.

        Raises on startup for critical issues (missing directory, not a git repo,
        protected branch, no managed files). Once the watch loop is running,
        individual sync errors are logged and retried on the next change — the
        daemon stays alive.
        """
        # --- Critical startup checks (fail fast) ---
        validate_branch(self.branch)

        if not self.source_dir.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {self.source_dir}")

        git_dir = self.source_dir / ".git"
        if not git_dir.exists():
            raise FileNotFoundError(f"Not a git repository: {self.source_dir}")

        managed_paths = get_managed_paths()
        if not managed_paths:
            raise RuntimeError("chezmoi reports no managed files. Nothing to watch.")

        watch_dirs = _get_watch_dirs(managed_paths)
        logger.info(
            "Watching %d managed files across %d directories, pushing to %s/%s (debounce=%.1fs)",
            len(managed_paths),
            len(watch_dirs),
            self.remote,
            self.branch,
            self.debounce_seconds,
        )

        # --- Initial sync (non-fatal) ---
        try:
            sync_and_push(self.source_dir, self.remote, self.branch)
        except BranchSafetyError:
            logger.critical("BRANCH SAFETY VIOLATION on initial sync — the daemon will continue watching but will not push until the issue is resolved.")
        except subprocess.CalledProcessError as exc:
            logger.error("Initial sync failed (exit %d): %s — will retry on next change", exc.returncode, exc.stderr.strip() if exc.stderr else "no details")
        except OSError as exc:
            logger.error("Initial sync OS error: %s — will retry on next change", exc)

        # --- Watch loop ---
        handler = _ManagedFileHandler(managed_paths, self.source_dir, self.remote, self.branch, self.debounce_seconds)
        self._observer = Observer()
        for d in sorted(watch_dirs):
            if os.path.isdir(d):
                self._observer.schedule(handler, d, recursive=False)
                logger.debug("Watching directory: %s", d)
            else:
                logger.warning("Skipping non-existent directory: %s", d)
        self._observer.start()

        try:
            self._stop_event.wait()
        finally:
            self._observer.stop()
            self._observer.join()

    def stop(self) -> None:
        """Signal the daemon to stop."""
        logger.info("Stopping daemon.")
        self._stop_event.set()
