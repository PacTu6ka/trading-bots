"""BTC Trend Divergence Bot for ArenaGo.

This is a separate entry point from main.py. It trades the ArenaGo
portfolio named "btc trend" using multi-timeframe divergence signals.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from src.live.trend_runner import DEFAULT_DAILY_LOSS_LIMIT_RUB, create_btc_trend_trader


def load_env_token() -> str:
    env_path = Path(__file__).resolve().parent / ".env"
    token = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ARENA_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return token or os.environ.get("ARENA_TOKEN", "")


def setup_logging(level: str = "INFO") -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"btc_trend_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    fmt = "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.info(f"Logging to {log_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC Trend Divergence Bot")
    parser.add_argument("--mode", choices=["live", "status"], default="live")
    parser.add_argument("--token", default="")
    parser.add_argument("--bot-name", default="btc trend")
    parser.add_argument("--poll", type=int, default=60)
    parser.add_argument("--daily-loss-limit-rub", type=float, default=DEFAULT_DAILY_LOSS_LIMIT_RUB)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    token = args.token or load_env_token()
    if not token:
        logger.error("ArenaGo token is missing. Set ARENA_TOKEN in .env or pass --token.")
        return

    if args.daily_loss_limit_rub <= 0:
        logger.error("--daily-loss-limit-rub must be greater than 0")
        return

    trader = create_btc_trend_trader(
        token=token,
        bot_name=args.bot_name,
        daily_loss_limit_rub=args.daily_loss_limit_rub,
    )

    if args.mode == "status":
        trader.print_status()
        return

    logger.info("Starting BTC Trend Divergence Bot")
    logger.info(f"ArenaGo portfolio: {args.bot_name!r}")
    logger.info(f"Daily loss limit: {args.daily_loss_limit_rub:,.2f} RUB")
    logger.info(f"Profiles: {trader.describe_profiles()}")

    try:
        asyncio.run(trader.run(poll_interval_seconds=args.poll))
    except KeyboardInterrupt:
        logger.info("Interrupted. Final status:")
        trader.print_status()


if __name__ == "__main__":
    main()
