"""Tests for chezmoi_autosync.daemon."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chezmoi_autosync.daemon import (
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_REMOTE,
    DEFAULT_SOURCE_DIR,
    PROTECTED_BRANCHES,
    BranchSafetyError,
    Daemon,
    _get_watch_dirs,
    _ManagedFileHandler,
    chezmoi_run,
    get_branch_name,
    get_current_branch,
    get_managed_paths,
    git_run,
    has_changes,
    sync_and_push,
    validate_branch,
)

# ---------------------------------------------------------------------------
# get_branch_name
# ---------------------------------------------------------------------------


class TestGetBranchName:
    def test_returns_auto_prefix_with_hostname(self) -> None:
        with patch("chezmoi_autosync.daemon.socket.gethostname", return_value="myhost"):
            assert get_branch_name() == "auto/myhost"

    def test_uses_actual_hostname(self) -> None:
        import socket

        expected = f"auto/{socket.gethostname()}"
        assert get_branch_name() == expected

    def test_branch_is_never_protected(self) -> None:
        """The default branch name should never collide with a protected branch."""
        branch = get_branch_name()
        final = branch.rsplit("/", 1)[-1]
        assert branch not in PROTECTED_BRANCHES
        assert final not in PROTECTED_BRANCHES


# ---------------------------------------------------------------------------
# validate_branch
# ---------------------------------------------------------------------------


class TestValidateBranch:
    def test_allows_auto_prefix(self) -> None:
        validate_branch("auto/myhost")  # should not raise

    def test_allows_arbitrary_non_protected(self) -> None:
        validate_branch("feature/dotfiles")  # should not raise

    def test_rejects_main(self) -> None:
        with pytest.raises(BranchSafetyError, match="protected branch"):
            validate_branch("main")

    def test_rejects_master(self) -> None:
        with pytest.raises(BranchSafetyError, match="protected branch"):
            validate_branch("master")

    def test_rejects_refs_heads_main(self) -> None:
        with pytest.raises(BranchSafetyError, match="protected branch"):
            validate_branch("refs/heads/main")

    def test_rejects_refs_heads_master(self) -> None:
        with pytest.raises(BranchSafetyError, match="protected branch"):
            validate_branch("refs/heads/master")

    def test_rejects_with_whitespace(self) -> None:
        with pytest.raises(BranchSafetyError, match="protected branch"):
            validate_branch("  main  ")

    def test_allows_main_as_prefix(self) -> None:
        validate_branch("main-backup")  # should not raise — "main-backup" != "main"

    def test_allows_branch_containing_main_as_component(self) -> None:
        validate_branch("auto/mainframe")  # should not raise — final component is "mainframe"


# ---------------------------------------------------------------------------
# git_run
# ---------------------------------------------------------------------------


class TestGitRun:
    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_calls_git_with_args(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        git_run(["status", "--porcelain"], cwd=tmp_path)
        mock_run.assert_called_once_with(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_raises_on_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            git_run(["push"], cwd=tmp_path)


# ---------------------------------------------------------------------------
# chezmoi_run
# ---------------------------------------------------------------------------


class TestChezmoiRun:
    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_calls_chezmoi_with_args(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        chezmoi_run(["managed", "--path-style", "absolute"])
        mock_run.assert_called_once_with(
            ["chezmoi", "managed", "--path-style", "absolute"],
            capture_output=True,
            text=True,
            check=True,
        )

    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_raises_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "chezmoi")
        with pytest.raises(subprocess.CalledProcessError):
            chezmoi_run(["re-add"])


# ---------------------------------------------------------------------------
# get_managed_paths
# ---------------------------------------------------------------------------


class TestGetManagedPaths:
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_returns_set_of_paths(self, mock_chezmoi: MagicMock) -> None:
        mock_chezmoi.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="/home/user/.bashrc\n/home/user/.config/git/config\n/home/user/.vimrc\n",
            stderr="",
        )
        result = get_managed_paths()
        assert result == {"/home/user/.bashrc", "/home/user/.config/git/config", "/home/user/.vimrc"}

    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_returns_empty_set_for_no_output(self, mock_chezmoi: MagicMock) -> None:
        mock_chezmoi.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        result = get_managed_paths()
        assert result == set()

    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_strips_whitespace_and_empty_lines(self, mock_chezmoi: MagicMock) -> None:
        mock_chezmoi.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n/home/user/.bashrc\n\n/home/user/.vimrc\n\n",
            stderr="",
        )
        result = get_managed_paths()
        assert result == {"/home/user/.bashrc", "/home/user/.vimrc"}


# ---------------------------------------------------------------------------
# get_current_branch
# ---------------------------------------------------------------------------


class TestGetCurrentBranch:
    @patch("chezmoi_autosync.daemon.git_run")
    def test_returns_branch_name(self, mock_git: MagicMock, tmp_path: Path) -> None:
        mock_git.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="main\n", stderr="")
        assert get_current_branch(tmp_path) == "main"

    @patch("chezmoi_autosync.daemon.git_run")
    def test_returns_head_when_detached(self, mock_git: MagicMock, tmp_path: Path) -> None:
        mock_git.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="HEAD\n", stderr="")
        assert get_current_branch(tmp_path) == "HEAD"

    @patch("chezmoi_autosync.daemon.git_run")
    def test_strips_whitespace(self, mock_git: MagicMock, tmp_path: Path) -> None:
        mock_git.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="  feature/foo  \n", stderr="")
        assert get_current_branch(tmp_path) == "feature/foo"


# ---------------------------------------------------------------------------
# has_changes
# ---------------------------------------------------------------------------


class TestHasChanges:
    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_returns_true_when_changes_exist(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=" M file.txt\n", stderr="")
        assert has_changes(tmp_path) is True

    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_returns_false_when_clean(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        assert has_changes(tmp_path) is False

    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_returns_false_when_only_whitespace(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="   \n  \n", stderr="")
        assert has_changes(tmp_path) is False


# ---------------------------------------------------------------------------
# sync_and_push
# ---------------------------------------------------------------------------


class TestSyncAndPush:
    def test_raises_on_protected_branch_main(self, tmp_path: Path) -> None:
        with pytest.raises(BranchSafetyError):
            sync_and_push(tmp_path, "origin", "main")

    def test_raises_on_protected_branch_master(self, tmp_path: Path) -> None:
        with pytest.raises(BranchSafetyError):
            sync_and_push(tmp_path, "origin", "master")

    @patch("chezmoi_autosync.daemon.get_current_branch", return_value="main")
    @patch("chezmoi_autosync.daemon.validate_branch")  # allow the target branch
    def test_raises_when_local_on_protected_branch(self, mock_validate: MagicMock, mock_branch: MagicMock, tmp_path: Path) -> None:
        with pytest.raises(BranchSafetyError, match="Local repo is checked out on protected branch"):
            sync_and_push(tmp_path, "origin", "auto/myhost")

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.has_changes", return_value=False)
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    @patch("chezmoi_autosync.daemon.get_current_branch", return_value="auto/myhost")
    def test_returns_false_when_no_changes(
        self, mock_branch: MagicMock, mock_chezmoi: MagicMock, mock_has: MagicMock, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is False
        mock_chezmoi.assert_called_once_with(["re-add"])
        mock_git.assert_not_called()

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.subprocess.run")
    @patch("chezmoi_autosync.daemon.has_changes", return_value=True)
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    @patch("chezmoi_autosync.daemon.get_current_branch", return_value="auto/myhost")
    @patch("chezmoi_autosync.daemon.time.strftime", return_value="2026-09-09T12:00:00-0700")
    def test_commits_and_pushes_when_changes(
        self,
        mock_time: MagicMock,
        mock_branch: MagicMock,
        mock_chezmoi: MagicMock,
        mock_has: MagicMock,
        mock_subprocess: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        # git diff --cached --quiet returns non-zero (has staged changes)
        mock_subprocess.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is True
        mock_git.assert_any_call(["add", "-A"], cwd=tmp_path)
        mock_git.assert_any_call(["commit", "-m", "auto: 2026-09-09T12:00:00-0700"], cwd=tmp_path)
        mock_git.assert_any_call(["push", "--force", "origin", "HEAD:refs/heads/auto/myhost"], cwd=tmp_path)

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.subprocess.run")
    @patch("chezmoi_autosync.daemon.has_changes", return_value=True)
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    @patch("chezmoi_autosync.daemon.get_current_branch", return_value="auto/myhost")
    def test_returns_false_when_staging_resolves_to_no_diff(
        self,
        mock_branch: MagicMock,
        mock_chezmoi: MagicMock,
        mock_has: MagicMock,
        mock_subprocess: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        # git diff --cached --quiet returns 0 (no staged changes)
        mock_subprocess.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is False
        mock_git.assert_called_once_with(["add", "-A"], cwd=tmp_path)

    @patch("chezmoi_autosync.daemon.has_changes")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    @patch("chezmoi_autosync.daemon.get_current_branch", return_value="auto/myhost")
    def test_returns_false_when_readd_fails(self, mock_branch: MagicMock, mock_chezmoi: MagicMock, mock_has: MagicMock, tmp_path: Path) -> None:
        mock_chezmoi.side_effect = subprocess.CalledProcessError(1, "chezmoi")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is False
        mock_has.assert_not_called()

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.subprocess.run")
    @patch("chezmoi_autosync.daemon.has_changes", return_value=True)
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    @patch("chezmoi_autosync.daemon.get_current_branch", return_value="auto/myhost")
    def test_push_uses_refs_heads_prefix(
        self,
        mock_branch: MagicMock,
        mock_chezmoi: MagicMock,
        mock_has: MagicMock,
        mock_subprocess: MagicMock,
        mock_git: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        sync_and_push(tmp_path, "origin", "auto/myhost")
        push_calls = [c for c in mock_git.call_args_list if c[0][0][0] == "push"]
        assert len(push_calls) == 1
        assert push_calls[0][0][0] == ["push", "--force", "origin", "HEAD:refs/heads/auto/myhost"]


# ---------------------------------------------------------------------------
# _get_watch_dirs
# ---------------------------------------------------------------------------


class TestGetWatchDirs:
    def test_extracts_parent_directories(self) -> None:
        paths = {"/home/user/.bashrc", "/home/user/.config/git/config", "/home/user/.vimrc"}
        result = _get_watch_dirs(paths)
        assert result == {"/home/user", "/home/user/.config/git"}

    def test_deduplicates_directories(self) -> None:
        paths = {"/home/user/.bashrc", "/home/user/.profile", "/home/user/.vimrc"}
        result = _get_watch_dirs(paths)
        assert result == {"/home/user"}

    def test_empty_paths(self) -> None:
        assert _get_watch_dirs(set()) == set()


# ---------------------------------------------------------------------------
# _ManagedFileHandler
# ---------------------------------------------------------------------------


class TestManagedFileHandler:
    def _make_handler(self, managed_paths: set[str] | None = None, debounce: float = 0.1) -> _ManagedFileHandler:
        return _ManagedFileHandler(
            managed_paths=managed_paths or {"/home/user/.bashrc", "/home/user/.vimrc"},
            source_dir=Path("/fake/source"),
            remote="origin",
            branch="auto/test",
            debounce_seconds=debounce,
        )

    def test_ignores_unmanaged_files(self) -> None:
        handler = self._make_handler()
        event = MagicMock()
        event.src_path = "/home/user/.unmanaged_file"
        with patch.object(handler, "_schedule_sync") as mock_schedule:
            handler.on_any_event(event)
            mock_schedule.assert_not_called()

    def test_triggers_sync_for_managed_files(self) -> None:
        handler = self._make_handler()
        event = MagicMock()
        event.src_path = "/home/user/.bashrc"
        event.event_type = "modified"
        with patch.object(handler, "_schedule_sync") as mock_schedule:
            handler.on_any_event(event)
            mock_schedule.assert_called_once()

    def test_handles_bytes_path(self) -> None:
        handler = self._make_handler()
        event = MagicMock()
        event.src_path = b"/home/user/.bashrc"
        event.event_type = "modified"
        with patch.object(handler, "_schedule_sync") as mock_schedule:
            handler.on_any_event(event)
            mock_schedule.assert_called_once()

    @patch("chezmoi_autosync.daemon.sync_and_push")
    def test_debounce_coalesces_events(self, mock_sync: MagicMock) -> None:
        handler = self._make_handler(debounce=0.2)
        event = MagicMock()
        event.src_path = "/home/user/.bashrc"
        event.event_type = "modified"

        # Fire multiple events rapidly
        handler.on_any_event(event)
        handler.on_any_event(event)
        handler.on_any_event(event)

        # Wait for debounce to fire
        time.sleep(0.5)
        mock_sync.assert_called_once()

    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=subprocess.CalledProcessError(1, "git", stderr="error"))
    def test_do_sync_handles_git_error(self, mock_sync: MagicMock) -> None:
        handler = self._make_handler(debounce=0.05)
        handler._do_sync()  # should not raise

    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=RuntimeError("unexpected"))
    def test_do_sync_handles_unexpected_error(self, mock_sync: MagicMock) -> None:
        handler = self._make_handler(debounce=0.05)
        handler._do_sync()  # should not raise

    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=BranchSafetyError("bad branch"))
    def test_do_sync_handles_branch_safety_error(self, mock_sync: MagicMock) -> None:
        handler = self._make_handler(debounce=0.05)
        handler._do_sync()  # should not raise — logs critical but stays alive

    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=OSError("disk full"))
    def test_do_sync_handles_os_error(self, mock_sync: MagicMock) -> None:
        handler = self._make_handler(debounce=0.05)
        handler._do_sync()  # should not raise

    def test_schedule_sync_cancels_previous_timer(self) -> None:
        handler = self._make_handler(debounce=10.0)  # long debounce so timer doesn't fire
        with patch("chezmoi_autosync.daemon.sync_and_push"):
            handler._schedule_sync()
            first_timer = handler._timer
            assert first_timer is not None

            handler._schedule_sync()
            second_timer = handler._timer
            assert second_timer is not first_timer
            # Clean up
            if handler._timer:
                handler._timer.cancel()


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class TestDaemon:
    def test_defaults(self) -> None:
        with patch("chezmoi_autosync.daemon.get_branch_name", return_value="auto/testhost"):
            d = Daemon()
        assert d.source_dir == DEFAULT_SOURCE_DIR
        assert d.remote == DEFAULT_REMOTE
        assert d.branch == "auto/testhost"
        assert d.debounce_seconds == DEFAULT_DEBOUNCE_SECONDS

    def test_custom_params(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path, remote="upstream", branch="my/branch", debounce_seconds=10.0)
        assert d.source_dir == tmp_path
        assert d.remote == "upstream"
        assert d.branch == "my/branch"
        assert d.debounce_seconds == 10.0

    def test_run_raises_when_branch_is_protected(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path, branch="main")
        with pytest.raises(BranchSafetyError):
            d.run()

    def test_run_raises_when_branch_is_master(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path, branch="master")
        with pytest.raises(BranchSafetyError):
            d.run()

    def test_run_raises_when_source_dir_missing(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path / "nonexistent", branch="auto/test")
        with pytest.raises(FileNotFoundError, match="Source directory does not exist"):
            d.run()

    def test_run_raises_when_not_git_repo(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path, branch="auto/test")
        with pytest.raises(FileNotFoundError, match="Not a git repository"):
            d.run()

    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value=set())
    def test_run_raises_when_no_managed_files(self, mock_managed: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        d = Daemon(source_dir=tmp_path, branch="auto/test")
        with pytest.raises(RuntimeError, match="no managed files"):
            d.run()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", return_value=False)
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_starts_observer_and_stops(self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer

        d = Daemon(source_dir=tmp_path, branch="auto/test", debounce_seconds=0.1)

        def stop_later() -> None:
            time.sleep(0.2)
            d.stop()

        t = threading.Thread(target=stop_later)
        t.start()

        with patch("os.path.isdir", return_value=True):
            d.run()

        t.join()
        mock_observer.start.assert_called_once()
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=subprocess.CalledProcessError(1, "git", stderr="push failed"))
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_handles_initial_sync_failure(self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer

        d = Daemon(source_dir=tmp_path, branch="auto/test")

        def stop_later() -> None:
            time.sleep(0.2)
            d.stop()

        t = threading.Thread(target=stop_later)
        t.start()

        with patch("os.path.isdir", return_value=True):
            d.run()  # should not raise despite initial sync failure

        t.join()
        mock_observer.start.assert_called_once()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=BranchSafetyError("local on main"))
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_handles_initial_branch_safety_error(self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer

        d = Daemon(source_dir=tmp_path, branch="auto/test")

        def stop_later() -> None:
            time.sleep(0.2)
            d.stop()

        t = threading.Thread(target=stop_later)
        t.start()

        with patch("os.path.isdir", return_value=True):
            d.run()  # should not raise — logs critical but continues

        t.join()
        mock_observer.start.assert_called_once()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", return_value=False)
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc", "/home/user/.config/foo/bar"})
    def test_run_watches_correct_directories(self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer

        d = Daemon(source_dir=tmp_path, branch="auto/test")

        def stop_later() -> None:
            time.sleep(0.2)
            d.stop()

        t = threading.Thread(target=stop_later)
        t.start()

        with patch("os.path.isdir", return_value=True):
            d.run()

        t.join()
        schedule_calls = mock_observer.schedule.call_args_list
        watched_dirs = {c[0][1] for c in schedule_calls}
        assert "/home/user" in watched_dirs
        assert "/home/user/.config/foo" in watched_dirs

    def test_stop_sets_event(self) -> None:
        d = Daemon(source_dir=Path("/fake"), branch="auto/test")
        assert not d._stop_event.is_set()
        d.stop()
        assert d._stop_event.is_set()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", return_value=False)
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/nonexistent_dir_abc/.bashrc"})
    def test_run_skips_nonexistent_watch_dirs(self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer

        d = Daemon(source_dir=tmp_path, branch="auto/test")

        def stop_later() -> None:
            time.sleep(0.2)
            d.stop()

        t = threading.Thread(target=stop_later)
        t.start()

        d.run()

        t.join()
        mock_observer.schedule.assert_not_called()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=OSError("network down"))
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_handles_initial_os_error(self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer

        d = Daemon(source_dir=tmp_path, branch="auto/test")

        def stop_later() -> None:
            time.sleep(0.2)
            d.stop()

        t = threading.Thread(target=stop_later)
        t.start()

        with patch("os.path.isdir", return_value=True):
            d.run()  # should not raise

        t.join()
        mock_observer.start.assert_called_once()
