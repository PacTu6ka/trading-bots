"""MOEX ISS data loader with bounded parquet and memory caches."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import aiohttp
import aiomoex
import pandas as pd

from src.manager.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

INTERVAL_MAP = {
    "1min": 1,
    "10min": 10,
    "1h": 60,
    "1d": 24,
}

RESAMPLE_MAP = {
    "5min": ("1min", "5min", 5),
    "15min": ("1min", "15min", 15),
    "30min": ("1min", "30min", 30),
}

DATA_DIR = PROJECT_ROOT / "data" / "moex"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MOEX_MAX_BARS = 600
_MEMORY_CACHE: dict[tuple[str, str], pd.DataFrame] = {}

LOT_SIZES = {
    "SBER": 1,
    "NLMK": 10,
    "MTSS": 10,
    "SNGSP": 10,
    "NVTK": 1,
    "GAZP": 10,
    "LKOH": 1,
    "ROSN": 1,
    "VTBR": 10000,
    "YDEX": 1,
    "PLZL": 1,
    "T": 1,
    "X5": 1,
    "GMKN": 10,
    "MGNT": 1,
    "ALRS": 10,
    "AFLT": 10,
    "CHMF": 1,
    "MOEX": 10,
    "PIKK": 10,
}


def get_lot_size(ticker: str) -> int:
    return LOT_SIZES.get(ticker.upper(), 1)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 1)
    except ValueError:
        logger.warning("%s=%r is not an integer, using %s", name, raw, default)
        return default


def _max_bars(max_bars: int | None = None, min_bars: int = 1) -> int:
    configured = max_bars
    if configured is None:
        configured = _env_int("MOEX_MARKET_DATA_MAX_BARS", _env_int("MARKET_DATA_MAX_BARS", DEFAULT_MOEX_MAX_BARS))
    return max(configured, min_bars)


def _trim_bars(df: pd.DataFrame, max_bars: int | None = None, min_bars: int = 1) -> pd.DataFrame:
    if df.empty:
        return df
    return df.tail(_max_bars(max_bars=max_bars, min_bars=min_bars))


def _cache_path(ticker: str, interval: str) -> Path:
    return DATA_DIR / f"{ticker.upper()}_{interval}.parquet"


def get_candles(
    ticker: str,
    interval: str = "1h",
    months_back: int = 6,
    force_refresh: bool = False,
    max_bars: int | None = None,
    min_bars: int = 1,
) -> pd.DataFrame:
    return asyncio.run(
        get_candles_async(
            ticker=ticker,
            interval=interval,
            months_back=months_back,
            force_refresh=force_refresh,
            max_bars=max_bars,
            min_bars=min_bars,
        )
    )


async def get_candles_async(
    ticker: str,
    interval: str = "1h",
    months_back: int = 6,
    force_refresh: bool = False,
    max_bars: int | None = None,
    min_bars: int = 1,
) -> pd.DataFrame:
    ticker = ticker.upper()
    cache_key = (ticker, interval)

    if not force_refresh:
        cached = _MEMORY_CACHE.get(cache_key)
        if cached is not None and len(cached) >= min_bars:
            return cached.copy(deep=False)

        path = _cache_path(ticker, interval)
        if path.exists():
            try:
                df = pd.read_parquet(path)
                df = _trim_bars(df, max_bars=max_bars, min_bars=min_bars)
                _MEMORY_CACHE[cache_key] = df
                return df.copy(deep=False)
            except Exception as e:
                logger.warning("Cannot read MOEX cache %s: %s", path.name, e)

    if interval in RESAMPLE_MAP:
        source_interval, rule, multiplier = RESAMPLE_MAP[interval]
        source_max_bars = _max_bars(max_bars=max_bars, min_bars=min_bars) * multiplier
        source = await get_candles_async(
            ticker=ticker,
            interval=source_interval,
            months_back=months_back,
            force_refresh=force_refresh,
            max_bars=source_max_bars,
            min_bars=min_bars * multiplier,
        )
        df = _resample(source, rule) if not source.empty else source
    else:
        df = await _fetch_moex(ticker, interval, months_back)

    df = _trim_bars(df, max_bars=max_bars, min_bars=min_bars)
    if not df.empty:
        _MEMORY_CACHE[cache_key] = df
        try:
            df.to_parquet(_cache_path(ticker, interval))
        except Exception as e:
            logger.warning("Cannot write MOEX cache for %s %s: %s", ticker, interval, e)

    return df.copy(deep=False)


def merge_history(
    old: pd.DataFrame | None,
    new: pd.DataFrame,
    max_bars: int | None = None,
    min_bars: int = 1,
) -> pd.DataFrame:
    if old is None or old.empty:
        return _trim_bars(new, max_bars=max_bars, min_bars=min_bars)
    if new.empty:
        return _trim_bars(old, max_bars=max_bars, min_bars=min_bars)
    combined = pd.concat([old, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return _trim_bars(combined, max_bars=max_bars, min_bars=min_bars)


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "value" in df.columns:
        agg["value"] = "sum"
    return df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open"])


async def _fetch_moex(ticker: str, interval: str, months_back: int) -> pd.DataFrame:
    if interval not in INTERVAL_MAP:
        raise ValueError(f"Unsupported MOEX interval: {interval}")

    iss_interval = INTERVAL_MAP[interval]
    end = datetime.now()
    start = (pd.Timestamp(end).normalize() - pd.DateOffset(months=months_back)).to_pydatetime()

    async with aiohttp.ClientSession() as session:
        try:
            data = await aiomoex.get_market_candles(
                session,
                security=ticker,
                interval=iss_interval,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                market="shares",
                engine="stock",
            )
        except Exception as e:
            logger.error("MOEX ISS error for %s: %s", ticker, e)
            return pd.DataFrame()

    if not data:
        logger.warning("No MOEX candles returned for %s %s", ticker, interval)
        return pd.DataFrame()

    df = pd.DataFrame(data)
    df["begin"] = pd.to_datetime(df["begin"])
    df = df.rename(columns={"begin": "timestamp"}).set_index("timestamp").sort_index()
    keep_cols = ["open", "high", "low", "close", "volume", "value"]
    df = df[[col for col in keep_cols if col in df.columns]]
    return df[~df.index.duplicated(keep="last")]
