"""Live multi-timeframe BTC trend divergence trader."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from ..data import estimate_order_value_rub, estimate_quantity_for_budget, get_candles_async, get_lot_size
from ..strategies.trend_divergence import FrameSignal, TrendDivergenceStrategy
from .broker import ArenaGoBroker

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent.parent.parent / "state"
STATE_DIR.mkdir(exist_ok=True)
TRADES_DIR = Path(__file__).resolve().parent.parent.parent / "trades"
TRADES_DIR.mkdir(exist_ok=True)

DEFAULT_DAILY_LOSS_LIMIT_RUB = 70_000.0


@dataclass(frozen=True)
class TradeProfile:
    name: str
    interval: str
    months_back: int
    min_bars: int
    min_score: int
    cash_usage: float
    take_profit_pct: float
    stop_loss_pct: float


DEFAULT_PROFILES = [
    TradeProfile("BIG_D1", "1d", 18, 240, 6, 0.80, 0.05, 0.03),
    TradeProfile("MEDIUM_H1", "1h", 6, 240, 5, 0.40, 0.03, 0.02),
    TradeProfile("SMALL_M5", "5min", 1, 240, 4, 0.15, 0.015, 0.008),
]


class BtcTrendTrader:
    def __init__(
        self,
        token: str,
        bot_name: str = "btc trend",
        ticker: str = "BTC",
        daily_loss_limit_rub: float = DEFAULT_DAILY_LOSS_LIMIT_RUB,
        profiles: list[TradeProfile] | None = None,
        max_bars: int | None = None,
        event_sink: Callable[[str, dict], None] | None = None,
    ):
        self.ticker = ticker.upper()
        self.bot_name = bot_name
        self.daily_loss_limit_rub = daily_loss_limit_rub
        self.profiles = profiles or DEFAULT_PROFILES
        self.max_bars = max_bars
        self.event_sink = event_sink
        self.strategy = TrendDivergenceStrategy()
        self.broker = ArenaGoBroker(token=token, bot_name=bot_name)
        self.state_path = STATE_DIR / "btc_trend_state.json"
        self.state = self._load_state()

    def describe_profiles(self) -> str:
        parts = []
        for profile in self.profiles:
            parts.append(
                f"{profile.name}:{profile.interval}, score>={profile.min_score}, "
                f"cash={profile.cash_usage:.0%}, TP={profile.take_profit_pct:.1%}, "
                f"SL={profile.stop_loss_pct:.1%}"
            )
        return "; ".join(parts)

    async def run(self, poll_interval_seconds: int = 60) -> None:
        logger.info(f"Running {self.ticker} trend bot on ArenaGo portfolio {self.bot_name!r}")
        await self.prepare()

        while True:
            try:
                await self.run_step()
            except Exception as e:
                logger.exception(f"Trend loop error: {e}")

            await asyncio.sleep(poll_interval_seconds)

    async def prepare(self) -> None:
        logger.info("Profiles: %s", self.describe_profiles())

    async def run_step(self) -> None:
        signals = await self._collect_signals()
        current_price = self._best_price(signals)
        if current_price <= 0:
            logger.warning("No valid current price, waiting")
            return

        self._ensure_day_state(current_price)
        if self._daily_loss_hit(current_price):
            logger.warning("Daily loss limit hit, closing exposure and disabling entries")
            self._close_all(reason="daily_loss_limit", market_price=current_price)
            self.state["trading_disabled"] = True
            self._write_trade_event(
                "DAILY_LOSS_LIMIT",
                {
                    "market_price": current_price,
                    "daily_loss_limit_rub": self.daily_loss_limit_rub,
                    "state": self.state,
                },
            )
            self._save_state()

        self._manage_open_position(current_price)

        if not self.state.get("trading_disabled", False):
            self._maybe_enter(signals)

        self._log_signals(signals)

    async def _collect_signals(self) -> list[tuple[TradeProfile, FrameSignal]]:
        out: list[tuple[TradeProfile, FrameSignal]] = []
        for profile in self.profiles:
            df = await get_candles_async(
                self.ticker,
                interval=profile.interval,
                months_back=profile.months_back,
                force_refresh=False,
                min_bars=profile.min_bars,
                max_bars=self.max_bars,
            )
            if len(df) < profile.min_bars:
                logger.info(
                    f"{profile.name}: only {len(df)} bars for {profile.interval}, "
                    f"need {profile.min_bars}; forcing refresh"
                )
                df = await get_candles_async(
                    self.ticker,
                    interval=profile.interval,
                    months_back=profile.months_back,
                    force_refresh=True,
                    min_bars=profile.min_bars,
                    max_bars=self.max_bars,
                )
            if len(df) < profile.min_bars:
                logger.warning(
                    f"{profile.name}: still only {len(df)} bars for {profile.interval}, "
                    f"skip signal"
                )
                continue
            out.append((profile, self.strategy.score(df, profile.interval)))
        return out

    def _maybe_enter(self, signals: list[tuple[TradeProfile, FrameSignal]]) -> None:
        candidates = [
            (profile, signal)
            for profile, signal in signals
            if signal.direction != 0 and signal.score >= profile.min_score
        ]
        if not candidates:
            return

        profile, signal = max(candidates, key=lambda item: (item[1].score, item[0].cash_usage))
        positions = self.broker.get_positions()
        current = positions.get(self.ticker)
        current_direction = 0
        if current and current.quantity != 0:
            current_direction = 1 if current.quantity > 0 else -1

        if current_direction and current_direction != signal.direction:
            logger.info(
                f"{profile.name}: opposite signal {signal.side}, closing current exposure first"
            )
            self._close_all(reason="opposite_signal", market_price=signal.close)
            return

        qty = self._calc_entry_quantity(signal.close, profile.cash_usage)
        if qty <= 0:
            logger.info(f"{profile.name}: qty=0, skip entry")
            return

        direction = "B" if signal.direction == 1 else "S"
        logger.info(
            f"{profile.name}: entering {signal.side} qty={qty}, score={signal.score}, "
            f"reasons={','.join(signal.reasons)}"
        )
        result = self.broker.submit_order(direction, self.ticker, qty)
        order_price = float(result.get("price") or signal.close)
        order_qty = int(result.get("quantity") or qty)
        self.state["active_exit"] = {
            "profile": profile.name,
            "direction": signal.direction,
            "take_profit_pct": profile.take_profit_pct,
            "stop_loss_pct": profile.stop_loss_pct,
            "last_entry_price": order_price,
        }
        self._save_state()
        self._write_trade_event(
            "ENTRY",
            {
                "profile": profile.name,
                "interval": profile.interval,
                "side": signal.side,
                "direction": direction,
                "ticker": self.ticker,
                "quantity": order_qty,
                "signal_price": signal.close,
                "order_price": order_price,
                "score": signal.score,
                "reasons": signal.reasons,
                "details": signal.details,
                "cash_usage": profile.cash_usage,
                "take_profit_pct": profile.take_profit_pct,
                "stop_loss_pct": profile.stop_loss_pct,
                "api_response": result,
            },
        )

    def _manage_open_position(self, current_price: float) -> None:
        positions = self.broker.get_positions()
        pos = positions.get(self.ticker)
        if not pos or pos.quantity == 0 or pos.avg_price <= 0:
            self.state.pop("active_exit", None)
            self._save_state()
            return

        active_exit = self.state.get("active_exit") or {}
        take_profit = float(active_exit.get("take_profit_pct", 0.03))
        stop_loss = float(active_exit.get("stop_loss_pct", 0.02))
        direction = 1 if pos.quantity > 0 else -1
        change = ((current_price - pos.avg_price) / pos.avg_price) * direction

        if change >= take_profit:
            logger.info(f"Take profit hit: change={change:.2%}, target={take_profit:.2%}")
            self._close_all(reason="take_profit", market_price=current_price, pnl_pct=change)
        elif change <= -stop_loss:
            logger.warning(f"Stop loss hit: change={change:.2%}, stop={stop_loss:.2%}")
            self._close_all(reason="stop_loss", market_price=current_price, pnl_pct=change)

    def _close_all(
        self,
        reason: str = "manual",
        market_price: float | None = None,
        pnl_pct: float | None = None,
    ) -> None:
        positions = self.broker.get_positions()
        pos = positions.get(self.ticker)
        if not pos or pos.quantity == 0:
            return

        qty = abs(pos.quantity) * get_lot_size(self.ticker)
        direction = "S" if pos.quantity > 0 else "B"
        result = self.broker.submit_order(direction, self.ticker, qty)
        order_price = float(result.get("price") or market_price or 0)
        estimated_pnl = None
        if market_price is not None:
            signed_qty = pos.quantity * get_lot_size(self.ticker)
            estimated_pnl = estimate_order_value_rub(self.ticker, market_price - pos.avg_price, signed_qty)

        self._write_trade_event(
            "EXIT",
            {
                "reason": reason,
                "ticker": self.ticker,
                "direction": direction,
                "quantity": int(result.get("quantity") or qty),
                "avg_entry_price": pos.avg_price,
                "market_price": market_price,
                "order_price": order_price,
                "pnl_pct": pnl_pct,
                "estimated_pnl": estimated_pnl,
                "active_exit": self.state.get("active_exit"),
                "api_response": result,
            },
        )
        self.state.pop("active_exit", None)
        self._save_state()

    def _calc_entry_quantity(self, price: float, cash_usage: float) -> int:
        if price <= 0:
            return 0
        cash = self.broker.get_cash_balance(self.bot_name)
        budget = cash * cash_usage
        return estimate_quantity_for_budget(self.ticker, price, budget)

    def _ensure_day_state(self, current_price: float) -> None:
        today = date.today().isoformat()
        if self.state.get("date") == today:
            return
        equity = self._equity(current_price)
        self.state = {
            "date": today,
            "day_start_equity": equity,
            "trading_disabled": False,
            "active_exit": self.state.get("active_exit"),
        }
        self._save_state()
        logger.info(f"New trading day: start equity={equity:,.2f}")

    def _daily_loss_hit(self, current_price: float) -> bool:
        start_equity = float(self.state.get("day_start_equity") or 0)
        if start_equity <= 0:
            return False
        equity = self._equity(current_price)
        daily_pnl = equity - start_equity
        drawdown = daily_pnl / start_equity
        logger.info(
            f"Daily equity={equity:,.2f}, "
            f"PnL={daily_pnl:,.2f} RUB ({drawdown:.2%})"
        )
        return daily_pnl <= -self.daily_loss_limit_rub

    def _equity(self, current_price: float) -> float:
        cash = self.broker.get_cash_balance(self.bot_name)
        positions = self.broker.get_positions()
        pos = positions.get(self.ticker)
        if not pos:
            return cash
        quantity = pos.quantity * get_lot_size(self.ticker)
        if quantity > 0:
            position_equity = estimate_order_value_rub(self.ticker, current_price, quantity)
        else:
            # ArenaGo cash does not add short sale proceeds; mark shorts by unrealized PnL.
            position_equity = estimate_order_value_rub(
                self.ticker,
                pos.avg_price - current_price,
                abs(quantity),
            )
        return cash + position_equity

    def _best_price(self, signals: list[tuple[TradeProfile, FrameSignal]]) -> float:
        for profile_name in ("SMALL_M5", "MEDIUM_H1", "BIG_D1"):
            for profile, signal in signals:
                if profile.name == profile_name and signal.close > 0:
                    return signal.close
        return 0.0

    def _log_signals(self, signals: list[tuple[TradeProfile, FrameSignal]]) -> None:
        for profile, signal in signals:
            details = ", ".join(f"{k}={v}" for k, v in signal.details.items())
            logger.info(
                f"{profile.name}: {signal.side} score={signal.score} "
                f"close={signal.close:,.2f} reasons={','.join(signal.reasons) or '-'}"
            )
            logger.info(f"{profile.name} details: {details or '-'}")

    def print_status(self) -> None:
        print(self.broker.summary())
        positions = self.broker.get_positions()
        pos = positions.get(self.ticker)
        if pos:
            print(f"  {self.ticker}: {pos.quantity:+d} lots @ {pos.avg_price:.2f}")
        else:
            print(f"  {self.ticker}: no open position")
        print(f"  State: {self.state}")

    def status_snapshot(self) -> dict:
        active_exit = self.state.get("active_exit") or {}
        return {
            "ticker": self.ticker,
            "portfolio": self.bot_name,
            "strategy": "trend_divergence",
            "active_profile": active_exit.get("profile", "-"),
            "trading_disabled": bool(self.state.get("trading_disabled", False)),
            "daily_loss_limit_rub": self.daily_loss_limit_rub,
            "profiles": [profile.name for profile in self.profiles],
        }

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Cannot read state file: {e}")
            return {}

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _write_trade_event(self, event_type: str, payload: dict) -> None:
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event_type,
            "bot_name": self.bot_name,
            "ticker": self.ticker,
            **payload,
        }
        path = TRADES_DIR / f"btc_trend_trades_{date.today().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self._emit_event(event_type, event)

    def _emit_event(self, event_type: str, event: dict) -> None:
        if not self.event_sink:
            return
        try:
            self.event_sink(event_type, event)
        except Exception as e:
            logger.debug("Event sink failed: %s", e)


def create_btc_trend_trader(
    token: str,
    bot_name: str = "btc trend",
    ticker: str = "BTC",
    daily_loss_limit_rub: float = DEFAULT_DAILY_LOSS_LIMIT_RUB,
    max_bars: int | None = None,
    event_sink: Callable[[str, dict], None] | None = None,
) -> BtcTrendTrader:
    if not token:
        raise ValueError("ArenaGo token is required")
    return BtcTrendTrader(
        token=token,
        bot_name=bot_name,
        ticker=ticker,
        daily_loss_limit_rub=daily_loss_limit_rub,
        max_bars=max_bars,
        event_sink=event_sink,
    )
