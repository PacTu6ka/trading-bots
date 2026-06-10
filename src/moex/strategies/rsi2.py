"""RSI(2) mean-reversion strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import ema, rsi
from .base import Strategy


class RSI2Strategy(Strategy):
    name = "rsi2"
    default_params = {
        "rsi_length": 2,
        "entry_low": 5,
        "entry_high": 95,
        "exit_low": 50,
        "exit_high": 50,
        "trend_ema": 50,
        "use_trend_filter": True,
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        rv = rsi(df["close"], p["rsi_length"])
        trend = ema(df["close"], p["trend_ema"])
        out = np.zeros(len(df), dtype=int)
        pos = 0

        for i in range(len(df)):
            if np.isnan(rv.iloc[i]) or np.isnan(trend.iloc[i]):
                out[i] = pos
                continue
            price = df["close"].iloc[i]
            if pos == 0:
                if rv.iloc[i] < p["entry_low"] and (
                    not p["use_trend_filter"] or price > trend.iloc[i]
                ):
                    pos = 1
                elif rv.iloc[i] > p["entry_high"] and (
                    not p["use_trend_filter"] or price < trend.iloc[i]
                ):
                    pos = -1
            elif pos == 1 and rv.iloc[i] > p["exit_low"]:
                pos = 0
            elif pos == -1 and rv.iloc[i] < p["exit_high"]:
                pos = 0
            out[i] = pos

        return pd.Series(out, index=df.index, dtype=int)

    def diagnose(self, df: pd.DataFrame) -> str:
        p = self.params
        rv = rsi(df["close"], p["rsi_length"])
        trend = ema(df["close"], p["trend_ema"])
        return (
            f"RSI2 rsi={rv.iloc[-1]:.1f} price={df['close'].iloc[-1]:.2f} "
            f"ema={trend.iloc[-1]:.2f}"
        )

