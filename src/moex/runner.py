"""MOEX live traders adapted to the shared BotManager interface."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from src.manager.config import PROJECT_ROOT

from .broker import ArenaGoBroker, Order, OrderSide, OrderStatus
from .data import get_candles_async, merge_history
from .presets import PORTFOLIO_TICKERS, make_strategy
from .presets_intraday import INTRADAY_STRATEGY_MAP, make_intraday_strategy
from .strategies import Strategy

logger = logging.getLogger(__name__)

TRADES_DIR = PROJECT_ROOT / "trades"
TRADES_DIR.mkdir(exist_ok=True)

MSK = timezone(timedelta(hours=3))
EventSink = Callable[[str, dict], None]


class MoexLiveTrader:
    """Shared runner for portfolio and intraday MOEX bots.

    The old MOEX app owned an infinite RealTimeDataFeed loop. This adapter keeps
    the trading logic step-based so the common BotManager controls scheduling,
    shutdown, Telegram status, and RAM limits.
    """

    def __init__(
        self,
        name: str,
        broker: ArenaGoBroker,
        strategies: dict[str, Strategy],
        fixed_position_rub: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        interval: str,
        allow_short: bool = True,
        max_bars: int = 600,
        min_bars: int = 50,
        history_months_back: int = 6,
        refresh_months_back: int = 1,
        market_open_time: str = "09:50",
        market_close_time: str = "18:50",
        no_entry_after: str | None = None,
        force_flat_at: str | None = None,
        max_open_positions: int | None = None,
        fetch_concurrency: int = 4,
        trade_file_prefix: str = "moex",
        event_sink: EventSink | None = None,
    ):
        self.name = name
        self.broker = broker
        self.strategies = strategies
        self.fixed_position_rub = fixed_position_rub
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.interval = interval
        self.allow_short = allow_short
        self.max_bars = max_bars
        self.min_bars = min_bars
        self.history_months_back = history_months_back
        self.refresh_months_back = refresh_months_back
        self.market_open_time = self._parse_clock(market_open_time)
        self.market_close_time = self._parse_clock(market_close_time)
        self.no_entry_after = self._parse_clock(no_entry_after) if no_entry_after else None
        self.force_flat_at = self._parse_clock(force_flat_at) if force_flat_at else None
        self.max_open_positions = max_open_positions
        self.fetch_concurrency = max(1, fetch_concurrency)
        self.trade_file_prefix = trade_file_prefix
        self.event_sink = event_sink

        self._history: dict[str, pd.DataFrame] = {}
        self._last_bar_time: dict[str, pd.Timestamp] = {}
        self._last_signal: dict[str, int] = {ticker: 0 for ticker in strategies}
        self._entry_prices: dict[str, float] = {}
        self._loop_count = 0

    @staticmethod
    def _parse_clock(value: str) -> dt_time:
        hour, minute = value.split(":", 1)
        return dt_time(int(hour), int(minute))

    def _arena_lot_size(self, ticker: str) -> int:
        return int(self.broker._get_lot_size(ticker))

    def _clock_for_bar(self, bar_time: pd.Timestamp | None = None) -> dt_time:
        if bar_time is not None:
            ts = pd.Timestamp(bar_time)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("Europe/Moscow")
            return ts.time().replace(second=0, microsecond=0)
        return datetime.now(MSK).time().replace(second=0, microsecond=0)

    def _is_market_open(self) -> bool:
        now = datetime.now(MSK)
        if now.weekday() >= 5:
            return False
        clock = now.time().replace(second=0, microsecond=0)
        return self.market_open_time <= clock <= self.market_close_time

    def _is_no_entry_period(self, bar_time: pd.Timestamp | None = None) -> bool:
        return self.no_entry_after is not None and self._clock_for_bar(bar_time) >= self.no_entry_after

    def _is_force_flat_time(self, bar_time: pd.Timestamp | None = None) -> bool:
        return self.force_flat_at is not None and self._clock_for_bar(bar_time) >= self.force_flat_at

    async def prepare(self) -> None:
        logger.info(
            "Preparing %s: tickers=%s interval=%s max_bars=%s",
            self.name,
            list(self.strategies),
            self.interval,
            self.max_bars,
        )
        self.sync_positions()
        self._history = await self._load_all_history(self.history_months_back, force_refresh=False)
        self._last_bar_time = {
            ticker: df.index[-1]
            for ticker, df in self._history.items()
            if not df.empty
        }
        await self._process_initial_signals()

    async def run_step(self) -> None:
        self._loop_count += 1
        if not self._history:
            self._history = await self._load_all_history(self.history_months_back, force_refresh=False)

        semaphore = asyncio.Semaphore(self.fetch_concurrency)

        async def poll(ticker: str) -> None:
            async with semaphore:
                await self._poll_ticker(ticker)

        await asyncio.gather(*(poll(ticker) for ticker in self.strategies), return_exceptions=False)

    async def _load_all_history(self, months_back: int, force_refresh: bool) -> dict[str, pd.DataFrame]:
        semaphore = asyncio.Semaphore(self.fetch_concurrency)
        history: dict[str, pd.DataFrame] = {}

        async def load_one(ticker: str) -> None:
            async with semaphore:
                try:
                    df = await get_candles_async(
                        ticker,
                        interval=self.interval,
                        months_back=months_back,
                        force_refresh=force_refresh,
                        max_bars=self.max_bars,
                        min_bars=self.min_bars,
                    )
                except Exception as e:
                    logger.warning("%s: history load failed for %s: %s", self.name, ticker, e)
                    return
                if not df.empty:
                    history[ticker] = df
                    logger.info("%s: loaded %s bars for %s", self.name, len(df), ticker)
                else:
                    logger.warning("%s: no history for %s", self.name, ticker)

        await asyncio.gather(*(load_one(ticker) for ticker in self.strategies))
        return history

    async def _poll_ticker(self, ticker: str) -> None:
        try:
            df = await get_candles_async(
                ticker,
                interval=self.interval,
                months_back=self.refresh_months_back,
                force_refresh=True,
                max_bars=self.max_bars,
                min_bars=self.min_bars,
            )
        except Exception as e:
            logger.warning("%s: poll failed for %s: %s", self.name, ticker, e)
            return

        if df.empty:
            return

        last_time = df.index[-1]
        prev_time = self._last_bar_time.get(ticker)
        if prev_time is not None and last_time <= prev_time:
            return

        self._history[ticker] = merge_history(
            self._history.get(ticker),
            df,
            max_bars=self.max_bars,
            min_bars=self.min_bars,
        )
        self._last_bar_time[ticker] = self._history[ticker].index[-1]
        await self._on_new_bar(ticker, self._history[ticker])

    def sync_positions(self) -> None:
        try:
            real_positions = self.broker.get_positions()
        except Exception as e:
            logger.warning("%s: could not sync positions: %s", self.name, e)
            return

        for ticker in self.strategies:
            pos = real_positions.get(ticker)
            if pos is None or pos.quantity == 0:
                self._last_signal[ticker] = 0
                self._entry_prices.pop(ticker, None)
            else:
                self._last_signal[ticker] = 1 if pos.quantity > 0 else -1
                self._entry_prices[ticker] = pos.avg_price
        logger.info("%s: synced %s open positions", self.name, sum(1 for s in self._last_signal.values() if s))

    async def _process_initial_signals(self) -> None:
        for ticker, df in self._history.items():
            if df.empty or len(df) < self.min_bars:
                logger.warning("%s: not enough initial data for %s (%s bars)", self.name, ticker, len(df))
                continue

            strategy = self.strategies.get(ticker)
            if strategy is None:
                continue

            last_price = float(df["close"].iloc[-1])
            self.broker.update_price(ticker, last_price)
            if self._last_signal.get(ticker, 0) != 0:
                continue

            try:
                signals = strategy.generate_signals(df)
            except Exception as e:
                logger.error("%s: initial signal failed for %s: %s", self.name, ticker, e)
                continue

            new_signal = int(signals.iloc[-1]) if not signals.empty else 0
            if not self.allow_short:
                new_signal = max(0, new_signal)
            if new_signal == 0:
                continue

            if self._is_no_entry_period(df.index[-1]):
                logger.info("%s: %s initial signal skipped in no-entry period", self.name, ticker)
                continue
            if not self._is_market_open():
                logger.info("%s: %s initial signal waits for market open", self.name, ticker)
                continue

            final_signal = self._execute_signal_change(ticker, 0, new_signal, last_price)
            if final_signal == new_signal:
                self._last_signal[ticker] = final_signal

    async def _on_new_bar(self, ticker: str, df: pd.DataFrame) -> None:
        if df.empty or len(df) < self.min_bars:
            logger.warning("%s: not enough data for %s (%s bars)", self.name, ticker, len(df))
            return

        strategy = self.strategies.get(ticker)
        if strategy is None:
            return

        last_price = float(df["close"].iloc[-1])
        bar_time = df.index[-1]
        self.broker.update_price(ticker, last_price)

        old_signal = self._last_signal.get(ticker, 0)
        if old_signal != 0 and self._is_force_flat_time(bar_time):
            logger.warning("%s: force-flat %s at %s", self.name, ticker, bar_time)
            order = self.broker.close_position(ticker)
            self._record_order_event(order, "EXIT", "FORCE_FLAT", ticker, last_price, old_signal, 0)
            if order is None or order.status == OrderStatus.FILLED:
                self._last_signal[ticker] = 0
                self._entry_prices.pop(ticker, None)
            return

        if old_signal != 0 and self._check_tp_sl(ticker, last_price):
            return

        try:
            signals = strategy.generate_signals(df)
        except Exception as e:
            logger.error("%s: signal generation failed for %s: %s", self.name, ticker, e)
            return

        new_signal = int(signals.iloc[-1]) if not signals.empty else 0
        if not self.allow_short:
            new_signal = max(0, new_signal)

        if self._is_no_entry_period(bar_time) and new_signal != old_signal:
            new_signal = 0

        logger.info(
            "%s: %s bar=%s close=%.2f signal %+.0f -> %+.0f",
            self.name,
            ticker,
            bar_time,
            last_price,
            old_signal,
            new_signal,
        )

        if new_signal != old_signal:
            if not self._is_market_open():
                logger.info("%s: %s signal skipped while market is closed", self.name, ticker)
                return
            final_signal = self._execute_signal_change(ticker, old_signal, new_signal, last_price)
            if final_signal is not None:
                self._last_signal[ticker] = final_signal

    def _check_tp_sl(self, ticker: str, current_price: float) -> bool:
        entry_price = self._entry_prices.get(ticker)
        signal = self._last_signal.get(ticker, 0)
        if entry_price is None or entry_price == 0 or signal == 0:
            return False

        pnl_pct = (
            (current_price - entry_price) / entry_price
            if signal == 1
            else (entry_price - current_price) / entry_price
        )
        if pnl_pct < self.take_profit_pct and pnl_pct > -self.stop_loss_pct:
            return False

        reason = "TAKE_PROFIT" if pnl_pct >= self.take_profit_pct else "STOP_LOSS"
        order = self.broker.close_position(ticker)
        self._record_order_event(order, "EXIT", reason, ticker, current_price, signal, 0)
        if order is not None and order.status != OrderStatus.FILLED:
            return False
        self._last_signal[ticker] = 0
        self._entry_prices.pop(ticker, None)
        return True

    def _execute_signal_change(self, ticker: str, old_signal: int, new_signal: int, price: float) -> int | None:
        final_signal = old_signal
        if old_signal != 0:
            close_order = self.broker.close_position(ticker)
            self._record_order_event(close_order, "EXIT", "SIGNAL_CHANGE", ticker, price, old_signal, 0)
            if close_order is not None and close_order.status != OrderStatus.FILLED:
                return None
            self._entry_prices.pop(ticker, None)
            final_signal = 0

        if new_signal == 0:
            return final_signal

        if old_signal == 0 and self.max_open_positions is not None:
            open_count = sum(1 for signal in self._last_signal.values() if signal != 0)
            if open_count >= self.max_open_positions:
                logger.info("%s: max_open_positions reached, skip %s", self.name, ticker)
                return final_signal

        quantity_shares = self._compute_quantity(ticker, price)
        if quantity_shares <= 0:
            return final_signal

        side = OrderSide.BUY if new_signal > 0 else OrderSide.SELL
        position_value = quantity_shares * price
        if position_value > self.fixed_position_rub * 3:
            logger.error(
                "%s: safety skip %s position value %.0f > %.0f",
                self.name,
                ticker,
                position_value,
                self.fixed_position_rub * 3,
            )
            return final_signal

        order = self.broker.submit_market_order(ticker, side, quantity_shares)
        self._record_order_event(order, "ENTRY", "SIGNAL_CHANGE", ticker, price, old_signal, new_signal)
        if order.status == OrderStatus.FILLED:
            self._entry_prices[ticker] = order.filled_price or price
            final_signal = new_signal
        return final_signal

    def _compute_quantity(self, ticker: str, price: float) -> int:
        if price <= 0:
            return 0
        arena_lot = self._arena_lot_size(ticker)
        arena_units = max(1, int(self.fixed_position_rub / (price * arena_lot)))
        return arena_units * arena_lot

    def _record_order_event(
        self,
        order: Order | None,
        event_type: str,
        reason: str,
        ticker: str,
        price: float | None,
        signal_before: int,
        signal_after: int,
    ) -> None:
        if order is None:
            return
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event_type,
            "reason": reason,
            "bot_name": self.broker.bot_name,
            "ticker": ticker,
            "strategy": getattr(self.strategies.get(ticker), "name", ""),
            "interval": self.interval,
            "side": "LONG" if signal_after > 0 else "SHORT" if signal_after < 0 else order.side.value,
            "direction": order.side.value,
            "quantity": order.quantity,
            "api_quantity": order.api_quantity,
            "signal_before": signal_before,
            "signal_after": signal_after,
            "signal_price": price,
            "order_price": order.filled_price,
            "status": order.status.value,
        }
        path = TRADES_DIR / f"{self.trade_file_prefix}_trades_{date.today().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        if self.event_sink:
            self.event_sink(event_type, event)

    def status_snapshot(self) -> dict:
        open_positions = sum(1 for signal in self._last_signal.values() if signal != 0)
        last_bar = max(self._last_bar_time.values()).isoformat() if self._last_bar_time else "-"
        return {
            "portfolio": self.broker.bot_name,
            "interval": self.interval,
            "watched": len(self.strategies),
            "open_positions": open_positions,
            "last_bar_time": last_bar,
            "max_bars": self.max_bars,
            "loop_count": self._loop_count,
        }


def create_moex_portfolio_trader(
    token: str,
    bot_name: str = "saux hak",
    fixed_position_rub: float = 200_000.0,
    stop_loss_pct: float = 0.03,
    take_profit_pct: float = 0.025,
    max_bars: int = 600,
    event_sink: EventSink | None = None,
) -> MoexLiveTrader:
    if not token:
        raise ValueError("ArenaGo token is required")
    strategies = {ticker: make_strategy(ticker) for ticker in PORTFOLIO_TICKERS}
    return MoexLiveTrader(
        name="moex_portfolio",
        broker=ArenaGoBroker(token=token, bot_name=bot_name),
        strategies=strategies,
        fixed_position_rub=fixed_position_rub,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        interval="1h",
        allow_short=True,
        max_bars=max_bars,
        min_bars=160,
        history_months_back=6,
        refresh_months_back=1,
        market_open_time="09:50",
        market_close_time="18:50",
        trade_file_prefix="moex_portfolio",
        event_sink=event_sink,
    )


def create_moex_intraday_trader(
    token: str,
    bot_name: str = "saux intraday",
    fixed_position_rub: float = 200_000.0,
    stop_loss_pct: float = 0.012,
    take_profit_pct: float = 0.018,
    max_bars: int = 1200,
    max_open_positions: int = 5,
    event_sink: EventSink | None = None,
) -> MoexLiveTrader:
    if not token:
        raise ValueError("ArenaGo token is required")
    strategies = {
        ticker: make_intraday_strategy(ticker)
        for ticker in INTRADAY_STRATEGY_MAP
    }
    return MoexLiveTrader(
        name="moex_intraday",
        broker=ArenaGoBroker(token=token, bot_name=bot_name),
        strategies=strategies,
        fixed_position_rub=fixed_position_rub,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        interval="5min",
        allow_short=True,
        max_bars=max_bars,
        min_bars=240,
        history_months_back=1,
        refresh_months_back=1,
        market_open_time="06:55",
        market_close_time="23:30",
        no_entry_after="23:20",
        force_flat_at="23:25",
        max_open_positions=max_open_positions,
        trade_file_prefix="moex_intraday",
        event_sink=event_sink,
    )

