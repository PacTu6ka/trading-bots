"""Base strategy class."""

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
        """Returns: 1=long, -1=short, 0=flat."""

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(params={self.params})"

    def describe(self) -> str:
        return f"{self.name}({self.params})"
