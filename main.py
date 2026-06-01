"""BTC Trading Bot — Bollinger(40) Mean Reversion.

Запуск:
    python main.py              # live-режим
    python main.py --mode status # показать баланс
    python main.py --poll 30     # опрос каждые 30 секунд

Стратегия: Bollinger Bands (bb=40, std=3.0) + RSI(14) filter
Тикер: BTC
Бот: btc (ArenaGo)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from src.live.runner import create_btc_trader, _load_env_token


def setup_logging(level: str = "INFO") -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"btc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

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
    parser = argparse.ArgumentParser(description="BTC Bot - Bollinger(40) MR")
    parser.add_argument("--mode", choices=["live", "status"], default="live")
    parser.add_argument("--token", default="")
    parser.add_argument("--poll", type=int, default=60,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--cash-usage", type=float, default=0.20,
                        help="Fraction of cash to use per entry, 0..1 (default: 0.20)")
    parser.add_argument("--stop-loss", type=float, default=0.02,
                        help="Stop-loss fraction (default: 0.02)")
    parser.add_argument("--take-profit", type=float, default=0.03,
                        help="Take-profit fraction (default: 0.03)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    env_token, _ = _load_env_token()
    effective_token = args.token or env_token

    if not effective_token:
        logger.error("ArenaGo token is missing. Set ARENA_TOKEN in .env or pass --token.")
        return

    if not 0 < args.cash_usage <= 1:
        logger.error("--cash-usage must be greater than 0 and less than or equal to 1")
        return

    if args.stop_loss <= 0 or args.take_profit <= 0:
        logger.error("--stop-loss and --take-profit must be positive fractions")
        return

    # Status command
    if args.mode == "status":
        from src.live.broker import ArenaGoBroker
        broker = ArenaGoBroker(token=effective_token, bot_name="btc")
        try:
            print(f"\n  {broker.summary()}")
            positions = broker.get_positions()
            if positions:
                for ticker, pos in positions.items():
                    print(f"    {ticker}: {pos.quantity:+d} lots @ {pos.avg_price:.2f}")
            else:
                print("    No open positions")
        except Exception as e:
            print(f"  Error: {e}")
        return

    logger.info("Starting BTC Bot - Bollinger(40) bb_std=3.0")
    trader = create_btc_trader(
        token=effective_token,
        cash_usage=args.cash_usage,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
    )

    logger.info(f"Strategy: {trader.strategy.describe()}")
    logger.info(f"Ticker: {trader.ticker}")
    logger.info(f"Cash usage: {trader.cash_usage:.0%} of deposit per trade")
    logger.info(f"SL: {trader.stop_loss_pct:.1%}, TP: {trader.take_profit_pct:.1%}")

    try:
        asyncio.run(trader.run(poll_interval_seconds=args.poll))
    except KeyboardInterrupt:
        logger.info("Interrupted. Final status:")
        trader.print_status()


if __name__ == "__main__":
    main()
