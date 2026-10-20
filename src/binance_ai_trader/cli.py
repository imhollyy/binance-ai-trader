"""Minimal paper-only command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from binance_ai_trader.config import Settings


def build_parser() -> argparse.ArgumentParser:
    """Build a minimal diagnostic CLI with no trading actions."""

    parser = argparse.ArgumentParser(
        prog="binance-ai-trader", description="Paper-only Binance research scaffold"
    )
    parser.add_argument("--status", action="store_true", help="Print safe runtime status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print runtime status; refuse unsafe configuration."""

    build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"binance-ai-trader | mode={settings.mode.value} | live_trading=disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
