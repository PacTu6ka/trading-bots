"""Multi-confirmation divergence signals for BTC trend bot."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..indicators import bollinger, ema, rsi


@dataclass
class FrameSignal:
    interval: str
    direction: int = 0
    score: int = 0
    close: float = 0.0
    reasons: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)

    @property
    def side(self) -> str:
        return {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(self.direction, "FLAT")


class TrendDivergenceStrategy:
    """Scores RSI/MACD divergence with Bollinger, EMA, volume and structure."""

    def __init__(
        self,
        rsi_length: int = 14,
        bb_length: int = 40,
        bb_std: float = 2.5,
        ema_length: int = 200,
        pivot_window: int = 3,
    ):
        self.rsi_length = rsi_length
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.ema_length = ema_length
        self.pivot_window = pivot_window

    def score(self, df: pd.DataFrame, interval: str) -> FrameSignal:
        if len(df) < max(self.bb_length, self.ema_length, self.rsi_length) + 10:
            return FrameSignal(interval=interval, close=self._last_close(df))

        work = df.copy()
        work["rsi"] = rsi(work["close"], self.rsi_length)
        macd_line, macd_signal = self._macd(work["close"])
        work["macd_hist"] = macd_line - macd_signal
        work["ema"] = ema(work["close"], self.ema_length)
        bb = bollinger(work["close"], self.bb_length, self.bb_std)
        work = work.join(bb)

        long_score, long_reasons, long_details = self._score_direction(work, direction=1)
        short_score, short_reasons, short_details = self._score_direction(work, direction=-1)

        close = self._last_close(work)
        base_details = self._base_details(work)
        if long_score <= 0 and short_score <= 0:
            return FrameSignal(interval=interval, close=close, details=base_details)

        if long_score >= short_score:
            return FrameSignal(
                interval=interval,
                direction=1,
                score=long_score,
                close=close,
                reasons=long_reasons,
                details={**base_details, **long_details},
            )
        return FrameSignal(
            interval=interval,
            direction=-1,
            score=short_score,
            close=close,
            reasons=short_reasons,
            details={**base_details, **short_details},
        )

    def _score_direction(self, df: pd.DataFrame, direction: int) -> tuple[int, list[str], dict[str, str]]:
        score = 0
        reasons: list[str] = []
        details: dict[str, str] = {}

        rsi_div, rsi_details = self._divergence_details(df, "rsi", direction)
        if rsi_div:
            score += 2
            reasons.append("rsi_div")
            details.update({f"rsi_{k}": v for k, v in rsi_details.items()})

        macd_div, macd_details = self._divergence_details(df, "macd_hist", direction)
        if macd_div:
            score += 2
            reasons.append("macd_div")
            details.update({f"macd_{k}": v for k, v in macd_details.items()})

        if score == 0:
            return 0, [], details

        last = df.iloc[-1]
        close = float(last["close"])
        ema_value = float(last["ema"])

        if direction == 1:
            if close <= float(last["lower"]) or df["close"].iloc[-2] <= df["lower"].iloc[-2]:
                score += 1
                reasons.append("lower_bb")
            if close >= ema_value:
                score += 1
                reasons.append("ema_ok")
            else:
                score -= 2
                reasons.append("against_ema")
            if self._breaks_structure(df, direction=1):
                score += 1
                reasons.append("structure_break")
        else:
            if close >= float(last["upper"]) or df["close"].iloc[-2] >= df["upper"].iloc[-2]:
                score += 1
                reasons.append("upper_bb")
            if close <= ema_value:
                score += 1
                reasons.append("ema_ok")
            else:
                score -= 2
                reasons.append("against_ema")
            if self._breaks_structure(df, direction=-1):
                score += 1
                reasons.append("structure_break")

        if self._volume_confirms(df):
            score += 1
            reasons.append("volume")

        return score, reasons, details

    def _has_divergence(self, df: pd.DataFrame, indicator: str, direction: int) -> bool:
        found, _ = self._divergence_details(df, indicator, direction)
        return found

    def _divergence_details(
        self,
        df: pd.DataFrame,
        indicator: str,
        direction: int,
    ) -> tuple[bool, dict[str, str]]:
        pivots = self._pivot_lows(df) if direction == 1 else self._pivot_highs(df)
        if len(pivots) < 2:
            return False, {}

        first, second = pivots[-2], pivots[-1]
        price_first = float(df["close"].iloc[first])
        price_second = float(df["close"].iloc[second])
        ind_first = df[indicator].iloc[first]
        ind_second = df[indicator].iloc[second]
        if pd.isna(ind_first) or pd.isna(ind_second):
            return False, {}

        details = {
            "pivot1": str(df.index[first]),
            "pivot2": str(df.index[second]),
            "price1": f"{price_first:.2f}",
            "price2": f"{price_second:.2f}",
            "value1": f"{float(ind_first):.2f}",
            "value2": f"{float(ind_second):.2f}",
            "bars_ago": str(len(df) - 1 - second),
        }

        if direction == 1:
            return price_second < price_first and float(ind_second) > float(ind_first), details
        return price_second > price_first and float(ind_second) < float(ind_first), details

    def _breaks_structure(self, df: pd.DataFrame, direction: int) -> bool:
        close = float(df["close"].iloc[-1])
        if direction == 1:
            highs = self._pivot_highs(df)
            return bool(highs and close > float(df["high"].iloc[highs[-1]]))
        lows = self._pivot_lows(df)
        return bool(lows and close < float(df["low"].iloc[lows[-1]]))

    def _volume_confirms(self, df: pd.DataFrame) -> bool:
        if "volume" not in df or len(df) < 25:
            return False
        avg_volume = df["volume"].tail(21).iloc[:-1].mean()
        return bool(avg_volume > 0 and df["volume"].iloc[-1] > avg_volume * 1.2)

    def _pivot_lows(self, df: pd.DataFrame) -> list[int]:
        lows = df["low"].to_numpy()
        pivots: list[int] = []
        w = self.pivot_window
        for i in range(w, len(lows) - w):
            window = lows[i - w:i + w + 1]
            if lows[i] == window.min():
                pivots.append(i)
        return pivots

    def _pivot_highs(self, df: pd.DataFrame) -> list[int]:
        highs = df["high"].to_numpy()
        pivots: list[int] = []
        w = self.pivot_window
        for i in range(w, len(highs) - w):
            window = highs[i - w:i + w + 1]
            if highs[i] == window.max():
                pivots.append(i)
        return pivots

    @staticmethod
    def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series]:
        macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        return macd_line, signal_line

    @staticmethod
    def _last_close(df: pd.DataFrame) -> float:
        if df.empty or "close" not in df:
            return 0.0
        return float(df["close"].iloc[-1])

    @staticmethod
    def _base_details(df: pd.DataFrame) -> dict[str, str]:
        last = df.iloc[-1]
        volume = float(last.get("volume", 0.0))
        avg_volume = df["volume"].tail(21).iloc[:-1].mean() if "volume" in df and len(df) >= 21 else 0
        return {
            "last_bar": str(df.index[-1]),
            "rsi": f"{float(last['rsi']):.2f}",
            "macd_hist": f"{float(last['macd_hist']):.2f}",
            "ema200": f"{float(last['ema']):.2f}",
            "bb_lower": f"{float(last['lower']):.2f}",
            "bb_mid": f"{float(last['mid']):.2f}",
            "bb_upper": f"{float(last['upper']):.2f}",
            "volume_ratio": f"{(volume / avg_volume):.2f}" if avg_volume else "n/a",
        }
