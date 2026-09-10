"""CLI entry point for chezmoi-autosync."""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
from pathlib import Path

from chezmoi_autosync import __version__
from chezmoi_autosync.daemon import DEFAULT_DEBOUNCE_SECONDS, DEFAULT_REMOTE, DEFAULT_SOURCE_DIR, BranchSafetyError, Daemon, get_branch_name


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chezmoi-autosync",
        description="Watch chezmoi source directory and auto-push changes to a hostname-based branch.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"Path to chezmoi source directory (default: {DEFAULT_SOURCE_DIR})",
    )
    parser.add_argument(
        "--remote",
        default=DEFAULT_REMOTE,
        help=f"Git remote name (default: {DEFAULT_REMOTE})",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help=f"Branch to push to (default: auto/<hostname>, e.g. auto/{get_branch_name().split('/', 1)[1]})",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=DEFAULT_DEBOUNCE_SECONDS,
        metavar="SECONDS",
        help=f"Debounce interval in seconds (default: {DEFAULT_DEBOUNCE_SECONDS})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Perform a single sync-and-push cycle and exit, without watching for changes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be synced without running chezmoi re-add, committing, or pushing.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    daemon = Daemon(
        source_dir=args.source_dir,
        remote=args.remote,
        branch=args.branch,
        debounce_seconds=args.debounce,
        dry_run=args.dry_run,
    )

    log = logging.getLogger(__name__)

    if args.once:
        try:
            daemon.run_once()
        except (FileNotFoundError, BranchSafetyError, RuntimeError) as exc:
            log.error("%s", exc)
            sys.exit(1)
        except subprocess.CalledProcessError as exc:
            log.error("git operation failed (exit %d): %s", exc.returncode, exc.stderr.strip() if exc.stderr else "no details")
            sys.exit(1)
        except OSError as exc:
            log.error("%s", exc)
            sys.exit(1)
        return

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("Received signal %d, shutting down.", signum)
        daemon.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        daemon.run()
    except (FileNotFoundError, BranchSafetyError, RuntimeError) as exc:
        log.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        daemon.stop()
