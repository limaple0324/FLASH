"""Verify live SP1 reconnect recognition; clicks require an explicit flag."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from config.path_manager import PathManager


RECONNECT_STATE_FILENAME = "smart_reconnect_state.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the exact Flash window group and recognize reconnect "
            "screens without persisting captures."
        )
    )
    parser.add_argument("--expected-count", type=int, default=14)
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=["Adobe Flash Player"],
    )
    parser.add_argument(
        "--execute-approved-reconnect",
        action="store_true",
        help=(
            "Send only the confirmed client-relative clicks for recognized "
            "screens. Without this flag the command is read-only."
        ),
    )
    parser.add_argument(
        "--watch-seconds",
        type=int,
        default=0,
        help=(
            "Keep one controller alive for this many seconds. This is allowed "
            "only with --execute-approved-reconnect."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.watch_seconds < 0:
        raise SystemExit("--watch-seconds must not be negative")
    if args.watch_seconds and not args.execute_approved_reconnect:
        raise SystemExit(
            "--watch-seconds requires --execute-approved-reconnect"
        )
    paths = PathManager(root=PROJECT_ROOT)
    controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=PROJECT_ROOT / "assets" / "reconnect_reference",
        expected_windows=args.expected_count,
        title_keywords=args.keywords,
        state_path=paths.data_dir() / RECONNECT_STATE_FILENAME,
    )
    if args.execute_approved_reconnect:
        controller.set_execution_enabled(True)
    cycles = 0
    if args.watch_seconds:
        deadline = time.monotonic() + args.watch_seconds
        while True:
            operation = controller.reconnect()
            cycles += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            details = operation.details or {}
            delay = max(1, int(details.get("next_check_seconds", 1)))
            time.sleep(min(delay, remaining))
    else:
        operation = (
            controller.reconnect()
            if args.execute_approved_reconnect
            else controller.check_connection()
        )
        cycles = 1
    payload = dict(operation.details or {})
    payload.update(
        {
            "operation_success": operation.success,
            "operation_code": operation.code,
            "input_sent": bool(payload.get("clicked_windows", 0)),
            "monitor_cycles": cycles,
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    group_valid = (
        payload.get("validated_windows") == args.expected_count
        and payload.get("discovered_windows") == args.expected_count
    )
    recognition_complete = payload.get("unknown_windows") == 0
    if group_valid and recognition_complete:
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
