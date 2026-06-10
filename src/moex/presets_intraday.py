"""Live intraday presets for the MOEX intraday bot."""

from __future__ import annotations

from typing import Any

from .strategies import IntradayStrategy


INTRADAY_COMMON = {
    "session_start": "06:55",
    "no_entry_after": "23:20",
    "force_flat_at": "23:25",
}

INTRADAY_CANDIDATES: dict[str, dict[str, Any]] = {
    "bollinger_wide_reversion": {
        "kind": "bollinger_reversion",
        "bb_length": 40,
        "bb_std": 2.5,
        "rsi_low": 35,
        "rsi_high": 65,
    },
    "supertrend": {"kind": "supertrend", "atr_period": 7, "atr_mult": 2.0},
    "supertrend_slow": {"kind": "supertrend", "atr_period": 14, "atr_mult": 3.0},
}

INTRADAY_STRATEGY_MAP: dict[str, dict[str, Any]] = {
    "MGNT": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend"]},
    "NLMK": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend"]},
    "CHMF": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend_slow"]},
    "NVTK": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend_slow"]},
    "GAZP": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend_slow"]},
    "AFLT": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["bollinger_wide_reversion"]},
}


def make_intraday_strategy(ticker: str) -> IntradayStrategy:
    cfg = INTRADAY_STRATEGY_MAP.get(ticker.upper())
    if cfg is None:
        raise KeyError(f"No intraday strategy configured for {ticker}")
    return IntradayStrategy(params=cfg)

