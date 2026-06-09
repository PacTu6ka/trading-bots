"""Bothost entrypoint for running all managed ArenaGo bots in one process."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from src.manager.config import config_check_lines, load_manager_config, missing_required_env
from src.manager.logging_setup import setup_logging


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ArenaGo multi-bot manager")
    parser.add_argument("--check-config", action="store_true", help="Print sanitized config and exit")
    parser.add_argument("--status", action="store_true", help="Print local manager status and exit")
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    config = load_manager_config(log_level_override=args.log_level)
    setup_logging(
        level=config.log_level,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )

    if args.check_config:
        print("\n".join(config_check_lines(config)))
        return 0

    if args.status:
        print("\n".join(config_check_lines(config)))
        return 0

    missing = missing_required_env(config)
    if missing:
        logger.error("Missing required config: %s", ", ".join(missing))
        logger.error("Run python run_bot_manager.py --check-config to inspect sanitized settings.")
        return 2

    from src.manager.core import BotManager
    from src.manager.telegram import TelegramController

    manager = BotManager(config)
    telegram = TelegramController(config.telegram, manager)
    manager.set_notifier(telegram.notify if telegram.enabled else None)

    telegram_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, manager.stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    await manager.start()
    if telegram.enabled:
        telegram_task = asyncio.create_task(telegram.run(), name="telegram-polling")
    else:
        logger.warning(
            "Telegram disabled. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID or TELEGRAM_ADMIN_IDS."
        )

    try:
        await manager.wait()
    finally:
        await manager.shutdown()
        if telegram_task:
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)

    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
