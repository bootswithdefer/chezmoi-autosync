"""Tests for chezmoi_autosync.cli."""

from __future__ import annotations

import logging
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chezmoi_autosync.cli import _build_parser, main
from chezmoi_autosync.daemon import BranchSafetyError

# ---------------------------------------------------------------------------
# _build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_default_args(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.remote == "origin"
        assert args.branch is None
        assert args.debounce == 5.0
        assert args.verbose is False

    def test_source_dir(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--source-dir", "/tmp/test"])
        assert args.source_dir == Path("/tmp/test")

    def test_remote(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--remote", "upstream"])
        assert args.remote == "upstream"

    def test_branch(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--branch", "my/branch"])
        assert args.branch == "my/branch"

    def test_debounce(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--debounce", "10.5"])
        assert args.debounce == 10.5

    def test_verbose_short(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["-v"])
        assert args.verbose is True

    def test_verbose_long(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--verbose"])
        assert args.verbose is True

    def test_once_default_false(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.once is False

    def test_once_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--once"])
        assert args.once is True

    def test_dry_run_default_false(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.dry_run is False

    def test_dry_run_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit, match="0"):
            parser.parse_args(["--version"])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    @patch("chezmoi_autosync.cli.Daemon")
    def test_creates_daemon_with_defaults(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        main([])

        mock_daemon_cls.assert_called_once()
        kwargs = mock_daemon_cls.call_args[1]
        assert kwargs["remote"] == "origin"
        assert kwargs["branch"] is None
        assert kwargs["debounce_seconds"] == 5.0

    @patch("chezmoi_autosync.cli.Daemon")
    def test_creates_daemon_with_custom_args(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        main(["--source-dir", "/tmp/src", "--remote", "upstream", "--branch", "my/branch", "--debounce", "15.0"])

        kwargs = mock_daemon_cls.call_args[1]
        assert kwargs["source_dir"] == Path("/tmp/src")
        assert kwargs["remote"] == "upstream"
        assert kwargs["branch"] == "my/branch"
        assert kwargs["debounce_seconds"] == 15.0

    @patch("chezmoi_autosync.cli.Daemon")
    def test_dry_run_defaults_false(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        main([])

        assert mock_daemon_cls.call_args[1]["dry_run"] is False

    @patch("chezmoi_autosync.cli.Daemon")
    def test_dry_run_passed_to_daemon(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        main(["--dry-run"])

        assert mock_daemon_cls.call_args[1]["dry_run"] is True

    @patch("chezmoi_autosync.cli.Daemon")
    def test_exits_on_file_not_found(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = FileNotFoundError("Source directory does not exist")

        with pytest.raises(SystemExit, match="1"):
            main([])

    @patch("chezmoi_autosync.cli.Daemon")
    def test_exits_on_branch_safety_error(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = BranchSafetyError("Refusing to target protected branch")

        with pytest.raises(SystemExit, match="1"):
            main([])

    @patch("chezmoi_autosync.cli.Daemon")
    def test_exits_on_runtime_error(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = RuntimeError("chezmoi reports no managed files")

        with pytest.raises(SystemExit, match="1"):
            main([])

    @patch("chezmoi_autosync.cli.Daemon")
    def test_keyboard_interrupt_calls_stop(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        main([])  # should not raise
        mock_daemon.stop.assert_called_once()

    @patch("chezmoi_autosync.cli.Daemon")
    @patch("chezmoi_autosync.cli.signal.signal")
    def test_registers_signal_handlers(self, mock_signal: MagicMock, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        main([])

        registered_signals = {c[0][0] for c in mock_signal.call_args_list}
        assert signal.SIGTERM in registered_signals
        assert signal.SIGINT in registered_signals

    @patch("chezmoi_autosync.cli.Daemon")
    def test_verbose_flag_sets_debug_logging(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        with patch("chezmoi_autosync.cli.logging.basicConfig") as mock_config:
            main(["-v"])
            mock_config.assert_called_once()
            assert mock_config.call_args[1]["level"] == logging.DEBUG

    @patch("chezmoi_autosync.cli.Daemon")
    def test_normal_logging_level(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run.side_effect = KeyboardInterrupt

        with patch("chezmoi_autosync.cli.logging.basicConfig") as mock_config:
            main([])
            assert mock_config.call_args[1]["level"] == logging.INFO


# ---------------------------------------------------------------------------
# main --once
# ---------------------------------------------------------------------------


class TestMainOnce:
    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_calls_run_once_not_run(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.return_value = True

        main(["--once"])

        mock_daemon.run_once.assert_called_once()
        mock_daemon.run.assert_not_called()

    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_dry_run_uses_run_once_and_passes_dry_run(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.return_value = True

        main(["--once", "--dry-run"])

        assert mock_daemon_cls.call_args[1]["dry_run"] is True
        mock_daemon.run_once.assert_called_once()
        mock_daemon.run.assert_not_called()

    @patch("chezmoi_autosync.cli.signal.signal")
    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_does_not_register_signal_handlers(self, mock_daemon_cls: MagicMock, mock_signal: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.return_value = False

        main(["--once"])

        mock_signal.assert_not_called()

    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_returns_cleanly_when_nothing_to_sync(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.return_value = False

        main(["--once"])  # should not raise or exit

    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_exits_on_branch_safety_error(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.side_effect = BranchSafetyError("Refusing to target protected branch")

        with pytest.raises(SystemExit, match="1"):
            main(["--once"])

    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_exits_on_file_not_found(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.side_effect = FileNotFoundError("Source directory does not exist")

        with pytest.raises(SystemExit, match="1"):
            main(["--once"])

    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_exits_on_runtime_error(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.side_effect = RuntimeError("chezmoi reports no managed files")

        with pytest.raises(SystemExit, match="1"):
            main(["--once"])

    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_exits_on_git_error(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.side_effect = subprocess.CalledProcessError(1, "git", stderr="push failed")

        with pytest.raises(SystemExit, match="1"):
            main(["--once"])

    @patch("chezmoi_autosync.cli.Daemon")
    def test_once_exits_on_os_error(self, mock_daemon_cls: MagicMock) -> None:
        mock_daemon = MagicMock()
        mock_daemon_cls.return_value = mock_daemon
        mock_daemon.run_once.side_effect = OSError("network down")

        with pytest.raises(SystemExit, match="1"):
            main(["--once"])
