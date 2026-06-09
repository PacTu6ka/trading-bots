"""Data loader for BTC bot — Bybit (primary) + OKX (fallback).

Same data sources as used in backtesting.
Bybit and OKX both work from Russia (unlike Binance which returns 451).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

INTERVAL_MAP = {"1h": "60", "1d": "D", "5min": "5", "10min": "10", "1min": "1"}
OKX_INTERVAL_MAP = {"1h": "1H", "1d": "1D", "5min": "5m", "10min": "10m", "1min": "1m"}
INTERVAL_MAX_AGE = {
    "1min": timedelta(minutes=3),
    "5min": timedelta(minutes=12),
    "10min": timedelta(minutes=22),
    "1h": timedelta(hours=2),
    "1d": timedelta(days=2),
}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_MARKET_DATA_MAX_BARS = 600
_MEMORY_CACHE: dict[tuple[str, str], pd.DataFrame] = {}

# BTC lot size on ArenaGo
LOT_SIZES = {"BTC": 1}

# ArenaGo BTC quantity is a small BTC contract, while market data close is BTCUSDT.
# Based on live fills, 1 BTC lot is approximately 0.001 BTC converted to RUB.
RUB_VALUE_MULTIPLIERS = {"BTC": 0.071}


def get_lot_size(ticker: str) -> int:
    return LOT_SIZES.get(ticker.upper(), 1)


def estimate_order_value_rub(ticker: str, price: float, quantity: int) -> float:
    """Estimate ArenaGo order value in RUB for market-data price and lot quantity."""
    multiplier = RUB_VALUE_MULTIPLIERS.get(ticker.upper(), 1.0)
    return price * quantity * multiplier


def estimate_quantity_for_budget(ticker: str, price: float, budget_rub: float) -> int:
    """Estimate ArenaGo lot quantity that fits a RUB budget."""
    lot_size = get_lot_size(ticker)
    multiplier = RUB_VALUE_MULTIPLIERS.get(ticker.upper(), 1.0)
    unit_value = price * multiplier * lot_size
    if price <= 0 or budget_rub <= 0 or unit_value <= 0:
        return 0
    qty = int(budget_rub // unit_value) * lot_size
    return max(qty, 0)


# Minimum bars needed for indicators (bb_length=40 + rsi_length=14 + margin)
MIN_BARS_FOR_STRATEGY = 60


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using %s", name, raw, default)
        return default
    return max(value, 1)


def _max_bars_limit(min_bars: int = MIN_BARS_FOR_STRATEGY, max_bars: int | None = None) -> int:
    configured = max_bars if max_bars is not None else _env_int(
        "MARKET_DATA_MAX_BARS",
        DEFAULT_MARKET_DATA_MAX_BARS,
    )
    return max(configured, min_bars)


def _trim_bars(df: pd.DataFrame, min_bars: int = MIN_BARS_FOR_STRATEGY, max_bars: int | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    limit = _max_bars_limit(min_bars=min_bars, max_bars=max_bars)
    if len(df) <= limit:
        return df
    return df.tail(limit)


def _is_fresh_dataframe(
    df: pd.DataFrame,
    min_bars: int = MIN_BARS_FOR_STRATEGY,
    interval: str = "1h",
) -> bool:
    if df.empty or len(df) < min_bars:
        return False
    try:
        last_ts = pd.Timestamp(df.index[-1])
        if last_ts.tzinfo is not None:
            last_ts = last_ts.tz_convert(None)
        max_age = INTERVAL_MAX_AGE.get(interval, timedelta(hours=2))
        age = pd.Timestamp.utcnow().tz_localize(None) - last_ts
        return age <= max_age
    except Exception:
        return False


def _validate_cache(
    cache_path: Path,
    min_bars: int = MIN_BARS_FOR_STRATEGY,
    interval: str = "1h",
) -> bool:
    """Check if cached file has enough bars. Returns False if stale."""
    if not cache_path.exists():
        return False
    try:
        df = pd.read_parquet(cache_path)
        if len(df) < min_bars:
            logger.warning(
                f"Cache {cache_path.name} has only {len(df)} bars (need {min_bars}), will refresh"
            )
            return False

        last_ts = pd.Timestamp(df.index[-1])
        if last_ts.tzinfo is not None:
            last_ts = last_ts.tz_convert(None)
        max_age = INTERVAL_MAX_AGE.get(interval, timedelta(hours=2))
        age = pd.Timestamp.utcnow().tz_localize(None) - last_ts
        if age > max_age:
            logger.info(
                f"Cache {cache_path.name} is stale: last bar {last_ts}, age {age}, will refresh"
            )
            return False

        return True
    except Exception as e:
        logger.warning(f"Cache read error: {e}, will refresh")
        return False


def get_candles(
    ticker: str,
    interval: str = "1h",
    months_back: int = 6,
    force_refresh: bool = False,
    max_bars: int | None = None,
) -> pd.DataFrame:
    cache_path = DATA_DIR / f"{ticker}_{interval}.parquet"

    if not force_refresh and _validate_cache(cache_path, interval=interval):
        df = pd.read_parquet(cache_path)
        logger.debug(f"Loaded {ticker} from cache: {len(df)} candles")
        df = _trim_bars(df, max_bars=max_bars)
        _MEMORY_CACHE[(ticker.upper(), interval)] = df
        return df

    # Delete stale cache if it exists
    if cache_path.exists() and not _validate_cache(cache_path, interval=interval):
        cache_path.unlink(missing_ok=True)
        logger.info(f"Deleted stale cache: {cache_path}")

    df = asyncio.run(_fetch_crypto(ticker, interval, months_back))

    if not df.empty:
        df.to_parquet(cache_path)
        logger.info(f"Saved {ticker}: {len(df)} candles -> {cache_path}")
    df = _trim_bars(df, max_bars=max_bars)
    if not df.empty:
        _MEMORY_CACHE[(ticker.upper(), interval)] = df
    return df


async def get_candles_async(
    ticker: str,
    interval: str = "1h",
    months_back: int = 6,
    force_refresh: bool = False,
    min_bars: int = MIN_BARS_FOR_STRATEGY,
    max_bars: int | None = None,
) -> pd.DataFrame:
    cache_path = DATA_DIR / f"{ticker}_{interval}.parquet"
    cache_key = (ticker.upper(), interval)

    if not force_refresh:
        cached = _MEMORY_CACHE.get(cache_key)
        if cached is not None and _is_fresh_dataframe(cached, min_bars=min_bars, interval=interval):
            return cached.copy(deep=False)

    if not force_refresh and _validate_cache(cache_path, min_bars=min_bars, interval=interval):
        df = pd.read_parquet(cache_path)
        df = _trim_bars(df, min_bars=min_bars, max_bars=max_bars)
        _MEMORY_CACHE[cache_key] = df
        return df.copy(deep=False)

    # Delete stale cache if it exists
    if cache_path.exists() and not _validate_cache(cache_path, min_bars=min_bars, interval=interval):
        cache_path.unlink(missing_ok=True)
        logger.info(f"Deleted stale cache: {cache_path}")

    df = await _fetch_crypto(ticker, interval, months_back)

    if not df.empty:
        df.to_parquet(cache_path)
        df = _trim_bars(df, min_bars=min_bars, max_bars=max_bars)
        _MEMORY_CACHE[cache_key] = df
    return df.copy(deep=False)


# ── Bybit V5 API ──────────────────────────────────────────────────────────────

async def _fetch_bybit(
    symbol: str,
    interval: str,
    months_back: int,
    session: aiohttp.ClientSession,
) -> pd.DataFrame:
    """Fetch klines from Bybit V5 API."""
    bybit_interval = INTERVAL_MAP.get(interval, "60")
    end_ms = int(datetime.now().timestamp() * 1000)
    start_ms = int((datetime.now() - timedelta(days=30 * months_back)).timestamp() * 1000)

    url = "https://api.bybit.com/v5/market/kline"
    all_rows: list = []
    current_end = end_ms

    while True:
        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": bybit_interval,
            "start": start_ms,
            "end": current_end,
            "limit": 200,
        }
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Bybit {resp.status}, trying OKX...")
                    break
                data = await resp.json()
        except Exception as e:
            logger.warning(f"Bybit fetch error: {e}")
            break

        rows = data.get("result", {}).get("list", [])
        if not rows:
            break
        all_rows.extend(rows)
        # Bybit returns newest-first; oldest timestamp in this batch:
        oldest_ts = int(rows[-1][0])
        if oldest_ts <= start_ms:
            break
        current_end = oldest_ts - 1
        if len(rows) < 200:
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
    df = df.set_index("timestamp").sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume"]]


# ── OKX API (fallback) ────────────────────────────────────────────────────────

async def _fetch_okx(
    symbol: str,
    interval: str,
    months_back: int,
    session: aiohttp.ClientSession,
) -> pd.DataFrame:
    """Fetch candles from OKX API."""
    okx_interval = OKX_INTERVAL_MAP.get(interval, "1H")
    inst_id = symbol.replace("USDT", "-USDT")  # BTCUSDT -> BTC-USDT
    start_ms = int((datetime.now() - timedelta(days=30 * months_back)).timestamp() * 1000)

    url = "https://www.okx.com/api/v5/market/history-candles"
    all_rows: list = []
    after = ""

    while True:
        params: dict = {"instId": inst_id, "bar": okx_interval, "limit": "300"}
        if after:
            params["after"] = after
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"OKX error {resp.status}")
                    break
                data = await resp.json()
        except Exception as e:
            logger.warning(f"OKX fetch error: {e}")
            break

        rows = data.get("data", [])
        if not rows:
            break
        all_rows.extend(rows)
        oldest_ts = int(rows[-1][0])
        if oldest_ts <= start_ms:
            break
        after = str(oldest_ts)
        if len(rows) < 300:
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_rows,
        columns=["timestamp", "open", "high", "low", "close",
                 "volume", "volCcy", "volCcyQuote", "confirm"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
    df = df.set_index("timestamp").sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume"]]


# ── Main fetch function ───────────────────────────────────────────────────────

async def _fetch_crypto(ticker: str, interval: str, months_back: int) -> pd.DataFrame:
    """Fetch BTC data. Tries Bybit first, falls back to OKX."""
    symbol_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    symbol = symbol_map.get(ticker.upper(), ticker.upper())

    async with aiohttp.ClientSession() as session:
        # Try Bybit first
        logger.info(f"Fetching {symbol} from Bybit ({interval}, {months_back}mo)...")
        df = await _fetch_bybit(symbol, interval, months_back, session)
        if not df.empty:
            logger.info(f"Bybit: {len(df)} bars for {symbol}")
            return df

        # Fallback: OKX
        logger.info(f"Bybit empty, trying OKX for {symbol}...")
        df = await _fetch_okx(symbol, interval, months_back, session)
        if not df.empty:
            logger.info(f"OKX: {len(df)} bars for {symbol}")
            return df

    logger.error(f"All sources failed for {ticker}")
    return pd.DataFrame()
