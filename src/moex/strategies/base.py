"""Base class for MOEX trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class Strategy(ABC):
    name: str = "base"
    default_params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = {**self.default_params, **(params or {})}

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return a signal series: 1=long, -1=short, 0=flat."""

    def diagnose(self, df: pd.DataFrame) -> str:
        return f"{self.name}: diagnose not implemented"

    def describe(self) -> str:
        return f"{self.name}({self.params})"

