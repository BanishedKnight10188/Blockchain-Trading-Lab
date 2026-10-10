"""HMAC wire encoding and secret wrappers; no transport or key discovery."""

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from pydantic import SecretStr

from agent_platform.domain.common import utc_datetime


def _secret(value: str | SecretStr) -> SecretStr:
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if (
        type(raw) is not str
        or not 1 <= len(raw) <= 256
        or any(not 33 <= ord(character) <= 126 for character in raw)
    ):
        raise ValueError("invalid credential format")
    return SecretStr(raw)


@dataclass(frozen=True)
class HmacCredentials:
    api_key: SecretStr = field(repr=False)
    secret_key: SecretStr = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "api_key", _secret(self.api_key))
        object.__setattr__(self, "secret_key", _secret(self.secret_key))


@dataclass(frozen=True)
class SignedQuery:
    query: str = field(repr=False)


def hmac_signature(payload: str, secret: SecretStr) -> str:
    if type(payload) is not str or not payload.isascii() or len(payload) > 4096:
        raise ValueError("invalid signature payload")
    key = _secret(secret).get_secret_value().encode("ascii")
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def sign_query(params: dict[str, str | int], secret: SecretStr) -> SignedQuery:
    if type(params) is not dict or not 1 <= len(params) <= 32:
        raise ValueError("invalid signature parameters")
    for name, value in params.items():
        if (
            type(name) is not str
            or len(name) > 64
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is None
            or name == "signature"
            or type(value) not in (str, int)
            or (type(value) is str and not 1 <= len(value) <= 256)
            or (type(value) is int and not 0 <= value <= 2**63 - 1)
        ):
            raise ValueError("invalid signature parameters")
    payload = urlencode(sorted(params.items()), quote_via=quote)
    return SignedQuery(payload + "&signature=" + hmac_signature(payload, secret))


def unix_milliseconds(timestamp: datetime) -> int:
    elapsed = utc_datetime(timestamp) - datetime(1970, 1, 1, tzinfo=UTC)
    if elapsed.days < 0:
        raise ValueError("timestamp predates Unix epoch")
    return elapsed.days * 86400000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000
