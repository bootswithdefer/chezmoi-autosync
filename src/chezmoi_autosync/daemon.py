"""Core watcher daemon: monitors chezmoi-managed files, re-adds changes, commits and pushes."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import tempfile
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


def git_run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory.

    If env is provided, its entries overlay the current process environment
    for this invocation (used to point git at a temporary index via
    GIT_INDEX_FILE).
    """
    cmd = ["git", *args]
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd)
    run_env = {**os.environ, **env} if env else None
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True, env=run_env)


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


def _resolve_ref(source_dir: Path, ref: str) -> str | None:
    """Resolve a ref to its commit SHA, or None if it does not exist."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=source_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return sha or None


def _is_ancestor(source_dir: Path, maybe_ancestor: str, descendant: str) -> bool:
    """Return True if maybe_ancestor is an ancestor of descendant (or equal)."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
        cwd=source_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _resolve_base_branch(source_dir: Path, remote: str) -> str | None:
    """Resolve the base-branch commit the auto branch should merge back into.

    Resolution order:
    1. A local protected base branch (main, then master).
    2. The target of the remote's default HEAD (e.g. refs/remotes/<remote>/HEAD).
    3. The current HEAD commit.

    Returns the base commit SHA, or None if none can be resolved.
    """
    for name in ("main", "master"):
        sha = _resolve_ref(source_dir, f"refs/heads/{name}")
        if sha:
            return sha

    # Fall back to the remote's default branch, if configured.
    head_ref = subprocess.run(
        ["git", "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"],
        cwd=source_dir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if head_ref:
        sha = _resolve_ref(source_dir, head_ref)
        if sha:
            return sha

    return _resolve_ref(source_dir, "HEAD")


def resolve_parent(source_dir: Path, remote: str, branch: str) -> str | None:
    """Resolve the parent commit for the next auto-branch snapshot.

    The goal is to keep the auto branch mergeable into the base branch while
    never going stale. The rule:

    - Use the auto-branch tip as the parent **only if it is strictly ahead of
      the base branch** (i.e. the base is an ancestor of the auto tip). This is
      the "stacked, un-merged snapshots" case, where a new snapshot should build
      on the previous one.
    - In every other case — no auto branch yet, the auto tip already merged into
      the base, or the auto tip diverged from a base that has since moved
      forward (e.g. dotfiles were committed to main directly) — parent on the
      **base branch tip**. This re-roots the auto branch on the base so it stays
      a clean, mergeable delta instead of a stale sibling.

    The subsequent tree-vs-parent no-op check in sync_and_push then decides
    whether the re-rooted snapshot actually produces a commit: if the working
    tree already matches the base tree, nothing is committed.

    Note: because each snapshot captures the entire current source working tree,
    re-rooting on the base never loses live dotfile state — anything reachable
    only from a discarded auto tip that is not in the working tree is, by
    definition, not the machine's current dotfile state. The auto branch is a
    per-machine scratch branch.

    Returns the parent commit SHA, or None if nothing can be resolved (empty
    repo with no base and no auto branch).
    """
    auto_tip = _resolve_ref(source_dir, f"refs/heads/{branch}") or _resolve_ref(source_dir, f"refs/remotes/{remote}/{branch}")
    base = _resolve_base_branch(source_dir, remote)

    if auto_tip is None:
        # First run for this machine: root on the base branch (or None if empty).
        return base

    if base is None:
        # No base branch resolvable; continue the auto branch.
        return auto_tip

    # Keep stacking on the auto tip only when it is strictly ahead of the base.
    if _is_ancestor(source_dir, base, auto_tip) and base != auto_tip:
        return auto_tip

    # Otherwise (merged, or diverged from a moved-on base): re-root on the base.
    if auto_tip != base:
        logger.info("Re-rooting %s onto base %s (discarding previous auto tip %s).", branch, base[:12], auto_tip[:12])
    return base


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


def _dry_run_report(source_dir: Path, remote: str, branch: str) -> bool:
    """Report pending chezmoi changes without mutating anything.

    Uses the read-only 'chezmoi status' to list targets that would be
    re-added, and logs the commit and force-push that would follow. Runs no
    chezmoi re-add, git add, commit, or push. Returns True if there were
    pending changes, False otherwise.
    """
    try:
        result = chezmoi_run(["status"])
    except subprocess.CalledProcessError as exc:
        logger.error("chezmoi status failed (exit %d): %s", exc.returncode, exc.stderr.strip() if exc.stderr else "no details")
        return False

    pending = result.stdout.strip()
    if not pending:
        logger.info("[dry-run] No pending changes; nothing would be synced.")
        return False

    parent = resolve_parent(source_dir, remote, branch)
    branch_action = "update" if _resolve_ref(source_dir, f"refs/heads/{branch}") else "create"
    parent_desc = parent[:12] if parent else "none (empty repo)"

    logger.info("[dry-run] Would re-add the following chezmoi changes:\n%s", pending)
    logger.info(
        "[dry-run] Would %s branch %s (parent %s) and force-push to %s/%s.",
        branch_action,
        branch,
        parent_desc,
        remote,
        branch,
    )
    return True


def sync_and_push(source_dir: Path, remote: str, branch: str, dry_run: bool = False) -> bool:
    """Re-add changed files via chezmoi, then snapshot and push.

    The snapshot is committed directly onto the ``auto/<hostname>`` branch using
    git plumbing (write-tree + commit-tree + update-ref), so the daemon never
    moves HEAD or creates a commit on the user's current checkout. The source
    repo can therefore stay on ``main`` during normal operation. The first
    auto-branch commit is parented on the current HEAD so the branch shares
    history with the base branch and remains mergeable via a normal PR.

    Returns True if a snapshot was committed and pushed, False if there was
    nothing to do. Raises BranchSafetyError if the target branch is protected.

    When dry_run is True, no mutating command is run (no chezmoi re-add, git
    write-tree, commit-tree, update-ref, or push). Instead, the pending chezmoi
    changes are reported via the read-only 'chezmoi status', and the would-be
    commit and push are logged. Returns True if there were pending changes to
    sync, False otherwise.
    """
    # Safety: re-validate the target every sync cycle in case config changed.
    validate_branch(branch)

    if dry_run:
        return _dry_run_report(source_dir, remote, branch)

    # Re-add all managed files to capture any local edits into the source tree.
    try:
        chezmoi_run(["re-add"])
    except subprocess.CalledProcessError as exc:
        logger.error("chezmoi re-add failed (exit %d): %s", exc.returncode, exc.stderr.strip() if exc.stderr else "no details")
        return False

    parent = resolve_parent(source_dir, remote, branch)

    # Build a tree from the current working-tree state in a temporary index so
    # the user's real index and HEAD are never touched.
    fd, tmp_index = tempfile.mkstemp(prefix="chezmoi-autosync-index-")
    os.close(fd)
    index_env = {"GIT_INDEX_FILE": tmp_index}
    try:
        if parent is not None:
            git_run(["read-tree", parent], cwd=source_dir, env=index_env)
        git_run(["add", "-A"], cwd=source_dir, env=index_env)

        tree = git_run(["write-tree"], cwd=source_dir, env=index_env).stdout.strip()

        # No-op detection: if the tree matches the parent's tree, there is no new
        # content to snapshot. But the auto branch ref may still be a stale
        # sibling of the parent (e.g. the base branch moved on and the auto tip's
        # content is already reproduced on it). In that case realign the auto
        # branch to the parent so it stops showing as diverged; otherwise there
        # is genuinely nothing to do.
        if parent is not None:
            parent_tree = git_run(["rev-parse", f"{parent}^{{tree}}"], cwd=source_dir).stdout.strip()
            if tree == parent_tree:
                current_auto = _resolve_ref(source_dir, f"refs/heads/{branch}")
                if current_auto == parent:
                    logger.debug("No changes since last snapshot; nothing to commit.")
                    return False
                logger.info("Realigning %s to base %s (no content change; clearing stale divergence).", branch, parent[:12])
                git_run(["update-ref", f"refs/heads/{branch}", parent], cwd=source_dir)
                realigned = True
            else:
                realigned = False
        else:
            realigned = False

        if not realigned:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            commit_msg = f"auto: {timestamp}"

            commit_tree_args = ["commit-tree", tree, "-m", commit_msg]
            if parent is not None:
                commit_tree_args += ["-p", parent]
            commit = git_run(commit_tree_args, cwd=source_dir).stdout.strip()

            # Advance the local auto branch ref to the new commit. HEAD never moves.
            git_run(["update-ref", f"refs/heads/{branch}", commit], cwd=source_dir)
            logger.info("Committed snapshot %s to %s: %s", commit[:12], branch, commit_msg)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_index)

    # Force-push to the remote branch. The auto/<hostname> branch is a
    # per-machine scratch branch — the local state is always authoritative.
    # Without --force, divergence (e.g., from a prior session or a GitHub edit)
    # would cause every subsequent push to fail permanently.
    git_run(["push", "--force", remote, f"refs/heads/{branch}:refs/heads/{branch}"], cwd=source_dir)
    logger.info("Pushed to %s/%s", remote, branch)

    return not realigned


class _ManagedFileHandler(FileSystemEventHandler):
    """Filesystem event handler that triggers sync when chezmoi-managed files change."""

    def __init__(self, managed_paths: set[str], source_dir: Path, remote: str, branch: str, debounce_seconds: float, dry_run: bool = False) -> None:
        super().__init__()
        self.managed_paths = managed_paths
        self.source_dir = source_dir
        self.remote = remote
        self.branch = branch
        self.debounce_seconds = debounce_seconds
        self.dry_run = dry_run
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
            sync_and_push(self.source_dir, self.remote, self.branch, dry_run=self.dry_run)
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
        dry_run: bool = False,
    ) -> None:
        self.source_dir = source_dir or DEFAULT_SOURCE_DIR
        self.remote = remote
        self.branch = branch or get_branch_name()
        self.debounce_seconds = debounce_seconds
        self.dry_run = dry_run
        self._observer: Any = None
        self._stop_event = threading.Event()

    def _validate_startup(self) -> set[str]:
        """Run critical startup checks and return the managed paths.

        Raises on critical issues (protected branch, missing directory, not a
        git repo, no managed files) so callers can fail fast.
        """
        validate_branch(self.branch)

        if not self.source_dir.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {self.source_dir}")

        git_dir = self.source_dir / ".git"
        if not git_dir.exists():
            raise FileNotFoundError(f"Not a git repository: {self.source_dir}")

        managed_paths = get_managed_paths()
        if not managed_paths:
            raise RuntimeError("chezmoi reports no managed files. Nothing to watch.")

        return managed_paths

    def run_once(self) -> bool:
        """Perform a single sync-and-push cycle, then return.

        Runs the same critical startup checks as run() and fails fast on the
        same conditions (protected branch, missing directory, not a git repo,
        no managed files). Unlike run(), it does not start the filesystem
        watcher — it performs exactly one sync and exits.

        Returns True if changes were committed and pushed, False if there was
        nothing to do. Propagates BranchSafetyError and subprocess/OS errors
        from the sync so the caller can report a non-zero exit.
        """
        managed_paths = self._validate_startup()
        logger.info(
            "One-shot%s sync of %d managed files, pushing to %s/%s",
            " dry-run" if self.dry_run else "",
            len(managed_paths),
            self.remote,
            self.branch,
        )
        return sync_and_push(self.source_dir, self.remote, self.branch, dry_run=self.dry_run)

    def run(self) -> None:
        """Start watching and block until stopped.

        Raises on startup for critical issues (missing directory, not a git repo,
        protected branch, no managed files). Once the watch loop is running,
        individual sync errors are logged and retried on the next change — the
        daemon stays alive.
        """
        # --- Critical startup checks (fail fast) ---
        managed_paths = self._validate_startup()

        watch_dirs = _get_watch_dirs(managed_paths)
        logger.info(
            "Watching %d managed files across %d directories, pushing to %s/%s (debounce=%.1fs)%s",
            len(managed_paths),
            len(watch_dirs),
            self.remote,
            self.branch,
            self.debounce_seconds,
            " [dry-run]" if self.dry_run else "",
        )

        # --- Initial sync (non-fatal) ---
        try:
            sync_and_push(self.source_dir, self.remote, self.branch, dry_run=self.dry_run)
        except BranchSafetyError:
            logger.critical("BRANCH SAFETY VIOLATION on initial sync — the daemon will continue watching but will not push until the issue is resolved.")
        except subprocess.CalledProcessError as exc:
            logger.error("Initial sync failed (exit %d): %s — will retry on next change", exc.returncode, exc.stderr.strip() if exc.stderr else "no details")
        except OSError as exc:
            logger.error("Initial sync OS error: %s — will retry on next change", exc)

        # --- Watch loop ---
        handler = _ManagedFileHandler(managed_paths, self.source_dir, self.remote, self.branch, self.debounce_seconds, dry_run=self.dry_run)
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
