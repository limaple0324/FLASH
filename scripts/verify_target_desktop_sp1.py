"""Run the secret-safe, read-only FLASH SP1 target-desktop verifier."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from adapters.windows_target_desktop_verifier import TargetDesktopVerifier


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one-to-one identity and non-blank background capture for "
            "every matching Flash window. No input is sent and no image is saved."
        )
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=14,
        help="Required number of matching windows (default: 14).",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        dest="keywords",
        help="Required title keyword; repeat for multiple keywords.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    keywords = args.keywords or ["Adobe Flash Player"]
    try:
        verifier = TargetDesktopVerifier.for_real_windows(
            expected_windows=args.expected_count,
            title_keywords=keywords,
        )
        result = verifier.verify()
        payload = result.to_dict()
    except (OSError, ValueError):
        payload = {
            "passed": False,
            "failure_codes": ["verifier_initialization_failed"],
            "raw_arguments_emitted": False,
            "captured_pixels_persisted": False,
            "input_sent": False,
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
