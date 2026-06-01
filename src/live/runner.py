"""Live BTC trader — Bollinger Mean Reversion on ArenaGo.

Fixes applied:
  1. "Not enough data" — ensure at least bb_length+10 bars before trading;
     force-refresh cache if it contains stale/insufficient data.
  2. Position sizing — use a configurable fraction of available deposit.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pandas as pd

from ..data import get_candles_async, get_lot_size
from ..strategies.bollinger import BollingerStrategy
from .broker import ArenaGoBroker

logger = logging.getLogger(__name__)

# Minimum bars required for the strategy to produce valid signals.
# bb_length=40 + rsi_length=14 + safety margin = ~60
MIN_BARS_REQUIRED = 60

# Fraction of available cash to use per trade by default.
CASH_USAGE_FRACTION = 0.20


def _load_env_token() -> tuple[str, str]:
    """Load API token from .env file or environment variable."""
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    token = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ARENA_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        token = os.environ.get("ARENA_TOKEN", "")
    return token, str(env_path)


class LiveTrader:
    """Runs a strategy in a loop, polling candle data and trading on ArenaGo."""

    def __init__(
        self,
        strategy: BollingerStrategy,
        ticker: str,
        broker: ArenaGoBroker,
        stop_loss_pct: float = 0.05,
        take_profit_pct: float = 0.10,
        interval: str = "1h",
        cash_usage: float = CASH_USAGE_FRACTION,
    ):
        self.strategy = strategy
        self.ticker = ticker.upper()
        self.broker = broker
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.interval = interval
        self.cash_usage = cash_usage

        # Internal state
        self.current_position: int = 0        # +1 long, -1 short, 0 flat
        self.entry_price: float = 0.0
        self.position_qty: int = 0
        self._loop_count: int = 0

    # ── Data loading with safety checks ─────────────────────────────────────

    async def _load_data(self, force_refresh: bool = False) -> pd.DataFrame:
        """Fetch candle data, guaranteeing MIN_BARS_REQUIRED bars.

        If the cached file has fewer than MIN_BARS_REQUIRED bars we
        force a fresh download.
        """
        df = await get_candles_async(
            self.ticker,
            interval=self.interval,
            months_back=6,
            force_refresh=force_refresh,
        )

        if len(df) < MIN_BARS_REQUIRED:
            logger.warning(
                f"{self.ticker}: only {len(df)} bars cached, "
                f"need {MIN_BARS_REQUIRED} — forcing refresh"
            )
            df = await get_candles_async(
                self.ticker,
                interval=self.interval,
                months_back=6,
                force_refresh=True,
            )

        return df

    # ── Position sizing ─────────────────────────────────────────────────────

    def _calc_buy_quantity(self, price: float) -> int:
        """Calculate quantity to buy using cash_usage of deposit."""
        try:
            cash = self.broker.get_cash_balance()
        except Exception as e:
            logger.error(f"Cannot get cash balance: {e}")
            return 0

        budget = cash * self.cash_usage
        lot_size = get_lot_size(self.ticker)

        if price <= 0 or lot_size <= 0:
            return 0

        qty = int(budget // (price * lot_size)) * lot_size
        # Ensure at least 1 lot if we can afford it
        if qty <= 0 and budget >= price * lot_size:
            qty = lot_size

        logger.info(
            f"Position sizing: cash={cash:,.2f} RUB, "
            f"budget({self.cash_usage:.0%})={budget:,.2f} RUB, "
            f"price={price:,.2f}, lot={lot_size} -> qty={qty}"
        )
        return qty

    def _calc_sell_quantity(self) -> int:
        """Calculate quantity to sell (all held)."""
        try:
            positions = self.broker.get_positions()
        except Exception as e:
            logger.error(f"Cannot get positions: {e}")
            return self.position_qty  # fallback to tracked qty

        pos = positions.get(self.ticker)
        if pos:
            return abs(pos.quantity) * get_lot_size(self.ticker)
        return self.position_qty

    # ── Signal evaluation ───────────────────────────────────────────────────

    def _evaluate_signals(self, df: pd.DataFrame) -> int:
        """Run strategy on the latest data, return signal: +1/0/-1."""
        if len(df) < MIN_BARS_REQUIRED:
            logger.warning(
                f"{self.ticker}: not enough data ({len(df)} bars) "
                f"to evaluate signals (need {MIN_BARS_REQUIRED})"
            )
            return 0

        signals = self.strategy.generate_signals(df)
        if signals.empty:
            return 0

        return int(signals.iloc[-1])

    # ── Order execution ─────────────────────────────────────────────────────

    async def _execute_entry(self, direction: int, price: float) -> None:
        """Execute a market entry order."""
        if direction == 1:
            qty = self._calc_buy_quantity(price)
            if qty <= 0:
                logger.warning(f"Calculated buy qty=0, skipping entry")
                return
            try:
                result = self.broker.buy(self.ticker, qty)
                self.current_position = 1
                self.entry_price = result.get("price", price)
                self.position_qty = result.get("quantity", qty)
                logger.info(
                    f"BOUGHT {self.position_qty} x {self.ticker} "
                    f"@ {self.entry_price:,.2f}"
                )
            except Exception as e:
                logger.error(f"Buy failed: {e}")

        elif direction == -1:
            qty = self._calc_buy_quantity(price)
            if qty <= 0:
                logger.warning(f"Calculated sell qty=0, skipping entry")
                return
            try:
                result = self.broker.sell(self.ticker, qty)
                self.current_position = -1
                self.entry_price = result.get("price", price)
                self.position_qty = result.get("quantity", qty)
                logger.info(
                    f"SOLD {self.position_qty} x {self.ticker} "
                    f"@ {self.entry_price:,.2f}"
                )
            except Exception as e:
                logger.error(f"Sell failed: {e}")

    async def _execute_exit(self) -> None:
        """Close the current position."""
        if self.current_position == 0:
            return

        qty = self._calc_sell_quantity() if self.current_position == 1 else self.position_qty
        direction = "S" if self.current_position == 1 else "B"

        if qty <= 0:
            logger.warning(f"Exit qty=0, cannot close position")
            return

        try:
            result = self.broker.submit_order(direction, self.ticker, qty)
            exit_price = result.get("price", 0)
            pnl = 0
            if self.current_position == 1 and self.entry_price > 0:
                pnl = (exit_price - self.entry_price) * self.position_qty
            elif self.current_position == -1 and self.entry_price > 0:
                pnl = (self.entry_price - exit_price) * self.position_qty

            logger.info(
                f"CLOSED {'LONG' if self.current_position == 1 else 'SHORT'} "
                f"{qty} x {self.ticker} @ {exit_price:,.2f}, "
                f"PnL ≈ {pnl:+,.2f} RUB"
            )
        except Exception as e:
            logger.error(f"Exit failed: {e}")
            return

        self.current_position = 0
        self.entry_price = 0.0
        self.position_qty = 0

    # ── Stop-loss / Take-profit ─────────────────────────────────────────────

    def _check_sl_tp(self, current_price: float) -> bool:
        """Return True if SL or TP triggered."""
        if self.current_position == 0 or self.entry_price == 0:
            return False

        if self.current_position == 1:
            change = (current_price - self.entry_price) / self.entry_price
        else:
            change = (self.entry_price - current_price) / self.entry_price

        if change <= -self.stop_loss_pct:
            logger.warning(
                f"STOP LOSS triggered at {change:.2%} "
                f"(price={current_price:,.2f}, entry={self.entry_price:,.2f})"
            )
            return True

        if change >= self.take_profit_pct:
            logger.info(
                f"TAKE PROFIT triggered at {change:.2%} "
                f"(price={current_price:,.2f}, entry={self.entry_price:,.2f})"
            )
            return True

        return False

    # ── Sync position from broker ───────────────────────────────────────────

    def _sync_position(self) -> None:
        """Sync internal position state from broker to avoid drift."""
        try:
            positions = self.broker.get_positions()
            pos = positions.get(self.ticker)
            if pos:
                if pos.quantity > 0 and self.current_position != 1:
                    logger.info(
                        f"Sync: detected open LONG {pos.quantity} x {self.ticker} "
                        f"@ {pos.avg_price:,.2f}"
                    )
                    self.current_position = 1
                    self.entry_price = pos.avg_price
                    self.position_qty = abs(pos.quantity) * get_lot_size(self.ticker)
                elif pos.quantity < 0 and self.current_position != -1:
                    logger.info(
                        f"Sync: detected open SHORT {pos.quantity} x {self.ticker}"
                    )
                    self.current_position = -1
                    self.entry_price = pos.avg_price
                    self.position_qty = abs(pos.quantity) * get_lot_size(self.ticker)
            else:
                if self.current_position != 0:
                    logger.info("Sync: no position on broker, setting flat")
                self.current_position = 0
                self.entry_price = 0.0
                self.position_qty = 0
        except Exception as e:
            logger.warning(f"Position sync failed: {e}")

    # ── Main loop ───────────────────────────────────────────────────────────

    async def run(self, poll_interval_seconds: int = 60) -> None:
        """Main trading loop."""
        logger.info(
            f"Starting live trader: {self.ticker} @ {self.interval}, "
            f"cash_usage={self.cash_usage:.0%}, "
            f"SL={self.stop_loss_pct:.1%}, TP={self.take_profit_pct:.1%}"
        )

        # Sync position from broker at startup
        self._sync_position()

        # Initial data load — force refresh if cache is insufficient
        df = await self._load_data(force_refresh=False)
        if len(df) < MIN_BARS_REQUIRED:
            logger.error(
                f"{self.ticker}: DATA UNAVAILABLE — only {len(df)} bars "
                f"after refresh. Cannot start trading."
            )
            # Do not exit — keep trying in the loop
        else:
            logger.info(f"Initial data: {len(df)} bars for {self.ticker}")

        while True:
            self._loop_count += 1
            try:
                df = await self._load_data(force_refresh=False)

                if len(df) < MIN_BARS_REQUIRED:
                    logger.warning(
                        f"{self.ticker}: not enough data ({len(df)} bars), "
                        f"waiting {poll_interval_seconds}s..."
                    )
                    await asyncio.sleep(poll_interval_seconds)
                    continue

                current_price = df["close"].iloc[-1]

                # Check SL/TP first
                if self._check_sl_tp(current_price):
                    await self._execute_exit()

                # Evaluate strategy signal
                signal = self._evaluate_signals(df)

                # Entry logic
                if self.current_position == 0 and signal != 0:
                    await self._execute_entry(signal, current_price)

                # Exit logic — strategy says go flat
                elif self.current_position != 0 and signal == 0:
                    await self._execute_exit()

                # Reversal — e.g. was long, now strategy says short
                elif self.current_position != 0 and signal != 0 and signal != self.current_position:
                    await self._execute_exit()
                    await self._execute_entry(signal, current_price)

            except Exception as e:
                logger.exception(f"Loop error: {e}")

            await asyncio.sleep(poll_interval_seconds)

    def print_status(self) -> None:
        direction = {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(self.current_position, "?")
        print(f"  Position: {direction}")
        if self.current_position != 0:
            print(f"  Entry: {self.entry_price:,.2f}")
            print(f"  Qty: {self.position_qty}")


# ── Factory ─────────────────────────────────────────────────────────────────

def create_btc_trader(
    token: str,
    cash_usage: float = CASH_USAGE_FRACTION,
    stop_loss_pct: float = 0.02,
    take_profit_pct: float = 0.03,
) -> LiveTrader:
    """Create a BTC Bollinger trader with sensible defaults."""
    if not token:
        raise ValueError("ArenaGo token is required")
    if not 0 < cash_usage <= 1:
        raise ValueError("cash_usage must be in the (0, 1] range")

    strategy = BollingerStrategy()
    broker = ArenaGoBroker(token=token, bot_name="btc")
    return LiveTrader(
        strategy=strategy,
        ticker="BTC",
        broker=broker,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        interval="1h",
        cash_usage=cash_usage,
    )
