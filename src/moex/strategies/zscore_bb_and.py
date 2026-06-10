"""Z-score and Bollinger confirmation strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy
from .bollinger import BollingerStrategy
from .zscore import ZScoreStrategy


class ZScoreBBAndStrategy(Strategy):
    name = "zscore_bb_and"
    default_params = {
        "ema_length": 100,
        "std_window": 50,
        "entry_z": 2.0,
        "exit_z": 0.5,
        "stop_z": 4.0,
        "bb_period": 20,
        "bb_std": 2.5,
        "rsi_period": 14,
        "rsi_low": 35,
        "rsi_high": 65,
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        zs_sig = ZScoreStrategy(
            params={
                "ema_length": p["ema_length"],
                "std_window": p["std_window"],
                "entry_z": p["entry_z"],
                "exit_z": p["exit_z"],
                "stop_z": p["stop_z"],
            }
        ).generate_signals(df)
        bb_sig = BollingerStrategy(
            params={
                "bb_length": p["bb_period"],
                "bb_std": p["bb_std"],
                "rsi_length": p["rsi_period"],
                "oversold": p["rsi_low"],
                "overbought": p["rsi_high"],
            }
        ).generate_signals(df)

        out = np.zeros(len(df), dtype=int)
        pos = 0
        for i in range(len(df)):
            if pos == 0:
                if zs_sig.iloc[i] == 1 and bb_sig.iloc[i] == 1:
                    pos = 1
                elif zs_sig.iloc[i] == -1 and bb_sig.iloc[i] == -1:
                    pos = -1
            elif zs_sig.iloc[i] == 0:
                pos = 0
            out[i] = pos

        return pd.Series(out, index=df.index, dtype=int)

    def diagnose(self, df: pd.DataFrame) -> str:
        return "ZScoreBBAnd: entry requires both zscore and bollinger confirmation"

