"""Live intraday presets for the MOEX intraday bot."""

from __future__ import annotations

from typing import Any

from .strategies import IntradayStrategy


INTRADAY_UNIVERSE = [
    "SBER",
    "GAZP",
    "LKOH",
    "ROSN",
    "T",
    "YDEX",
    "PLZL",
    "GMKN",
    "MOEX",
    "SNGSP",
    "MTSS",
    "ALRS",
    "PIKK",
    "X5",
    "NVTK",
    "NLMK",
    "CHMF",
    "MGNT",
    "AFLT",
]

INTRADAY_COMMON = {
    "session_start": "06:55",
    "no_entry_after": "20:45",
    "force_flat_at": "23:25",
    "allow_reversal": False,
    "entry_signal_max_age_bars": 2,
}

INTRADAY_CANDIDATES: dict[str, dict[str, Any]] = {
    "bollinger_wide_reversion": {
        "kind": "bollinger_reversion",
        "bb_length": 40,
        "bb_std": 2.5,
        "rsi_low": 35,
        "rsi_high": 65,
    },
    "supertrend": {
        "kind": "supertrend",
        "atr_period": 7,
        "atr_mult": 2.0,
        "max_trades_per_day": 1,
        "cooldown_bars": 24,
    },
    "supertrend_slow": {
        "kind": "supertrend",
        "atr_period": 14,
        "atr_mult": 3.0,
        "max_trades_per_day": 1,
        "cooldown_bars": 24,
    },
    "opening_range_breakout_30m": {
        "kind": "opening_range_breakout",
        "opening_range_minutes": 30,
        "atr_length": 14,
        "atr_buffer": 0.20,
        "max_trades_per_day": 1,
        "cooldown_bars": 24,
        "use_trend_filter": True,
        "trend_timeframe": "30min",
        "trend_fast": 10,
        "trend_slow": 30,
        "volume_window": 24,
        "min_volume_ratio": 1.10,
        "atr_pct_min": 0.0005,
    },
    "trend_vwap_reclaim": {
        "kind": "trend_vwap_reclaim",
        "fast": 20,
        "slow": 100,
        "rsi_length": 14,
        "long_rsi_min": 52,
        "short_rsi_max": 48,
        "max_trades_per_day": 2,
        "cooldown_bars": 18,
        "use_trend_filter": True,
        "trend_timeframe": "30min",
        "trend_fast": 10,
        "trend_slow": 30,
        "volume_window": 24,
        "min_volume_ratio": 1.05,
        "atr_pct_min": 0.0004,
    },
    "donchian_slow_filtered": {
        "kind": "donchian_breakout",
        "window": 72,
        "max_trades_per_day": 1,
        "cooldown_bars": 30,
        "use_trend_filter": True,
        "trend_timeframe": "30min",
        "trend_fast": 10,
        "trend_slow": 30,
        "volume_window": 24,
        "min_volume_ratio": 1.10,
        "atr_pct_min": 0.0005,
    },
    "keltner_breakout_filtered": {
        "kind": "keltner_breakout",
        "ema_length": 50,
        "atr_length": 14,
        "atr_mult": 2.2,
        "max_trades_per_day": 1,
        "cooldown_bars": 30,
        "use_trend_filter": True,
        "trend_timeframe": "30min",
        "trend_fast": 10,
        "trend_slow": 30,
        "volume_window": 24,
        "min_volume_ratio": 1.10,
        "atr_pct_min": 0.0005,
    },
    "vwap_deep_limited": {
        "kind": "vwap_atr_reversion",
        "atr_length": 14,
        "atr_mult": 3.0,
        "rsi_low": 30,
        "rsi_high": 70,
        "max_trades_per_day": 1,
        "cooldown_bars": 30,
        "volume_window": 24,
        "min_volume_ratio": 0.95,
        "atr_pct_min": 0.0005,
    },
}

INTRADAY_STRATEGY_MAP: dict[str, dict[str, Any]] = {
    # Live default is intentionally narrow: these were the only top-10 shares
    # with positive full-period and latest test-month results in the current
    # 6-month research run while staying under 1.5 trades/day.
    "NVTK": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend"]},
    "GAZP": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend_slow"]},
    "PLZL": {**INTRADAY_COMMON, **INTRADAY_CANDIDATES["supertrend_slow"]},
}


def make_intraday_strategy(ticker: str) -> IntradayStrategy:
    cfg = INTRADAY_STRATEGY_MAP.get(ticker.upper())
    if cfg is None:
        raise KeyError(f"No intraday strategy configured for {ticker}")
    return IntradayStrategy(params=cfg)
