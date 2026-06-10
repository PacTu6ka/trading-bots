"""MOEX portfolio presets used by the portfolio bot."""

from __future__ import annotations

from typing import Any

from .strategies import (
    BollingerStrategy,
    KeltnerStrategy,
    RSI2Strategy,
    ZScoreBBAndStrategy,
    ZScoreStrategy,
)


MOEX_PRESETS: dict[str, dict[str, Any]] = {
    "bollinger": {
        "bb_length": 30,
        "bb_std": 3.0,
        "rsi_length": 14,
        "oversold": 30,
        "overbought": 70,
    },
    "keltner_20": {
        "ema_length": 20,
        "atr_length": 14,
        "atr_mult": 2.0,
        "rsi_length": 14,
        "rsi_low": 35,
        "rsi_high": 65,
    },
    "keltner_30": {
        "ema_length": 30,
        "atr_length": 14,
        "atr_mult": 3.0,
        "rsi_length": 14,
        "rsi_low": 30,
        "rsi_high": 70,
    },
    "rsi2_tight": {
        "rsi_length": 2,
        "entry_low": 5,
        "entry_high": 95,
        "exit_low": 50,
        "exit_high": 50,
        "trend_ema": 50,
        "use_trend_filter": True,
    },
    "rsi2_default": {
        "rsi_length": 2,
        "entry_low": 10,
        "entry_high": 90,
        "exit_low": 60,
        "exit_high": 40,
        "trend_ema": 100,
        "use_trend_filter": True,
    },
    "zscore": {
        "ema_length": 100,
        "std_window": 50,
        "entry_z": 2.5,
        "exit_z": 0.5,
        "stop_z": 4.0,
    },
    "zscore_bb_and": {
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
    },
}

TICKER_STRATEGY_MAP: dict[str, dict[str, Any]] = {
    "SBER": {"type": "keltner_20"},
    "NLMK": {"type": "zscore_bb_and"},
    "MTSS": {"type": "zscore", "entry_z": 2.5},
    "SNGSP": {"type": "zscore", "entry_z": 2.0},
    "NVTK": {"type": "bollinger", "bb_length": 40},
    "GAZP": {"type": "rsi2_tight"},
    "MOEX": {"type": "keltner_30"},
    "ALRS": {"type": "rsi2_default"},
    "GMKN": {"type": "rsi2_tight"},
    "AFLT": {"type": "keltner_30"},
    "LKOH": {"type": "rsi2_tight"},
    "CHMF": {"type": "bollinger", "bb_length": 40},
    "MGNT": {"type": "rsi2_default"},
}

PORTFOLIO_TICKERS = [
    "SBER",
    "NLMK",
    "MTSS",
    "SNGSP",
    "NVTK",
    "GAZP",
    "MOEX",
    "ALRS",
    "GMKN",
    "AFLT",
    "LKOH",
    "CHMF",
    "MGNT",
]


def make_strategy(ticker: str):
    cfg = TICKER_STRATEGY_MAP.get(ticker.upper(), {"type": "zscore", "entry_z": 2.5})
    stype = cfg["type"]

    if stype == "bollinger":
        params = {**MOEX_PRESETS["bollinger"], **{k: v for k, v in cfg.items() if k != "type"}}
        return BollingerStrategy(params=params)
    if stype in ("keltner", "keltner_20", "keltner_30"):
        preset_key = stype if stype in MOEX_PRESETS else "keltner_20"
        params = {**MOEX_PRESETS[preset_key], **{k: v for k, v in cfg.items() if k != "type"}}
        return KeltnerStrategy(params=params)
    if stype in ("rsi2", "rsi2_tight", "rsi2_default"):
        preset_key = stype if stype in MOEX_PRESETS else "rsi2_tight"
        params = {**MOEX_PRESETS[preset_key], **{k: v for k, v in cfg.items() if k != "type"}}
        return RSI2Strategy(params=params)
    if stype == "zscore_bb_and":
        return ZScoreBBAndStrategy(params=MOEX_PRESETS["zscore_bb_and"])

    params = {**MOEX_PRESETS["zscore"], **{k: v for k, v in cfg.items() if k != "type"}}
    return ZScoreStrategy(params=params)

