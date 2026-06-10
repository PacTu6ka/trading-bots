"""Bollinger mean-reversion strategy for MOEX shares."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import bollinger, rsi
from .base import Strategy


class BollingerStrategy(Strategy):
    name = "bollinger"
    default_params = {
        "bb_length": 20,
        "bb_std": 2.0,
        "rsi_length": 14,
        "oversold": 35,
        "overbought": 65,
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        close = df["close"]
        bb = bollinger(close, p["bb_length"], p["bb_std"])
        rv = rsi(close, p["rsi_length"])

        long_entry = (close <= bb["lower"]) & (rv < p["oversold"])
        short_entry = (close >= bb["upper"]) & (rv > p["overbought"])
        long_exit = close >= bb["mid"]
        short_exit = close <= bb["mid"]

        out = np.zeros(len(df), dtype=int)
        pos = 0
        for i in range(len(df)):
            if pos == 0:
                if bool(long_entry.iloc[i]):
                    pos = 1
                elif bool(short_entry.iloc[i]):
                    pos = -1
            elif pos == 1 and bool(long_exit.iloc[i]):
                pos = 0
            elif pos == -1 and bool(short_exit.iloc[i]):
                pos = 0
            out[i] = pos

        return pd.Series(out, index=df.index, dtype=int)

    def diagnose(self, df: pd.DataFrame) -> str:
        p = self.params
        close = df["close"]
        bb = bollinger(close, p["bb_length"], p["bb_std"])
        rv = rsi(close, p["rsi_length"])
        return (
            f"BB({p['bb_length']},{p['bb_std']}) "
            f"price={close.iloc[-1]:.2f} lower={bb['lower'].iloc[-1]:.2f} "
            f"upper={bb['upper'].iloc[-1]:.2f} rsi={rv.iloc[-1]:.1f}"
        )

