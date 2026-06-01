"""ArenaGo Broker — API client for submitting orders and checking positions.

API docs: https://arenago.ru
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://arenago.ru/api"


@dataclass
class Position:
    quantity: int
    avg_price: float


@dataclass
class BotInfo:
    name: str
    cash_balance: float


class ArenaGoBroker:
    """Wrapper around the ArenaGo REST API."""

    def __init__(self, token: str, bot_name: str = "btc"):
        self.token = token.strip()
        self.bot_name = bot_name
        if not self.token:
            raise ValueError("ArenaGo token is required")

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": self.token,
        }

    # ── Orders ──────────────────────────────────────────────────────────────

    def submit_order(
        self,
        direction: str,
        secid: str,
        quantity: int,
        bot: str | None = None,
    ) -> dict:
        """Submit a market order.

        Args:
            direction: 'B' for buy, 'S' for sell.
            secid: Ticker symbol (e.g. 'BTC', 'SBER').
            quantity: Number of shares/contracts (integer).
            bot: Portfolio/bot name. Defaults to self.bot_name.

        Returns:
            API response dict with keys: success, order_value, price, quantity, remaining_cash.
        """
        payload = {
            "direction": direction,
            "secid": secid,
            "quantity": quantity,
            "bot": bot or self.bot_name,
        }
        resp = requests.post(
            f"{BASE_URL}/submit_order",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        data = self._json_response(resp, "submit_order")
        if not isinstance(data, dict):
            raise RuntimeError(f"ArenaGo submit_order returned unexpected response: {data!r}")

        if "error" in data:
            logger.error(f"Order error: {data['error']}")
            raise RuntimeError(data["error"])

        if data.get("success") is not True:
            raise RuntimeError(f"Order was not accepted: {data!r}")

        logger.info(
            f"Order OK: {direction} {quantity} x {secid} "
            f"@ {data.get('price', '?')} "
            f"value={data.get('order_value', '?')} "
            f"cash_left={data.get('remaining_cash', '?')}"
        )
        return data

    def buy(self, secid: str, quantity: int, bot: str | None = None) -> dict:
        return self.submit_order("B", secid, quantity, bot)

    def sell(self, secid: str, quantity: int, bot: str | None = None) -> dict:
        return self.submit_order("S", secid, quantity, bot)

    # ── Positions & Trades ──────────────────────────────────────────────────

    def get_positions(self, portfolio: str | None = None) -> dict[str, Position]:
        """Return open positions as {ticker: Position}.

        Handles various API response formats:
          - list of dicts: [{"secid": "BTC", "position": 1, "average_price": 63130.0}, ...]
          - dict with list: {"positions": [...]}
          - string / empty / error: returns {}
        """
        portf = portfolio or self.bot_name
        resp = requests.get(
            f"{BASE_URL}/positions/{portf}",
            headers=self._headers(),
            timeout=30,
        )
        data = self._json_response(resp, "positions")
        logger.debug(f"Positions API raw response ({portf}): {data!r:.500s}")

        # Normalize: extract list of position dicts
        items: list[dict] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Could be {"positions": [...]} or {"error": "..."}
            if "error" in data:
                logger.warning(f"Positions API error: {data['error']}")
                return {}
            # Try common wrapper keys
            for key in ("positions", "data", "result"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
            if not items:
                # Single position as a dict?
                if "secid" in data:
                    items = [data]
        else:
            logger.warning(f"Unexpected positions response type: {type(data).__name__}")
            return {}

        positions: dict[str, Position] = {}
        for pos in items:
            if not isinstance(pos, dict):
                logger.debug(f"Skipping non-dict position item: {pos!r}")
                continue
            secid = pos.get("secid", "")
            if not secid:
                continue
            positions[secid] = Position(
                quantity=int(pos.get("position", pos.get("quantity", 0))),
                avg_price=float(pos.get("average_price", pos.get("avg_price", 0))),
            )
        return positions

    def get_trades(self, portfolio: str | None = None) -> list[dict]:
        portf = portfolio or self.bot_name
        resp = requests.get(
            f"{BASE_URL}/trades/{portf}",
            headers=self._headers(),
            timeout=30,
        )
        data = self._json_response(resp, "trades")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "error" in data:
            logger.warning(f"Trades API error: {data['error']}")
            return []
        return []

    # ── Bots & Balance ──────────────────────────────────────────────────────

    def get_bots(self) -> list[BotInfo]:
        resp = requests.get(
            f"{BASE_URL}/bots",
            headers=self._headers(),
            timeout=30,
        )
        data = self._json_response(resp, "bots")
        logger.debug(f"Bots API raw response: {data!r:.500s}")

        # Normalize: extract list of bot dicts
        items: list[dict] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if "error" in data:
                logger.warning(f"Bots API error: {data['error']}")
                return []
            for key in ("bots", "data", "result"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
            if not items and "name" in data:
                items = [data]

        bots = []
        for b in items:
            if not isinstance(b, dict):
                continue
            bots.append(BotInfo(
                name=str(b.get("name", "")),
                cash_balance=float(b.get("cash_balance", b.get("cash", 0))),
            ))
        return bots

    def get_cash_balance(self, bot_name: str | None = None) -> float:
        """Get cash balance for a specific bot."""
        name = bot_name or self.bot_name
        bots = self.get_bots()
        for b in bots:
            if b.name == name:
                return b.cash_balance
        raise ValueError(f"Bot '{name}' not found")

    def summary(self) -> str:
        bots = self.get_bots()
        lines = ["ArenaGo Portfolio:"]
        for b in bots:
            lines.append(f"  Bot '{b.name}': cash = {b.cash_balance:,.2f} RUB")
        return "\n".join(lines)

    @staticmethod
    def _json_response(resp: requests.Response, endpoint: str) -> object:
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"ArenaGo {endpoint} HTTP error: {e}") from e

        try:
            return resp.json()
        except ValueError as e:
            body = resp.text[:300]
            raise RuntimeError(f"ArenaGo {endpoint} returned non-JSON response: {body!r}") from e
