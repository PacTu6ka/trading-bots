"""Real-time data feed for BTC via Bybit/OKX.

BTC trades 24/7 on crypto exchanges, so no market hours check.
Uses same data sources as backtesting (Bybit primary, OKX fallback).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)


async def _fetch_bybit(
    session: aiohttp.ClientSession,
    symbol: str,
    n_bars: int,
) -> pd.DataFrame:
    """Fetch last N 1h bars from Bybit."""
    end_ms = int(datetime.now().timestamp() * 1000)
    start_ms = int((datetime.now() - timedelta(hours=n_bars + 5)).timestamp() * 1000)

    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": "60",
        "start": start_ms,
        "end": end_ms,
        "limit": n_bars + 5,
    }
    try:
        async with session.get(
            "https://api.bybit.com/v5/market/kline",
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.debug(f"Bybit {resp.status}")
                return pd.DataFrame()
            data = await resp.json()
    except Exception as e:
        logger.debug(f"Bybit error: {e}")
        return pd.DataFrame()

    rows = data.get("result", {}).get("list", [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
    df = df.set_index("timestamp").sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close", "volume"]].tail(n_bars)


async def _fetch_okx(
    session: aiohttp.ClientSession,
    symbol: str,
    n_bars: int,
) -> pd.DataFrame:
    """Fetch last N 1h bars from OKX."""
    inst_id = symbol.replace("USDT", "-USDT")  # BTCUSDT -> BTC-USDT
    params = {"instId": inst_id, "bar": "1H", "limit": str(n_bars + 5)}

    try:
        async with session.get(
            "https://www.okx.com/api/v5/market/candles",
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.debug(f"OKX {resp.status}")
                return pd.DataFrame()
            data = await resp.json()
    except Exception as e:
        logger.debug(f"OKX error: {e}")
        return pd.DataFrame()

    rows = data.get("data", [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close",
                 "volume", "volCcy", "volCcyQuote", "confirm"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
    df = df.set_index("timestamp").sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close", "volume"]].tail(n_bars)


async def fetch_last_bars(
    session: aiohttp.ClientSession,
    ticker: str,
    n_bars: int = 3,
) -> pd.DataFrame:
    """Fetch the last N completed 1h bars. Tries Bybit, falls back to OKX."""
    symbol = "BTCUSDT" if ticker.upper() == "BTC" else ticker.upper()

    df = await _fetch_bybit(session, symbol, n_bars)
    if not df.empty:
        return df

    df = await _fetch_okx(session, symbol, n_bars)
    return df


class RealTimeDataFeed:
    """Polls Bybit/OKX for new 1h candles (BTC is 24/7)."""

    def __init__(
        self,
        tickers: list[str],
        on_new_bar: Callable[[str, pd.DataFrame], None],
        history_df: Optional[dict[str, pd.DataFrame]] = None,
        poll_interval_seconds: int = 60,
    ):
        self.tickers = tickers
        self.on_new_bar = on_new_bar
        self._history = history_df or {}
        self._last_bar_time: dict[str, Optional[pd.Timestamp]] = {t: None for t in tickers}
        self._poll_interval = poll_interval_seconds
        self._source_logged = False

    def set_history(self, ticker: str, df: pd.DataFrame) -> None:
        self._history[ticker] = df.copy()
        if not df.empty:
            self._last_bar_time[ticker] = df.index[-1]

    async def run(self) -> None:
        """Main loop — runs forever (BTC is 24/7)."""
        logger.info(f"Starting BTC data feed for {self.tickers} (Bybit → OKX fallback)")
        async with aiohttp.ClientSession() as session:
            while True:
                for ticker in self.tickers:
                    await self._poll_ticker(session, ticker)
                await asyncio.sleep(self._poll_interval)

    async def _poll_ticker(self, session: aiohttp.ClientSession, ticker: str) -> None:
        new_bars = await fetch_last_bars(session, ticker, n_bars=3)
        if new_bars.empty:
            return

        last_seen = self._last_bar_time.get(ticker)
        latest_bar_ts = new_bars.index[-1]

        if last_seen is None or latest_bar_ts > last_seen:
            if not self._source_logged:
                logger.info(f"Data source active for {ticker}")
                self._source_logged = True

            logger.info(f"New 1h bar: {ticker} @ {latest_bar_ts}")
            self._last_bar_time[ticker] = latest_bar_ts

            if ticker in self._history and not self._history[ticker].empty:
                hist = self._history[ticker]
                new_rows = new_bars[~new_bars.index.isin(hist.index)]
                self._history[ticker] = pd.concat([hist, new_rows]).sort_index()
            else:
                self._history[ticker] = new_bars

            try:
                self.on_new_bar(ticker, self._history[ticker].copy())
            except Exception as e:
                logger.error(f"on_new_bar callback error for {ticker}: {e}")
