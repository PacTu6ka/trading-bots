"""Technical indicators used by MOEX strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=1).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def bollinger(series: pd.Series, length: int = 20, std_mult: float = 2.0) -> pd.DataFrame:
    mid = series.rolling(window=length).mean()
    std = series.rolling(window=length).std()
    return pd.DataFrame(
        {
            "lower": mid - std_mult * std,
            "mid": mid,
            "upper": mid + std_mult * std,
        }
    )


def zscore(series: pd.Series, length: int = 20) -> pd.Series:
    mean = series.rolling(window=length).mean()
    std = series.rolling(window=length).std()
    return (series - mean) / std.replace(0, np.nan)

