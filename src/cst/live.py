"""Live Kalshi orders.

Paper mode never reaches this module. After the operator approves a whole-dollar
amount from $1 to $5,000, buys and sells are signed with the key id and private
key file named in .env and sent to Kalshi. The approved amount is a cap. The
desk will not spend more than that cap or the available Kalshi balance.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

from cst.models import Position, Quote

LIVE_MIN_DOLLARS = 1
LIVE_MAX_DOLLARS = 5000
_ORDERS = "/portfolio/events/orders"
_BALANCE = "/portfolio/balance"


class LiveTradingError(Exception):
    """A live order was not sent, or Kalshi refused it. The book is unchanged."""


@dataclass(frozen=True)
class Execution:
    filled: bool
    detail: str
    price: float | None = None
    shares: float = 0.0


def parse_live_amount(value) -> tuple[int | None, str | None]:
    """Whole dollars from $1 to $5,000. Anything else is refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "Enter a whole dollar amount from $1 to $5,000."
    if isinstance(value, float) and not value.is_integer():
        return None, "Enter a whole dollar amount from $1 to $5,000."
    dollars = int(value)
    if dollars < LIVE_MIN_DOLLARS or dollars > LIVE_MAX_DOLLARS:
        return None, "Live trading accepts $1 to $5,000."
    return dollars, None


def load_private_key(settings):
    """The key id and PEM path from .env. The path is on the machine that runs the desk."""
    key_id = str(getattr(settings, "kalshi_api_key_id", "") or "").strip()
    raw_path = str(getattr(settings, "kalshi_private_key_path", "") or "").strip()
    if not key_id or not raw_path:
        raise LiveTradingError(
            "Live trading needs CST_KALSHI_API_KEY_ID and CST_KALSHI_PRIVATE_KEY_PATH in .env."
        )
    file = Path(raw_path).expanduser()
    if not file.is_file():
        raise LiveTradingError(
            "The private key file named by CST_KALSHI_PRIVATE_KEY_PATH is not on this machine."
        )
    try:
        key = serialization.load_pem_private_key(file.read_bytes(), password=None)
    except (ValueError, TypeError, OSError):
        raise LiveTradingError("The Kalshi private key file could not be read.") from None
    return key_id, key


def sign_text(private_key, text: str) -> str:
    """RSA-PSS SHA-256, or Ed25519 when the PEM is an Ed25519 key."""
    message = text.encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode()


def _fixed_count(shares: float) -> str:
    count = Decimal(str(shares)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if count <= 0:
        raise LiveTradingError("The order size is not a tradable number of contracts.")
    return f"{count:.2f}"


def _fixed_price(value: Decimal) -> str:
    price = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if price <= 0 or price >= 1:
        raise LiveTradingError("The price is outside the range Kalshi will take.")
    return f"{price:.4f}"


def _fp(value) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def buy_order(quote: Quote, shares: float) -> tuple[dict, Decimal]:
    """V2 order. Kalshi quotes the YES book: buying NO is selling YES at 1 − price."""
    if quote.side == "yes":
        side = "bid"
        yes_price = Decimal(str(quote.ask))
        contract_price = yes_price
    elif quote.side == "no":
        side = "ask"
        contract_price = Decimal(str(quote.ask))
        yes_price = Decimal("1") - contract_price
    else:
        raise LiveTradingError("The quote side is not yes or no.")
    payload = {
        "ticker": quote.market_id,
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "count": _fixed_count(shares),
        "price": _fixed_price(yes_price),
        "time_in_force": "fill_or_kill",
        "self_trade_prevention_type": "taker_at_cross",
    }
    return payload, contract_price


def sell_order(position: Position, bid: float) -> tuple[dict, Decimal]:
    """Close a live clip. Reduce-only is only accepted with immediate-or-cancel."""
    if position.side == "yes":
        side = "ask"
        yes_price = Decimal(str(bid))
        contract_price = yes_price
    elif position.side == "no":
        side = "bid"
        contract_price = Decimal(str(bid))
        yes_price = Decimal("1") - contract_price
    else:
        raise LiveTradingError("The position side is not yes or no.")
    payload = {
        "ticker": position.market_id,
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "count": _fixed_count(position.shares),
        "price": _fixed_price(yes_price),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "reduce_only": True,
    }
    return payload, contract_price


def _contract_price(book_side: str, average_fill_price: str | None, fallback: Decimal) -> float:
    """The response price is the YES price. Convert it back to the side we hold."""
    if not average_fill_price:
        price = fallback
    else:
        yes = Decimal(str(average_fill_price))
        price = yes if book_side == "yes" else Decimal("1") - yes
    return float(price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def execution_from_response(book_side: str, fallback: Decimal, body: dict) -> Execution:
    payload = body.get("order") if isinstance(body.get("order"), dict) else body
    filled = _fp(payload.get("fill_count"))
    if filled <= 0:
        return Execution(False, "Kalshi did not fill the order.")
    return Execution(
        True,
        "",
        _contract_price(book_side, payload.get("average_fill_price"), fallback),
        filled,
    )


class KalshiTrader:
    def __init__(self, key_id: str, private_key, base_url: str, client: httpx.Client | None = None):
        self.key_id = key_id
        self.private_key = private_key
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=10)

    @classmethod
    def from_settings(cls, settings, client: httpx.Client | None = None) -> KalshiTrader:
        key_id, private_key = load_private_key(settings)
        return cls(key_id, private_key, settings.kalshi_base_url, client=client)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self.base_url + path
        sign_path = urlsplit(url).path.split("?", 1)[0]
        timestamp = str(int(time.time() * 1000))
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sign_text(self.private_key, timestamp + method + sign_path),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = self.client.request(method, url, headers=headers, json=body)
        except httpx.HTTPError:
            raise LiveTradingError("Kalshi could not be reached.") from None
        if response.status_code == 401:
            raise LiveTradingError("Kalshi rejected the API key.")
        if response.status_code >= 400:
            raise LiveTradingError(_refusal(response))
        try:
            payload = response.json()
        except ValueError:
            raise LiveTradingError("Kalshi returned a response that was not JSON.") from None
        if not isinstance(payload, dict):
            raise LiveTradingError("Kalshi returned a response that was not JSON.")
        return payload

    def available_dollars(self) -> float:
        payload = self._request("GET", _BALANCE)
        if payload.get("balance_dollars") not in (None, ""):
            return float(payload["balance_dollars"])
        return float(payload.get("balance") or 0) / 100.0

    def buy(self, quote: Quote, shares: float, cost: float) -> Execution:
        available = self.available_dollars()
        if cost > available + 1e-9:
            return Execution(False, "The Kalshi balance is short of this order.")
        payload, contract_price = buy_order(quote, shares)
        body = self._request("POST", _ORDERS, payload)
        return execution_from_response(quote.side, contract_price, body)

    def sell(self, position: Position, bid: float) -> Execution:
        payload, contract_price = sell_order(position, bid)
        body = self._request("POST", _ORDERS, payload)
        return execution_from_response(position.side, contract_price, body)


def _refusal(response: httpx.Response) -> str:
    message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("message") or payload.get("error") or ""
        if isinstance(raw, dict):
            raw = raw.get("message") or ""
        message = " ".join(str(raw).split())[:180]
    if message:
        return f"Kalshi refused the order ({response.status_code}). {message}"
    return f"Kalshi refused the order ({response.status_code})."
