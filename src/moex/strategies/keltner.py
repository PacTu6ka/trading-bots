"""Keltner channel mean-reversion strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import atr, ema, rsi
from .base import Strategy


class KeltnerStrategy(Strategy):
    name = "keltner"
    default_params = {
        "ema_length": 20,
        "atr_length": 14,
        "atr_mult": 2.0,
        "rsi_length": 14,
        "rsi_low": 35,
        "rsi_high": 65,
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        close = df["close"]
        mid = ema(close, p["ema_length"])
        band_atr = atr(df["high"], df["low"], close, p["atr_length"])
        upper = mid + p["atr_mult"] * band_atr
        lower = mid - p["atr_mult"] * band_atr
        rv = rsi(close, p["rsi_length"])

        out = np.zeros(len(df), dtype=int)
        pos = 0
        for i in range(len(df)):
            if np.isnan(upper.iloc[i]) or np.isnan(rv.iloc[i]):
                out[i] = pos
                continue
            if pos == 0:
                if close.iloc[i] <= lower.iloc[i] and rv.iloc[i] < p["rsi_low"]:
                    pos = 1
                elif close.iloc[i] >= upper.iloc[i] and rv.iloc[i] > p["rsi_high"]:
                    pos = -1
            elif pos == 1 and close.iloc[i] >= mid.iloc[i]:
                pos = 0
            elif pos == -1 and close.iloc[i] <= mid.iloc[i]:
                pos = 0
            out[i] = pos

        return pd.Series(out, index=df.index, dtype=int)

    def diagnose(self, df: pd.DataFrame) -> str:
        p = self.params
        close = df["close"]
        mid = ema(close, p["ema_length"])
        band_atr = atr(df["high"], df["low"], close, p["atr_length"])
        upper = mid + p["atr_mult"] * band_atr
        lower = mid - p["atr_mult"] * band_atr
        rv = rsi(close, p["rsi_length"])
        return (
            f"Keltner({p['ema_length']},x{p['atr_mult']}) "
            f"price={close.iloc[-1]:.2f} lower={lower.iloc[-1]:.2f} "
            f"upper={upper.iloc[-1]:.2f} rsi={rv.iloc[-1]:.1f}"
        )

