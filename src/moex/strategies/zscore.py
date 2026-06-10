"""Z-score mean-reversion strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import ema, zscore
from .base import Strategy


class ZScoreStrategy(Strategy):
    name = "zscore"
    default_params = {
        "ema_length": 100,
        "std_window": 50,
        "entry_z": 2.5,
        "exit_z": 0.5,
        "stop_z": 4.0,
        "use_volume_filter": False,
        "vol_window": 20,
        "vol_mult": 1.8,
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        spread = df["close"] - ema(df["close"], p["ema_length"])
        zv = zscore(spread, p["std_window"])

        if p.get("use_volume_filter", False) and "volume" in df.columns:
            avg_vol = df["volume"].astype(float).rolling(p.get("vol_window", 20)).mean()
            normal_vol = (df["volume"] <= avg_vol * p.get("vol_mult", 1.8)).to_numpy()
        else:
            normal_vol = np.ones(len(df), dtype=bool)

        out = np.zeros(len(df), dtype=int)
        pos = 0
        for i in range(len(df)):
            if np.isnan(zv.iloc[i]):
                out[i] = pos
                continue
            if pos == 0:
                if zv.iloc[i] <= -p["entry_z"] and normal_vol[i]:
                    pos = 1
                elif zv.iloc[i] >= p["entry_z"] and normal_vol[i]:
                    pos = -1
            elif pos == 1 and (zv.iloc[i] >= -p["exit_z"] or zv.iloc[i] <= -p["stop_z"]):
                pos = 0
            elif pos == -1 and (zv.iloc[i] <= p["exit_z"] or zv.iloc[i] >= p["stop_z"]):
                pos = 0
            out[i] = pos

        return pd.Series(out, index=df.index, dtype=int)

    def diagnose(self, df: pd.DataFrame) -> str:
        p = self.params
        ma = ema(df["close"], p["ema_length"])
        zv = zscore(df["close"] - ma, p["std_window"])
        return f"ZScore price={df['close'].iloc[-1]:.2f} ema={ma.iloc[-1]:.2f} z={zv.iloc[-1]:+.2f}"

