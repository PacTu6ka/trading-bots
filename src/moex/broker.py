"""ArenaGo broker for MOEX shares with share/unit conversion."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://arenago.ru/api"

ARENAGO_LOT_SIZES: dict[str, int] = {
    "SBER": 1,
    "NVTK": 1,
    "LKOH": 1,
    "MGNT": 1,
    "CHMF": 1,
    "ROSN": 1,
    "T": 1,
    "YDEX": 1,
    "PLZL": 1,
    "X5": 1,
    "NLMK": 10,
    "MTSS": 10,
    "SNGSP": 10,
    "GAZP": 10,
    "ALRS": 10,
    "GMKN": 10,
    "AFLT": 10,
    "MOEX": 10,
    "PIKK": 10,
}


class OrderSide(Enum):
    BUY = "B"
    SELL = "S"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"


@dataclass
class Order:
    order_id: str
    side: OrderSide
    ticker: str
    quantity: int
    api_quantity: int = 0
    filled_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING


@dataclass
class Position:
    ticker: str
    quantity: int
    api_quantity: int = 0
    avg_price: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class BotInfo:
    name: str
    cash_balance: float


class ArenaGoBroker:
    def __init__(
        self,
        token: str,
        bot_name: str,
        arena_lot_sizes: dict[str, int] | None = None,
    ):
        token = token.strip()
        if not token:
            raise ValueError("ArenaGo token is required")
        self.token = token
        self.bot_name = bot_name
        self._arena_lot_sizes = arena_lot_sizes or ARENAGO_LOT_SIZES
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json", "Authorization": token})
        self._prices: dict[str, float] = {}
        self._positions_cache: dict[str, Position] = {}
        self._cash_balance = 0.0

    def _get_lot_size(self, ticker: str) -> int:
        return self._arena_lot_sizes.get(ticker.upper(), 1)

    def _shares_to_api(self, ticker: str, shares: int) -> int:
        lot_size = self._get_lot_size(ticker)
        if lot_size <= 1:
            return max(1, shares)
        return max(1, shares // lot_size)

    def _api_to_shares(self, ticker: str, api_qty: int) -> int:
        return api_qty * self._get_lot_size(ticker)

    def update_price(self, ticker: str, price: float) -> None:
        self._prices[ticker.upper()] = price

    def submit_market_order(self, ticker: str, side: OrderSide, quantity: int) -> Order:
        ticker = ticker.upper()
        api_quantity = self._shares_to_api(ticker, abs(quantity))
        payload = {
            "direction": side.value,
            "secid": ticker,
            "quantity": api_quantity,
            "bot": self.bot_name,
        }

        try:
            resp = self._session.post(f"{BASE_URL}/submit_order", json=payload, timeout=30)
            data = resp.json()
        except Exception as e:
            logger.error("ArenaGo order error for %s: %s", ticker, e)
            return Order(f"error-{int(time.time())}", side, ticker, quantity, api_quantity, None, OrderStatus.REJECTED)

        if resp.status_code != 200 or data.get("error") or data.get("success") is False:
            error_msg = data.get("error", data.get("message", data))
            logger.error("ArenaGo order rejected for %s: %s", ticker, error_msg)
            return Order(f"rejected-{int(time.time())}", side, ticker, quantity, api_quantity, None, OrderStatus.REJECTED)

        filled_price = float(data.get("price") or 0)
        remaining_cash = data.get("remaining_cash")
        if remaining_cash is not None:
            self._cash_balance = float(remaining_cash)

        actual_shares = self._api_to_shares(ticker, api_quantity)
        order_id = f"ag-{data.get('order_value', int(time.time()))}"
        logger.info(
            "ArenaGo order filled: %s %s api_units=%s shares=%s price=%.2f",
            side.value,
            ticker,
            api_quantity,
            actual_shares,
            filled_price,
        )
        self.get_positions()
        return Order(order_id, side, ticker, actual_shares, api_quantity, filled_price, OrderStatus.FILLED)

    def close_position(self, ticker: str) -> Order | None:
        pos = self.get_positions().get(ticker.upper())
        if pos is None or pos.quantity == 0:
            return None
        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        return self.submit_market_order(ticker, side, abs(pos.quantity))

    def get_positions(self, portfolio: str | None = None) -> dict[str, Position]:
        bot_name = portfolio or self.bot_name
        try:
            resp = self._session.get(f"{BASE_URL}/positions/{bot_name}", timeout=30)
            data = resp.json()
        except Exception as e:
            logger.warning("Could not fetch positions for %s: %s", bot_name, e)
            return self._positions_cache if bot_name == self.bot_name else {}

        if resp.status_code != 200:
            logger.warning("Positions API returned %s for %s", resp.status_code, bot_name)
            return self._positions_cache if bot_name == self.bot_name else {}

        items = data if isinstance(data, list) else data.get("positions", []) if isinstance(data, dict) else []
        result: dict[str, Position] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("secid", "")).upper()
            if not ticker:
                continue
            api_qty = int(item.get("position", item.get("quantity", 0)))
            if api_qty == 0:
                continue
            avg_price = float(item.get("average_price", item.get("avg_price", 0)))
            qty_shares = self._api_to_shares(ticker, api_qty)
            price = self._prices.get(ticker, avg_price)
            pnl = (price - avg_price) * qty_shares
            result[ticker] = Position(ticker, qty_shares, api_qty, avg_price, pnl)

        if bot_name == self.bot_name:
            self._positions_cache = result
        return result

    def get_bots(self) -> list[BotInfo]:
        try:
            resp = self._session.get(f"{BASE_URL}/bots", timeout=30)
            data = resp.json()
        except Exception as e:
            logger.warning("Could not fetch ArenaGo bots: %s", e)
            return []

        items = data if isinstance(data, list) else data.get("bots", []) if isinstance(data, dict) else []
        bots = []
        for item in items:
            if not isinstance(item, dict):
                continue
            bots.append(
                BotInfo(
                    name=str(item.get("name", "")),
                    cash_balance=float(item.get("cash_balance", item.get("cash", 0))),
                )
            )
        return bots

    def get_cash_balance(self, bot_name: str | None = None) -> float:
        name = bot_name or self.bot_name
        for bot in self.get_bots():
            if bot.name == name:
                self._cash_balance = bot.cash_balance
                return bot.cash_balance
        return self._cash_balance

    def summary(self) -> str:
        positions = self.get_positions()
        pos_value = sum(abs(pos.quantity) * self._prices.get(ticker, pos.avg_price) for ticker, pos in positions.items())
        return (
            f"ArenaGo [{self.bot_name}] cash={self.get_cash_balance():,.0f} RUB "
            f"positions={len(positions)} pos_value={pos_value:,.0f} RUB"
        )

