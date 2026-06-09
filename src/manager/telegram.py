"""Minimal Telegram Bot API polling controller."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import TelegramConfig

if TYPE_CHECKING:
    from .core import BotManager

logger = logging.getLogger(__name__)


@dataclass
class TelegramController:
    config: TelegramConfig
    manager: "BotManager"
    offset: int = 0
    runtime_targets: set[str] = field(default_factory=set)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def notify(self, text: str) -> None:
        if not self.enabled:
            return
        targets = self.runtime_targets | set(self.config.targets)
        if not targets:
            return
        aiohttp = _aiohttp()
        async with aiohttp.ClientSession() as session:
            for chat_id in targets:
                await self._send_message(session, chat_id, text)

    async def run(self) -> None:
        if not self.enabled:
            logger.info("Telegram disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID or TELEGRAM_ADMIN_IDS")
            return

        logger.info("Starting Telegram polling")
        aiohttp = _aiohttp()
        async with aiohttp.ClientSession() as session:
            while not self.manager.stop_event.is_set():
                try:
                    updates = await self._get_updates(session)
                    for update in updates:
                        self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                        await self._handle_update(session, update)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Telegram polling error: %s", e)
                    await asyncio.sleep(5)

    async def _get_updates(self, session: Any) -> list[dict]:
        aiohttp = _aiohttp()
        url = self._api_url("getUpdates")
        params = {
            "timeout": 20,
            "offset": self.offset,
            "allowed_updates": json.dumps(["message"]),
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {data!r}")
        return list(data.get("result", []))

    async def _handle_update(self, session: Any, update: dict) -> None:
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        text = self._normalize_button_text(text)
        if not text.startswith("/"):
            return
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        chat_id = str(chat.get("id") or "")
        user_id = str(from_user.get("id") or "")

        if not self._authorized(chat_id, user_id):
            if chat_id:
                await self._send_message(session, chat_id, "Access denied")
            return

        if chat_id:
            self.runtime_targets.add(chat_id)

        response = await self._dispatch(text)
        if chat_id and response:
            await self._send_message(session, chat_id, response)

    async def _dispatch(self, text: str) -> str:
        command, arg = self._split_command(text)
        try:
            if command == "/start":
                return self._help_text()
            if command == "/status":
                return await self.manager.status_text()
            if command == "/bots":
                return await self.manager.bots_text()
            if command == "/balance":
                return await self.manager.balance_text()
            if command == "/positions":
                return await self.manager.positions_text()
            if command == "/trades":
                return await self.manager.trades_text()
            if command == "/pause":
                return await self.manager.pause(arg or None)
            if command == "/resume":
                return await self.manager.resume(arg or None)
            return self._help_text()
        except Exception as e:
            logger.exception("Telegram command failed: %s", text)
            return f"Command failed: {e}"

    def _authorized(self, chat_id: str, user_id: str) -> bool:
        if chat_id and chat_id == self.config.chat_id:
            return True
        if user_id and user_id in self.config.admin_ids:
            return True
        if chat_id and chat_id in self.config.admin_ids:
            return True
        return False

    @staticmethod
    def _split_command(text: str) -> tuple[str, str]:
        first, _, rest = text.partition(" ")
        command = first.split("@", 1)[0].lower()
        return command, rest.strip()

    @staticmethod
    def _normalize_button_text(text: str) -> str:
        button_commands = {
            "Start": "/start",
            "Status": "/status",
            "Bots": "/bots",
            "Balance": "/balance",
            "Positions": "/positions",
            "Trades": "/trades",
            "Pause all": "/pause all",
            "Resume all": "/resume all",
        }
        if text in button_commands:
            return button_commands[text]
        if text.startswith("Pause "):
            return "/pause " + text.removeprefix("Pause ").strip()
        if text.startswith("Resume "):
            return "/resume " + text.removeprefix("Resume ").strip()
        return text

    @staticmethod
    def _help_text() -> str:
        return "\n".join(
            [
                "ArenaGo bot manager",
                "/status - manager status",
                "/bots - configured bots",
                "/balance - ArenaGo balances",
                "/positions - open positions",
                "/trades - recent local trade events",
                "/pause [bot|all] - pause loop",
                "/resume [bot|all] - resume loop",
            ]
        )

    async def _send_message(self, session: Any, chat_id: str, text: str) -> None:
        aiohttp = _aiohttp()
        url = self._api_url("sendMessage")
        payload = {
            "chat_id": chat_id,
            "text": self._truncate(text),
            "disable_web_page_preview": True,
            "reply_markup": self._reply_keyboard(),
        }
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
        if not data.get("ok"):
            logger.debug("Telegram sendMessage failed for %s: %r", chat_id, data)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.config.token}/{method}"

    @staticmethod
    def _truncate(text: str, limit: int = 3900) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 20] + "\n...truncated..."

    def _reply_keyboard(self) -> dict:
        rows = [
            [{"text": "Status"}, {"text": "Bots"}],
            [{"text": "Balance"}, {"text": "Positions"}],
            [{"text": "Trades"}],
            [{"text": "Pause all"}, {"text": "Resume all"}],
        ]
        bot_names = list(getattr(self.manager, "bots", {}).keys())
        for name in bot_names:
            rows.append([{"text": f"Pause {name}"}, {"text": f"Resume {name}"}])
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "one_time_keyboard": False,
            "is_persistent": True,
        }


def _aiohttp():
    try:
        import aiohttp
    except ModuleNotFoundError as e:
        raise RuntimeError("aiohttp is required for Telegram polling; install requirements.txt") from e
    return aiohttp
