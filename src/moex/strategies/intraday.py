"""Intraday strategy family for MOEX shares."""

from __future__ import annotations

from datetime import time as dt_time
from typing import Any

import numpy as np
import pandas as pd

from ..indicators import atr, bollinger, ema, rsi, zscore
from .base import Strategy


def _parse_clock(value: str) -> dt_time:
    hour, minute = value.split(":", 1)
    return dt_time(int(hour), int(minute))


def _clock_to_min(value: str) -> int:
    clock = _parse_clock(value)
    return clock.hour * 60 + clock.minute


def _minutes(index: pd.DatetimeIndex) -> np.ndarray:
    return (index.hour * 60 + index.minute).to_numpy()


def _bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].replace(0, np.nan)
    pv = typical * volume
    dates = df.index.normalize()
    return pv.groupby(dates).cumsum() / volume.groupby(dates).cumsum()


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig


def _supertrend_direction(df: pd.DataFrame, period: int = 7, multiplier: float = 2.0) -> pd.Series:
    hl2 = (df["high"] + df["low"]) / 2.0
    band_atr = atr(df["high"], df["low"], df["close"], period)
    upper = hl2 + multiplier * band_atr
    lower = hl2 - multiplier * band_atr

    direction = np.zeros(len(df), dtype=int)
    final_upper = upper.copy()
    final_lower = lower.copy()
    close = df["close"].to_numpy()

    for i in range(1, len(df)):
        if np.isnan(band_atr.iloc[i]):
            direction[i] = direction[i - 1]
            continue

        final_upper.iloc[i] = (
            upper.iloc[i]
            if upper.iloc[i] < final_upper.iloc[i - 1] or close[i - 1] > final_upper.iloc[i - 1]
            else final_upper.iloc[i - 1]
        )
        final_lower.iloc[i] = (
            lower.iloc[i]
            if lower.iloc[i] > final_lower.iloc[i - 1] or close[i - 1] < final_lower.iloc[i - 1]
            else final_lower.iloc[i - 1]
        )

        prev_dir = direction[i - 1]
        if prev_dir == 0:
            direction[i] = 1 if close[i] >= hl2.iloc[i] else -1
        elif prev_dir <= 0 and close[i] > final_upper.iloc[i]:
            direction[i] = 1
        elif prev_dir >= 0 and close[i] < final_lower.iloc[i]:
            direction[i] = -1
        else:
            direction[i] = prev_dir

    return pd.Series(direction, index=df.index, dtype=int)


class IntradayStrategy(Strategy):
    name = "intraday"
    default_params: dict[str, Any] = {
        "kind": "vwap_atr_reversion",
        "session_start": "06:55",
        "no_entry_after": "23:20",
        "force_flat_at": "23:25",
    }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        if df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return pd.Series(0, index=df.index, dtype=int)

        kind = self.params.get("kind", "vwap_atr_reversion")
        raw = getattr(self, f"_signal_{kind}", self._signal_vwap_atr_reversion)(df)
        return self._apply_session_rules(raw, df)

    def _apply_session_rules(self, raw: pd.Series, df: pd.DataFrame | None = None) -> pd.Series:
        start_min = _clock_to_min(self.params.get("session_start", "06:55"))
        no_entry_min = _clock_to_min(self.params.get("no_entry_after", "23:20"))
        flat_min = _clock_to_min(self.params.get("force_flat_at", "23:25"))
        minutes = _minutes(raw.index)
        max_trades_per_day = int(self.params.get("max_trades_per_day", 10_000))
        cooldown_bars = int(self.params.get("cooldown_bars", 0))
        max_signal_age = int(self.params.get("entry_signal_max_age_bars", 10_000))
        allow_reversal = _bool_param(self.params.get("allow_reversal"), default=False)
        long_allowed, short_allowed = self._entry_filter_masks(raw.index, df)
        dates = raw.index.normalize()
        signal_age = self._signal_age(raw, dates)

        out = np.zeros(len(raw), dtype=int)
        pos = 0
        entries_today = 0
        last_exit_i = -10_000
        values = raw.fillna(0).astype(int).to_numpy()
        current_date = None

        def can_enter(i: int, desired: int) -> bool:
            if entries_today >= max_trades_per_day:
                return False
            if i - last_exit_i < cooldown_bars:
                return False
            if signal_age[i] > max_signal_age:
                return False
            return bool(long_allowed[i] if desired > 0 else short_allowed[i])

        for i, desired in enumerate(values):
            if dates[i] != current_date:
                pos = 0
                entries_today = 0
                last_exit_i = -10_000
                current_date = dates[i]

            minute = minutes[i]
            if minute < start_min or minute >= flat_min:
                if pos != 0:
                    last_exit_i = i
                pos = 0
            elif minute >= no_entry_min and pos == 0 and desired != 0:
                pos = 0
            elif desired == pos:
                pos = desired
            elif desired == 0:
                if pos != 0:
                    last_exit_i = i
                pos = 0
            elif minute >= no_entry_min:
                if pos != 0 and desired != pos:
                    last_exit_i = i
                pos = 0
            elif pos == 0:
                if can_enter(i, desired):
                    pos = desired
                    entries_today += 1
            elif allow_reversal and can_enter(i, desired):
                pos = desired
                entries_today += 1
            else:
                last_exit_i = i
                pos = 0
            out[i] = pos

        return pd.Series(out, index=raw.index, dtype=int)

    def _entry_filter_masks(
        self,
        index: pd.DatetimeIndex,
        df: pd.DataFrame | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        if df is None or df.empty:
            allowed = np.ones(len(index), dtype=bool)
            return allowed, allowed.copy()

        long_allowed = pd.Series(True, index=index)
        short_allowed = pd.Series(True, index=index)

        min_volume_ratio = self.params.get("min_volume_ratio")
        if min_volume_ratio is not None and "volume" in df:
            volume_window = int(self.params.get("volume_window", 20))
            volume_avg = df["volume"].rolling(volume_window).mean()
            volume_ok = df["volume"] >= volume_avg * float(min_volume_ratio)
            long_allowed &= volume_ok.fillna(False)
            short_allowed &= volume_ok.fillna(False)

        min_value = self.params.get("min_value")
        if min_value is not None:
            if "value" in df:
                value = df["value"]
            else:
                value = df["close"] * df.get("volume", 0)
            value_ok = value >= float(min_value)
            long_allowed &= value_ok.fillna(False)
            short_allowed &= value_ok.fillna(False)

        atr_pct = None
        if self.params.get("atr_pct_min") is not None or self.params.get("atr_pct_max") is not None:
            atr_pct = atr(
                df["high"],
                df["low"],
                df["close"],
                int(self.params.get("atr_length", 14)),
            ) / df["close"].replace(0, np.nan)

        atr_pct_min = self.params.get("atr_pct_min")
        if atr_pct_min is not None and atr_pct is not None:
            atr_ok = atr_pct >= float(atr_pct_min)
            long_allowed &= atr_ok.fillna(False)
            short_allowed &= atr_ok.fillna(False)

        atr_pct_max = self.params.get("atr_pct_max")
        if atr_pct_max is not None and atr_pct is not None:
            atr_ok = atr_pct <= float(atr_pct_max)
            long_allowed &= atr_ok.fillna(False)
            short_allowed &= atr_ok.fillna(False)

        if _bool_param(self.params.get("use_trend_filter"), default=False):
            trend_fast, trend_slow = self._trend_lines(df)
            long_allowed &= (trend_fast > trend_slow).fillna(False)
            short_allowed &= (trend_fast < trend_slow).fillna(False)

        return long_allowed.to_numpy(dtype=bool), short_allowed.to_numpy(dtype=bool)

    def _trend_lines(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        timeframe = self.params.get("trend_timeframe", "5min")
        fast_len = int(self.params.get("trend_fast", 20))
        slow_len = int(self.params.get("trend_slow", 100))
        close = df["close"].astype(float)

        if timeframe and timeframe != "5min":
            higher_close = close.resample(timeframe, label="left", closed="left").last().dropna()
            fast = ema(higher_close, fast_len).reindex(close.index, method="ffill")
            slow = ema(higher_close, slow_len).reindex(close.index, method="ffill")
            return fast, slow

        return ema(close, fast_len), ema(close, slow_len)

    @staticmethod
    def _signal_age(raw: pd.Series, dates: pd.DatetimeIndex) -> np.ndarray:
        values = raw.fillna(0).astype(int).to_numpy()
        ages = np.full(len(values), 10_000, dtype=int)
        active = 0
        age = 10_000
        current_date = None

        for i, desired in enumerate(values):
            if dates[i] != current_date:
                active = 0
                age = 10_000
                current_date = dates[i]

            if desired == 0:
                active = 0
                age = 10_000
            elif desired != active:
                active = desired
                age = 0
            else:
                age += 1
            ages[i] = age

        return ages

    def _signal_rsi2_reversion(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        rv = rsi(df["close"], p.get("rsi_length", 2))
        return self._state_from_entries(
            rv < p.get("entry_low", 10),
            rv > p.get("entry_high", 90),
            rv > p.get("exit_level", 50),
            rv < p.get("exit_level", 50),
            df.index,
        )

    def _signal_rsi2_trend(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        rv = rsi(df["close"], p.get("rsi_length", 2))
        trend = ema(df["close"], p.get("trend_ema", 100))
        return self._state_from_entries(
            (rv < p.get("entry_low", 10)) & (df["close"] > trend),
            (rv > p.get("entry_high", 90)) & (df["close"] < trend),
            rv > p.get("exit_level", 50),
            rv < p.get("exit_level", 50),
            df.index,
        )

    def _signal_bollinger_reversion(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        bands = bollinger(df["close"], p.get("bb_length", 20), p.get("bb_std", 2.0))
        rv = rsi(df["close"], p.get("rsi_length", 14))
        return self._state_from_entries(
            (df["close"] <= bands["lower"]) & (rv < p.get("rsi_low", 35)),
            (df["close"] >= bands["upper"]) & (rv > p.get("rsi_high", 65)),
            df["close"] >= bands["mid"],
            df["close"] <= bands["mid"],
            df.index,
        )

    def _signal_bb_vwap_reversion(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        bands = bollinger(df["close"], p.get("bb_length", 20), p.get("bb_std", 2.0))
        vw = _session_vwap(df)
        rv = rsi(df["close"], p.get("rsi_length", 14))
        return self._state_from_entries(
            (df["close"] <= bands["lower"]) & (df["close"] < vw) & (rv < p.get("rsi_low", 40)),
            (df["close"] >= bands["upper"]) & (df["close"] > vw) & (rv > p.get("rsi_high", 60)),
            (df["close"] >= vw) | (df["close"] >= bands["mid"]),
            (df["close"] <= vw) | (df["close"] <= bands["mid"]),
            df.index,
        )

    def _signal_vwap_atr_reversion(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        vw = _session_vwap(df)
        band_atr = atr(df["high"], df["low"], df["close"], p.get("atr_length", 14))
        rv = rsi(df["close"], p.get("rsi_length", 14))
        mult = p.get("atr_mult", 1.5)
        return self._state_from_entries(
            (df["close"] < vw - mult * band_atr) & (rv < p.get("rsi_low", 40)),
            (df["close"] > vw + mult * band_atr) & (rv > p.get("rsi_high", 60)),
            df["close"] >= vw,
            df["close"] <= vw,
            df.index,
        )

    def _signal_vwap_pct_reversion(self, df: pd.DataFrame) -> pd.Series:
        threshold = self.params.get("threshold", 0.004)
        dist = (df["close"] - _session_vwap(df)) / _session_vwap(df)
        return self._state_from_entries(dist < -threshold, dist > threshold, dist >= 0, dist <= 0, df.index)

    def _signal_zscore_ema_reversion(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        zv = zscore(df["close"] - ema(df["close"], p.get("ema_length", 50)), p.get("std_window", 50))
        entry = p.get("entry_z", 2.5)
        exit_z = p.get("exit_z", 0.3)
        return self._state_from_entries(zv <= -entry, zv >= entry, zv >= -exit_z, zv <= exit_z, df.index)

    def _signal_zscore_vwap_reversion(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        zv = zscore(df["close"] - _session_vwap(df), p.get("std_window", 50))
        entry = p.get("entry_z", 2.0)
        exit_z = p.get("exit_z", 0.2)
        return self._state_from_entries(zv <= -entry, zv >= entry, zv >= -exit_z, zv <= exit_z, df.index)

    def _signal_macd_momentum(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        line, sig = _macd(df["close"], p.get("fast", 12), p.get("slow", 26), p.get("signal", 9))
        hist = line - sig
        return self._state_from_entries(
            (line > sig) & (hist > hist.shift(1)),
            (line < sig) & (hist < hist.shift(1)),
            line < sig,
            line > sig,
            df.index,
        )

    def _signal_ema_cross(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        fast = ema(df["close"], p.get("fast", 9))
        slow = ema(df["close"], p.get("slow", 21))
        return self._state_from_entries(fast > slow, fast < slow, fast < slow, fast > slow, df.index)

    def _signal_trend_vwap_reclaim(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        fast = ema(df["close"], p.get("fast", 20))
        slow = ema(df["close"], p.get("slow", 100))
        vw = _session_vwap(df)
        rv = rsi(df["close"], p.get("rsi_length", 14))
        long_rsi = p.get("long_rsi_min", 48)
        short_rsi = p.get("short_rsi_max", 52)
        return self._state_from_entries(
            (fast > slow)
            & (df["close"].shift(1) <= vw.shift(1))
            & (df["close"] > vw)
            & (df["close"] > fast)
            & (rv >= long_rsi),
            (fast < slow)
            & (df["close"].shift(1) >= vw.shift(1))
            & (df["close"] < vw)
            & (df["close"] < fast)
            & (rv <= short_rsi),
            (df["close"] < vw) | (fast < slow),
            (df["close"] > vw) | (fast > slow),
            df.index,
        )

    def _signal_supertrend(self, df: pd.DataFrame) -> pd.Series:
        return _supertrend_direction(
            df,
            self.params.get("atr_period", 7),
            self.params.get("atr_mult", 2.0),
        )

    def _signal_opening_range_breakout(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        start_min = _clock_to_min(p.get("session_start", "06:55"))
        range_end_min = start_min + int(p.get("opening_range_minutes", 30))
        minutes = _minutes(df.index)
        dates = df.index.normalize()
        band_atr = atr(df["high"], df["low"], df["close"], p.get("atr_length", 14)).fillna(0.0)
        atr_buffer = float(p.get("atr_buffer", 0.15))

        long_entry = pd.Series(False, index=df.index)
        short_entry = pd.Series(False, index=df.index)
        long_exit = pd.Series(False, index=df.index)
        short_exit = pd.Series(False, index=df.index)

        for date in pd.unique(dates):
            day_pos = np.flatnonzero(dates == date)
            if len(day_pos) == 0:
                continue

            day_minutes = minutes[day_pos]
            range_pos = day_pos[(day_minutes >= start_min) & (day_minutes < range_end_min)]
            if len(range_pos) == 0:
                continue

            high = float(df["high"].iloc[range_pos].max())
            low = float(df["low"].iloc[range_pos].min())
            mid = (high + low) / 2.0
            after_pos = day_pos[day_minutes >= range_end_min]
            if len(after_pos) == 0:
                continue

            closes = df["close"].iloc[after_pos]
            previous = df["close"].shift(1).iloc[after_pos]
            buffer_value = atr_buffer * band_atr.iloc[after_pos]
            if _bool_param(p.get("breakout_cross_only"), default=True):
                long_break = (previous <= high) & (closes > high + buffer_value)
                short_break = (previous >= low) & (closes < low - buffer_value)
            else:
                long_break = closes > high + buffer_value
                short_break = closes < low - buffer_value

            long_entry.iloc[after_pos] = long_break.to_numpy()
            short_entry.iloc[after_pos] = short_break.to_numpy()
            long_exit.iloc[after_pos] = (closes < mid).to_numpy()
            short_exit.iloc[after_pos] = (closes > mid).to_numpy()

        return self._state_from_entries(long_entry, short_entry, long_exit, short_exit, df.index)

    def _signal_donchian_breakout(self, df: pd.DataFrame) -> pd.Series:
        window = self.params.get("window", 20)
        high = df["high"].rolling(window).max().shift(1)
        low = df["low"].rolling(window).min().shift(1)
        mid = (high + low) / 2.0
        return self._state_from_entries(df["close"] > high, df["close"] < low, df["close"] < mid, df["close"] > mid, df.index)

    def _signal_keltner_breakout(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        mid = ema(df["close"], p.get("ema_length", 20))
        band_atr = atr(df["high"], df["low"], df["close"], p.get("atr_length", 14))
        upper = mid + p.get("atr_mult", 1.5) * band_atr
        lower = mid - p.get("atr_mult", 1.5) * band_atr
        return self._state_from_entries(df["close"] > upper, df["close"] < lower, df["close"] < mid, df["close"] > mid, df.index)

    def _signal_keltner_reversion(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        mid = ema(df["close"], p.get("ema_length", 20))
        band_atr = atr(df["high"], df["low"], df["close"], p.get("atr_length", 14))
        upper = mid + p.get("atr_mult", 2.0) * band_atr
        lower = mid - p.get("atr_mult", 2.0) * band_atr
        return self._state_from_entries(df["close"] < lower, df["close"] > upper, df["close"] >= mid, df["close"] <= mid, df.index)

    def _signal_volume_momentum(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        vol_avg = df["volume"].rolling(p.get("volume_window", 20)).mean()
        band_atr = atr(df["high"], df["low"], df["close"], p.get("atr_length", 14))
        move = df["close"].diff()
        threshold = p.get("atr_move_mult", 0.3) * band_atr
        vol_ok = df["volume"] > vol_avg * p.get("volume_mult", 1.5)
        return self._state_from_entries(
            (move > threshold) & vol_ok,
            (move < -threshold) & vol_ok,
            move < 0,
            move > 0,
            df.index,
        )

    @staticmethod
    def _state_from_entries(
        long_entry: pd.Series,
        short_entry: pd.Series,
        long_exit: pd.Series,
        short_exit: pd.Series,
        index: pd.Index,
    ) -> pd.Series:
        out = np.zeros(len(index), dtype=int)
        pos = 0
        le = long_entry.fillna(False).to_numpy()
        se = short_entry.fillna(False).to_numpy()
        lx = long_exit.fillna(False).to_numpy()
        sx = short_exit.fillna(False).to_numpy()

        for i in range(len(index)):
            if pos == 0:
                if le[i]:
                    pos = 1
                elif se[i]:
                    pos = -1
            elif pos == 1 and lx[i]:
                pos = 0
            elif pos == -1 and sx[i]:
                pos = 0
            out[i] = pos

        return pd.Series(out, index=index, dtype=int)

    def diagnose(self, df: pd.DataFrame) -> str:
        if df.empty:
            return "intraday: empty dataframe"
        return (
            f"IntradayStrategy(kind={self.params.get('kind')}) "
            f"close={float(df['close'].iloc[-1]):.2f} "
            f"max_trades_per_day={self.params.get('max_trades_per_day', '-') } "
            f"cooldown_bars={self.params.get('cooldown_bars', '-') } "
            f"no_entry_after={self.params.get('no_entry_after')} "
            f"force_flat_at={self.params.get('force_flat_at')}"
        )
