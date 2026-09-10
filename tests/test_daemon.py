"""Tests for chezmoi_autosync.daemon."""

from __future__ import annotations

import os
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
    _dry_run_report,
    _get_watch_dirs,
    _ManagedFileHandler,
    _resolve_ref,
    chezmoi_run,
    get_branch_name,
    get_current_branch,
    get_managed_paths,
    git_run,
    has_changes,
    resolve_parent,
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
            env=None,
        )

    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_env_overlays_process_environment(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        git_run(["write-tree"], cwd=tmp_path, env={"GIT_INDEX_FILE": "/tmp/idx"})
        passed_env = mock_run.call_args.kwargs["env"]
        assert passed_env is not None
        assert passed_env["GIT_INDEX_FILE"] == "/tmp/idx"
        # Existing environment is preserved (overlay, not replacement).
        assert "PATH" in passed_env

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
# resolve_parent / _resolve_ref
# ---------------------------------------------------------------------------


class TestResolveParent:
    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_resolve_ref_returns_sha(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="deadbeef\n", stderr="")
        assert _resolve_ref(tmp_path, "refs/heads/auto/x") == "deadbeef"

    @patch("chezmoi_autosync.daemon.subprocess.run")
    def test_resolve_ref_returns_none_when_absent(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        assert _resolve_ref(tmp_path, "refs/heads/auto/x") is None

    # --- parent selection exercised against a real git repo ---

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        }
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env).stdout.strip()

    def _init_repo(self, repo: Path) -> str:
        repo.mkdir(parents=True, exist_ok=True)
        self._git(repo, "init", "-q", "-b", "main")
        (repo / "f").write_text("base\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "base")
        return self._git(repo, "rev-parse", "HEAD")

    def _commit(self, repo: Path, branch: str, content: str, msg: str) -> str:
        self._git(repo, "checkout", "-q", branch)
        (repo / "f").write_text(content)
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", msg)
        return self._git(repo, "rev-parse", "HEAD")

    def test_first_run_roots_on_base(self, tmp_path: Path) -> None:
        base = self._init_repo(tmp_path)
        # No auto branch yet → parent is the base tip.
        assert resolve_parent(tmp_path, "origin", "auto/h") == base

    def test_auto_ahead_of_base_keeps_stacking(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        # auto branch created off main and advanced ahead of it.
        self._git(tmp_path, "checkout", "-q", "-b", "auto/h")
        auto_tip = self._commit(tmp_path, "auto/h", "auto ahead\n", "auto snapshot")
        self._git(tmp_path, "checkout", "-q", "main")
        # base is an ancestor of auto tip → keep stacking on auto tip.
        assert resolve_parent(tmp_path, "origin", "auto/h") == auto_tip

    def test_auto_merged_into_base_reroots(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        self._git(tmp_path, "checkout", "-q", "-b", "auto/h")
        auto_tip = self._commit(tmp_path, "auto/h", "merged\n", "auto snapshot")
        # Fast-forward main to include the auto commit (simulates PR-merge).
        self._git(tmp_path, "checkout", "-q", "main")
        self._git(tmp_path, "merge", "-q", "--ff-only", "auto/h")
        base = self._git(tmp_path, "rev-parse", "HEAD")
        assert base == auto_tip  # ff merge
        # auto tip == base → re-root on base (returns base).
        assert resolve_parent(tmp_path, "origin", "auto/h") == base

    def test_diverged_base_moved_reroots_on_base(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path)
        # auto snapshot on its own branch.
        self._git(tmp_path, "checkout", "-q", "-b", "auto/h")
        self._commit(tmp_path, "auto/h", "auto side\n", "auto snapshot")
        # main advances independently (e.g. dotfiles committed directly to main).
        base = self._commit(tmp_path, "main", "main side\n", "direct commit")
        # Diverged: base not an ancestor of auto tip → re-root on base.
        assert resolve_parent(tmp_path, "origin", "auto/h") == base

    def test_prefers_main_over_master_as_base(self, tmp_path: Path) -> None:
        base = self._init_repo(tmp_path)  # creates 'main'
        # Create a 'master' pointing elsewhere; main must win.
        self._git(tmp_path, "branch", "master", "HEAD")
        assert resolve_parent(tmp_path, "origin", "auto/h") == base


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

    # --- dry-run ---

    @patch("chezmoi_autosync.daemon.resolve_parent", return_value="abcdef1234567890")
    @patch("chezmoi_autosync.daemon._resolve_ref", return_value="abcdef1234567890")
    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_dry_run_makes_no_mutations_and_reports_changes(
        self, mock_chezmoi: MagicMock, mock_git: MagicMock, mock_ref: MagicMock, mock_parent: MagicMock, tmp_path: Path
    ) -> None:
        mock_chezmoi.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=" M .bashrc\n M .vimrc\n", stderr="")
        result = sync_and_push(tmp_path, "origin", "auto/myhost", dry_run=True)
        assert result is True
        # Only the read-only 'chezmoi status' should run — never re-add.
        mock_chezmoi.assert_called_once_with(["status"])
        # No git mutations at all (parent/ref resolution is stubbed out).
        mock_git.assert_not_called()

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_dry_run_returns_false_when_no_pending_changes(self, mock_chezmoi: MagicMock, mock_git: MagicMock, tmp_path: Path) -> None:
        mock_chezmoi.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        result = sync_and_push(tmp_path, "origin", "auto/myhost", dry_run=True)
        assert result is False
        mock_chezmoi.assert_called_once_with(["status"])
        mock_git.assert_not_called()

    def test_dry_run_still_raises_on_protected_target_branch(self, tmp_path: Path) -> None:
        with pytest.raises(BranchSafetyError):
            sync_and_push(tmp_path, "origin", "main", dry_run=True)

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_dry_run_returns_false_when_status_fails(self, mock_chezmoi: MagicMock, mock_git: MagicMock, tmp_path: Path) -> None:
        mock_chezmoi.side_effect = subprocess.CalledProcessError(1, "chezmoi", stderr="boom")
        result = sync_and_push(tmp_path, "origin", "auto/myhost", dry_run=True)
        assert result is False
        mock_git.assert_not_called()

    # --- snapshot commit/push (plumbing) ---

    @staticmethod
    def _git_side_effect(tree: str, parent_tree: str) -> object:
        """Build a git_run side effect returning canned stdout per subcommand."""

        def _run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            sub = args[0]
            out = ""
            if sub == "write-tree":
                out = tree
            elif sub == "rev-parse":
                out = parent_tree
            elif sub == "commit-tree":
                out = "newcommitsha0001"
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out + "\n", stderr="")

        return _run

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.resolve_parent", return_value="parentsha000")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    @patch("chezmoi_autosync.daemon.time.strftime", return_value="2026-09-09T12:00:00-0700")
    def test_commits_and_pushes_with_parent(
        self, mock_time: MagicMock, mock_chezmoi: MagicMock, mock_parent: MagicMock, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        # Tree differs from parent tree → a real change.
        mock_git.side_effect = self._git_side_effect(tree="newtree111", parent_tree="oldtree000")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is True

        # re-add captured edits.
        mock_chezmoi.assert_called_once_with(["re-add"])

        calls = [c[0][0] for c in mock_git.call_args_list]
        # Parent tree read into a temp index, staged, tree written.
        assert ["read-tree", "parentsha000"] in calls
        assert ["add", "-A"] in calls
        assert ["write-tree"] in calls
        # commit-tree with the parent, then update-ref, then force-push the ref.
        assert ["commit-tree", "newtree111", "-m", "auto: 2026-09-09T12:00:00-0700", "-p", "parentsha000"] in calls
        assert ["update-ref", "refs/heads/auto/myhost", "newcommitsha0001"] in calls
        assert ["push", "--force", "origin", "refs/heads/auto/myhost:refs/heads/auto/myhost"] in calls

        # HEAD is never moved: no plain commit and no checkout/switch.
        assert not any(c[:1] == ["commit"] for c in calls)
        assert not any(c[:1] in (["checkout"], ["switch"]) for c in calls)

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.resolve_parent", return_value=None)
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    @patch("chezmoi_autosync.daemon.time.strftime", return_value="2026-09-09T12:00:00-0700")
    def test_first_commit_has_no_parent(
        self, mock_time: MagicMock, mock_chezmoi: MagicMock, mock_parent: MagicMock, mock_git: MagicMock, tmp_path: Path
    ) -> None:
        mock_git.side_effect = self._git_side_effect(tree="firsttree", parent_tree="")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is True

        calls = [c[0][0] for c in mock_git.call_args_list]
        # No parent: no read-tree, no parent-tree rev-parse, commit-tree without -p.
        assert not any(c[:1] == ["read-tree"] for c in calls)
        assert ["commit-tree", "firsttree", "-m", "auto: 2026-09-09T12:00:00-0700"] in calls
        assert ["update-ref", "refs/heads/auto/myhost", "newcommitsha0001"] in calls
        assert ["push", "--force", "origin", "refs/heads/auto/myhost:refs/heads/auto/myhost"] in calls

    @patch("chezmoi_autosync.daemon._resolve_ref", return_value="parentsha000")
    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.resolve_parent", return_value="parentsha000")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_returns_false_when_tree_matches_parent_and_ref_aligned(
        self, mock_chezmoi: MagicMock, mock_parent: MagicMock, mock_git: MagicMock, mock_ref: MagicMock, tmp_path: Path
    ) -> None:
        # write-tree == parent tree AND the auto ref already points at the
        # parent → genuine no-op: no commit, no ref move, no push.
        mock_git.side_effect = self._git_side_effect(tree="sametree", parent_tree="sametree")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is False

        calls = [c[0][0] for c in mock_git.call_args_list]
        assert not any(c[:1] == ["commit-tree"] for c in calls)
        assert not any(c[:1] == ["update-ref"] for c in calls)
        assert not any(c[:1] == ["push"] for c in calls)

    @patch("chezmoi_autosync.daemon._resolve_ref", return_value="stalesibling")
    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.resolve_parent", return_value="parentsha000")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_realigns_stale_sibling_when_tree_matches_parent(
        self, mock_chezmoi: MagicMock, mock_parent: MagicMock, mock_git: MagicMock, mock_ref: MagicMock, tmp_path: Path
    ) -> None:
        # No new content (tree == parent tree) but the auto ref is a stale
        # sibling (!= parent) → realign the ref to the parent and force-push,
        # without producing a new snapshot commit.
        mock_git.side_effect = self._git_side_effect(tree="sametree", parent_tree="sametree")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        # No new snapshot commit → returns False.
        assert result is False

        calls = [c[0][0] for c in mock_git.call_args_list]
        # Ref realigned to the parent, no commit-tree, then force-pushed.
        assert not any(c[:1] == ["commit-tree"] for c in calls)
        assert ["update-ref", "refs/heads/auto/myhost", "parentsha000"] in calls
        assert ["push", "--force", "origin", "refs/heads/auto/myhost:refs/heads/auto/myhost"] in calls

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.resolve_parent", return_value="parentsha000")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_returns_false_when_readd_fails(self, mock_chezmoi: MagicMock, mock_parent: MagicMock, mock_git: MagicMock, tmp_path: Path) -> None:
        mock_chezmoi.side_effect = subprocess.CalledProcessError(1, "chezmoi")
        result = sync_and_push(tmp_path, "origin", "auto/myhost")
        assert result is False
        # No git work happens if re-add fails.
        mock_git.assert_not_called()

    @patch("chezmoi_autosync.daemon.os.unlink")
    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.resolve_parent", return_value="parentsha000")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_temp_index_is_cleaned_up(
        self, mock_chezmoi: MagicMock, mock_parent: MagicMock, mock_git: MagicMock, mock_unlink: MagicMock, tmp_path: Path
    ) -> None:
        mock_git.side_effect = self._git_side_effect(tree="newtree", parent_tree="oldtree")
        sync_and_push(tmp_path, "origin", "auto/myhost")
        # The temp index file was removed exactly once.
        assert mock_unlink.call_count == 1

    @patch("chezmoi_autosync.daemon.git_run")
    @patch("chezmoi_autosync.daemon.resolve_parent", return_value="parentsha000")
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_staging_uses_temp_index_not_user_index(self, mock_chezmoi: MagicMock, mock_parent: MagicMock, mock_git: MagicMock, tmp_path: Path) -> None:
        mock_git.side_effect = self._git_side_effect(tree="newtree", parent_tree="oldtree")
        sync_and_push(tmp_path, "origin", "auto/myhost")

        # Every read-tree/add/write-tree call must carry a GIT_INDEX_FILE env
        # so the user's real index is untouched.
        for call in mock_git.call_args_list:
            sub = call[0][0][0]
            if sub in ("read-tree", "add", "write-tree"):
                env = call.kwargs.get("env") or (call[0][2] if len(call[0]) > 2 else None)
                assert env is not None and "GIT_INDEX_FILE" in env


# ---------------------------------------------------------------------------
# sync_and_push against a real git repo (parent selection + no-op end to end)
# ---------------------------------------------------------------------------


class TestSyncAndPushRealRepo:
    """Exercise the full plumbing path on a real repo with a fake remote.

    chezmoi re-add/status are stubbed (no chezmoi dependency), but git
    write-tree/commit-tree/update-ref/push and parent selection are real.
    """

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        }
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env).stdout.strip()

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a source repo with an 'origin' remote pointing at a bare repo."""
        bare = tmp_path / "remote.git"
        bare.mkdir()
        self._git(bare, "init", "-q", "--bare", "-b", "main")
        src = tmp_path / "src"
        src.mkdir()
        self._git(src, "init", "-q", "-b", "main")
        self._git(src, "remote", "add", "origin", str(bare))
        (src / "dot_gitconfig").write_text("base\n")
        self._git(src, "add", "-A")
        self._git(src, "commit", "-qm", "base")
        self._git(src, "push", "-q", "origin", "main")
        return src, bare

    def test_diverged_base_moved_commits_delta_on_base(self, tmp_path: Path) -> None:
        src, bare = self._setup(tmp_path)
        base_before = self._git(src, "rev-parse", "HEAD")

        # An old auto snapshot diverged from base.
        self._git(src, "update-ref", "refs/heads/auto/h", base_before)
        self._git(src, "checkout", "-q", "auto/h")
        (src / "dot_gitconfig").write_text("old auto\n")
        self._git(src, "add", "-A")
        self._git(src, "commit", "-qm", "old auto snapshot")
        old_auto = self._git(src, "rev-parse", "HEAD")

        # main advances independently; working tree left with new content.
        self._git(src, "checkout", "-q", "main")
        (src / "dot_gitconfig").write_text("main advanced\n")
        self._git(src, "add", "-A")
        self._git(src, "commit", "-qm", "direct main commit")
        base_after = self._git(src, "rev-parse", "HEAD")
        # Leave a working-tree edit distinct from base.
        (src / "dot_gitconfig").write_text("live edit\n")

        with (
            patch("chezmoi_autosync.daemon.chezmoi_run") as mock_chezmoi,
        ):
            mock_chezmoi.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            result = sync_and_push(src, "origin", "auto/h")

        assert result is True
        # New auto tip is parented on the *base* (re-root), not the old auto tip.
        new_auto = self._git(src, "rev-parse", "refs/heads/auto/h")
        parents = self._git(src, "rev-list", "--parents", "-n", "1", new_auto).split()
        assert parents[1] == base_after  # first parent is base
        assert old_auto not in parents
        # Pushed to the remote.
        assert self._git(bare, "rev-parse", "refs/heads/auto/h") == new_auto
        # HEAD/main untouched.
        assert self._git(src, "rev-parse", "main") == base_after
        assert self._git(src, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_diverged_but_worktree_matches_base_is_noop(self, tmp_path: Path) -> None:
        src, _bare = self._setup(tmp_path)
        base0 = self._git(src, "rev-parse", "HEAD")

        # Old auto snapshot diverges from base.
        self._git(src, "update-ref", "refs/heads/auto/h", base0)
        self._git(src, "checkout", "-q", "auto/h")
        (src / "dot_gitconfig").write_text("old auto\n")
        self._git(src, "add", "-A")
        self._git(src, "commit", "-qm", "old auto")
        old_auto_tip = self._git(src, "rev-parse", "refs/heads/auto/h")

        # main advances independently → auto tip and main are now diverged
        # (base is NOT an ancestor of the auto tip's chosen base).
        self._git(src, "checkout", "-q", "main")
        (src / "dot_gitconfig").write_text("main advanced\n")
        self._git(src, "add", "-A")
        self._git(src, "commit", "-qm", "direct main commit")

        # Working tree is left exactly matching the new base tip → no new
        # content to snapshot, but the stale sibling auto branch is realigned
        # onto the base so it stops showing as diverged.
        with patch("chezmoi_autosync.daemon.chezmoi_run") as mock_chezmoi:
            mock_chezmoi.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            result = sync_and_push(src, "origin", "auto/h")

        # No new snapshot commit was produced.
        assert result is False
        base_after = self._git(src, "rev-parse", "main")
        # Auto branch was realigned to the base tip (no longer the stale sibling).
        assert self._git(src, "rev-parse", "refs/heads/auto/h") == base_after
        assert self._git(src, "rev-parse", "refs/heads/auto/h") != old_auto_tip
        # And the realignment was pushed to the remote.
        assert self._git(_bare, "rev-parse", "refs/heads/auto/h") == base_after


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

    @patch("chezmoi_autosync.daemon.sync_and_push", return_value=False)
    def test_do_sync_passes_dry_run_flag(self, mock_sync: MagicMock) -> None:
        handler = _ManagedFileHandler(
            managed_paths={"/home/user/.bashrc"},
            source_dir=Path("/fake/source"),
            remote="origin",
            branch="auto/test",
            debounce_seconds=0.05,
            dry_run=True,
        )
        handler._do_sync()
        mock_sync.assert_called_once_with(Path("/fake/source"), "origin", "auto/test", dry_run=True)

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
# _dry_run_report
# ---------------------------------------------------------------------------


class TestDryRunReport:
    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_reports_pending_and_returns_true(self, mock_chezmoi: MagicMock, tmp_path: Path) -> None:
        mock_chezmoi.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=" M .bashrc\n", stderr="")
        assert _dry_run_report(tmp_path, "origin", "auto/test") is True
        mock_chezmoi.assert_called_once_with(["status"])

    @patch("chezmoi_autosync.daemon.chezmoi_run")
    def test_returns_false_when_no_pending(self, mock_chezmoi: MagicMock, tmp_path: Path) -> None:
        mock_chezmoi.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="   \n", stderr="")
        assert _dry_run_report(tmp_path, "origin", "auto/test") is False

    @patch("chezmoi_autosync.daemon.chezmoi_run", side_effect=subprocess.CalledProcessError(1, "chezmoi", stderr="boom"))
    def test_returns_false_when_status_errors(self, mock_chezmoi: MagicMock, tmp_path: Path) -> None:
        assert _dry_run_report(tmp_path, "origin", "auto/test") is False


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

    # --- run_once ---

    def test_run_once_raises_when_branch_is_protected(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path, branch="main")
        with pytest.raises(BranchSafetyError):
            d.run_once()

    def test_run_once_raises_when_source_dir_missing(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path / "nonexistent", branch="auto/test")
        with pytest.raises(FileNotFoundError, match="Source directory does not exist"):
            d.run_once()

    def test_run_once_raises_when_not_git_repo(self, tmp_path: Path) -> None:
        d = Daemon(source_dir=tmp_path, branch="auto/test")
        with pytest.raises(FileNotFoundError, match="Not a git repository"):
            d.run_once()

    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value=set())
    def test_run_once_raises_when_no_managed_files(self, mock_managed: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        d = Daemon(source_dir=tmp_path, branch="auto/test")
        with pytest.raises(RuntimeError, match="no managed files"):
            d.run_once()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", return_value=True)
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_once_syncs_without_observer(self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        d = Daemon(source_dir=tmp_path, branch="auto/test")

        result = d.run_once()

        assert result is True
        mock_sync.assert_called_once_with(tmp_path, "origin", "auto/test", dry_run=False)
        mock_observer_cls.assert_not_called()

    @patch("chezmoi_autosync.daemon.Observer")
    @patch("chezmoi_autosync.daemon.sync_and_push", return_value=True)
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_once_dry_run_passes_flag_and_no_observer(
        self, mock_managed: MagicMock, mock_sync: MagicMock, mock_observer_cls: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        d = Daemon(source_dir=tmp_path, branch="auto/test", dry_run=True)

        result = d.run_once()

        assert result is True
        mock_sync.assert_called_once_with(tmp_path, "origin", "auto/test", dry_run=True)
        mock_observer_cls.assert_not_called()

    @patch("chezmoi_autosync.daemon.sync_and_push", return_value=False)
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_once_returns_false_when_nothing_to_do(self, mock_managed: MagicMock, mock_sync: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        d = Daemon(source_dir=tmp_path, branch="auto/test")

        assert d.run_once() is False

    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=BranchSafetyError("local on main"))
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_once_propagates_branch_safety_error(self, mock_managed: MagicMock, mock_sync: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        d = Daemon(source_dir=tmp_path, branch="auto/test")
        with pytest.raises(BranchSafetyError):
            d.run_once()

    @patch("chezmoi_autosync.daemon.sync_and_push", side_effect=subprocess.CalledProcessError(1, "git", stderr="push failed"))
    @patch("chezmoi_autosync.daemon.get_managed_paths", return_value={"/home/user/.bashrc"})
    def test_run_once_propagates_git_error(self, mock_managed: MagicMock, mock_sync: MagicMock, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        d = Daemon(source_dir=tmp_path, branch="auto/test")
        with pytest.raises(subprocess.CalledProcessError):
            d.run_once()

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
