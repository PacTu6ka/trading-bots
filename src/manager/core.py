"""Core multi-bot manager."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from src.live.broker import ArenaGoBroker
from src.live.runner import create_btc_trader
from src.live.trend_runner import create_btc_trend_trader

from .config import BotConfig, ManagerConfig, PROJECT_ROOT

logger = logging.getLogger(__name__)

NotifyFn = Callable[[str], Awaitable[None]]
STATE_DIR = PROJECT_ROOT / "state"
TRADES_DIR = PROJECT_ROOT / "trades"
MANAGER_STATE_PATH = STATE_DIR / "bot_manager_state.json"


@dataclass
class ManagedBot:
    config: BotConfig
    trader: object
    paused: bool = False
    task: asyncio.Task | None = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    last_loop_at: str = "-"
    last_error: str = "-"
    loop_count: int = 0


class BotManager:
    def __init__(self, config: ManagerConfig):
        self.config = config
        self.state = self._load_state()
        self.stop_event = asyncio.Event()
        self.notifier: NotifyFn | None = None
        self.bots: dict[str, ManagedBot] = {}

        STATE_DIR.mkdir(exist_ok=True)
        TRADES_DIR.mkdir(exist_ok=True)

        for bot_config in config.enabled_bots:
            self.bots[bot_config.name] = self._build_managed_bot(bot_config)

    def set_notifier(self, notifier: NotifyFn | None) -> None:
        self.notifier = notifier

    async def start(self) -> None:
        if not self.bots:
            logger.warning("No enabled managed bots")
            return

        for bot in self.bots.values():
            bot.task = asyncio.create_task(self._run_bot(bot), name=f"bot:{bot.config.name}")
        await self.notify(await self.startup_text())

    async def wait(self) -> None:
        await self.stop_event.wait()

    async def shutdown(self) -> None:
        logger.info("Stopping bot manager")
        self.stop_event.set()
        for bot in self.bots.values():
            if bot.task:
                bot.task.cancel()
        tasks = [bot.task for bot in self.bots.values() if bot.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._save_state()
        await self.notify("Bot manager stopped")

    async def notify(self, text: str) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier(text)
        except Exception as e:
            logger.debug("Notifier failed: %s", e)

    async def startup_text(self) -> str:
        lines = [
            "Bot manager started",
            f"Enabled bots: {', '.join(self.bots) or 'none'}",
            f"Market data limit: {self.config.market_data_max_bars} bars",
        ]
        try:
            lines.append(await self.balance_text())
            lines.append(await self.positions_text())
        except Exception as e:
            lines.append(f"Account summary unavailable: {e}")
        return "\n".join(lines)

    async def status_text(self) -> str:
        lines = ["Status:"]
        for bot in self.bots.values():
            status = "paused" if bot.paused else "running"
            if bot.task and bot.task.done():
                status = "stopped"
            snapshot = self._snapshot(bot)
            lines.append(
                f"{bot.config.name}: {status}, strategy={bot.config.strategy}, "
                f"portfolio={bot.config.portfolio!r}, loops={bot.loop_count}, "
                f"last={bot.last_loop_at}"
            )
            if snapshot:
                lines.append(self._format_snapshot(snapshot))
            if bot.last_error != "-":
                lines.append(f"  last_error: {bot.last_error}")
        return "\n".join(lines)

    async def bots_text(self) -> str:
        lines = ["Managed bots:"]
        configured = {bot.name: bot for bot in self.config.bots}
        for name, bot_config in configured.items():
            state = "enabled" if name in self.bots else "disabled"
            if name in self.bots and self.bots[name].paused:
                state = "paused"
            lines.append(
                f"{name}: {state}, strategy={bot_config.strategy}, "
                f"ticker={bot_config.ticker}, portfolio={bot_config.portfolio!r}, "
                f"poll={bot_config.poll_interval}s"
            )
        return "\n".join(lines)

    async def balance_text(self) -> str:
        broker = self._broker()
        bots = await asyncio.to_thread(broker.get_bots)
        if not bots:
            return "Balance: no ArenaGo portfolios returned"
        lines = ["Balance:"]
        for bot in bots:
            lines.append(f"  {bot.name}: {bot.cash_balance:,.2f} RUB")
        return "\n".join(lines)

    async def positions_text(self) -> str:
        broker = self._broker()
        lines = ["Positions:"]
        any_position = False
        for bot in self.bots.values():
            positions = await asyncio.to_thread(broker.get_positions, bot.config.portfolio)
            if not positions:
                lines.append(f"  {bot.config.portfolio}: none")
                continue
            any_position = True
            for ticker, pos in positions.items():
                lines.append(
                    f"  {bot.config.portfolio}: {ticker} {pos.quantity:+d} lots @ {pos.avg_price:.2f}"
                )
        if not any_position and len(lines) == 1:
            lines.append("  none")
        return "\n".join(lines)

    async def trades_text(self, limit: int = 10) -> str:
        events = await asyncio.to_thread(self._read_recent_trade_events, limit)
        if not events:
            return "Trades: no local trade events yet"
        lines = ["Recent trades:"]
        for event in events:
            lines.append("  " + self._format_trade_event(event))
        return "\n".join(lines)

    async def pause(self, name: str | None = None) -> str:
        targets = self._resolve_targets(name)
        for bot in targets:
            bot.paused = True
            self.state.setdefault("paused", {})[bot.config.name] = True
        self._save_state()
        return "Paused: " + ", ".join(bot.config.name for bot in targets)

    async def resume(self, name: str | None = None) -> str:
        targets = self._resolve_targets(name)
        for bot in targets:
            bot.paused = False
            self.state.setdefault("paused", {})[bot.config.name] = False
        self._save_state()
        return "Resumed: " + ", ".join(bot.config.name for bot in targets)

    def _build_managed_bot(self, bot_config: BotConfig) -> ManagedBot:
        event_sink = lambda event_type, event, name=bot_config.name: self._handle_trade_event(
            name,
            event_type,
            event,
        )
        if bot_config.strategy == "bollinger":
            trader = create_btc_trader(
                token=self.config.arena_token,
                ticker=bot_config.ticker,
                cash_usage=bot_config.cash_usage,
                stop_loss_pct=bot_config.stop_loss_pct,
                take_profit_pct=bot_config.take_profit_pct,
                bot_name=bot_config.portfolio,
                max_bars=self.config.market_data_max_bars,
                event_sink=event_sink,
            )
        elif bot_config.strategy == "trend":
            trader = create_btc_trend_trader(
                token=self.config.arena_token,
                bot_name=bot_config.portfolio,
                ticker=bot_config.ticker,
                daily_loss_limit=bot_config.daily_loss_limit,
                max_bars=self.config.market_data_max_bars,
                event_sink=event_sink,
            )
        else:
            raise ValueError(f"Unknown strategy for {bot_config.name}: {bot_config.strategy}")

        paused = bool(self.state.get("paused", {}).get(bot_config.name, False))
        return ManagedBot(config=bot_config, trader=trader, paused=paused)

    async def _run_bot(self, bot: ManagedBot) -> None:
        logger.info("Starting managed bot %s", bot.config.name)
        try:
            prepare = getattr(bot.trader, "prepare", None)
            if prepare:
                await prepare()
        except Exception as e:
            bot.last_error = str(e)
            logger.exception("%s prepare failed", bot.config.name)
            await self.notify(f"{bot.config.name}: prepare failed: {e}")

        while not self.stop_event.is_set():
            if bot.paused:
                await self._sleep(bot.config.poll_interval)
                continue
            try:
                run_step = getattr(bot.trader, "run_step")
                await run_step()
                bot.loop_count += 1
                bot.last_loop_at = datetime.now().isoformat(timespec="seconds")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                bot.last_error = str(e)
                logger.exception("%s step failed", bot.config.name)
                await self.notify(f"{bot.config.name}: error: {e}")
            await self._sleep(bot.config.poll_interval)

    async def _sleep(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    def _handle_trade_event(self, bot_name: str, event_type: str, event: dict) -> None:
        text = f"{bot_name}: {self._format_trade_event(event)}"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.notify(text))

    def _resolve_targets(self, name: str | None) -> list[ManagedBot]:
        if not name or name.lower() == "all":
            return list(self.bots.values())
        key = name.strip()
        if key in self.bots:
            return [self.bots[key]]
        lowered = key.lower()
        for bot_name, bot in self.bots.items():
            if bot_name.lower() == lowered:
                return [bot]
        raise ValueError(f"Unknown enabled bot: {name}")

    def _broker(self) -> ArenaGoBroker:
        if self.bots:
            first = next(iter(self.bots.values()))
            return first.trader.broker
        first_config = self.config.bots[0]
        return ArenaGoBroker(self.config.arena_token, bot_name=first_config.portfolio)

    def _snapshot(self, bot: ManagedBot) -> dict:
        snapshot = getattr(bot.trader, "status_snapshot", None)
        if not snapshot:
            return {}
        try:
            return snapshot()
        except Exception as e:
            logger.debug("Snapshot failed for %s: %s", bot.config.name, e)
            return {}

    @staticmethod
    def _format_snapshot(snapshot: dict) -> str:
        items = []
        for key in ("position", "quantity", "entry_price", "active_profile", "trading_disabled"):
            if key in snapshot:
                items.append(f"{key}={snapshot[key]}")
        return "  " + ", ".join(items) if items else "  snapshot available"

    @staticmethod
    def _format_trade_event(event: dict) -> str:
        event_type = event.get("event", "EVENT")
        side = event.get("side") or event.get("direction") or "-"
        ticker = event.get("ticker", "-")
        qty = event.get("quantity", "-")
        price = event.get("order_price") or event.get("signal_price") or event.get("market_price") or "-"
        reason = event.get("reason")
        suffix = f", reason={reason}" if reason else ""
        return f"{event.get('ts', '-')}: {event_type} {side} {ticker} qty={qty} price={price}{suffix}"

    @staticmethod
    def _read_recent_trade_events(limit: int) -> list[dict]:
        if not TRADES_DIR.exists():
            return []
        paths = sorted(TRADES_DIR.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        events: list[dict] = []
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(events) >= limit:
                    return list(reversed(events))
        return list(reversed(events))

    @staticmethod
    def _load_state() -> dict:
        if not MANAGER_STATE_PATH.exists():
            return {}
        try:
            return json.loads(MANAGER_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Cannot read manager state: %s", e)
            return {}

    def _save_state(self) -> None:
        MANAGER_STATE_PATH.write_text(
            json.dumps(self.state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
