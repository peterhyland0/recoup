"""Typed clients for the clearinghouse, the payer portal and the practice management system."""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections.abc import Callable
from typing import Any

from recoup.integrations.http import ResilientClient, UpstreamError


class ClearinghouseClient:
    """Pulls 835 ERA files. Rate-limited and flaky, like the real ones."""

    def __init__(self, http: ResilientClient, account: str):
        self.http = http
        self.account = account

    def list_eras(self, since: str | None = None) -> list[dict[str, Any]]:
        params = {"since": since} if since else {}
        response = self.http.request("GET", f"/clearinghouse/{self.account}/eras", params=params)
        eras: list[dict[str, Any]] = response.json()["eras"]
        return eras

    def download(self, era_id: str) -> str:
        return self.http.request("GET", f"/clearinghouse/{self.account}/eras/{era_id}").text


class PayerPortalClient:
    """Files appeals. Every submission carries an idempotency key; retries are safe."""

    def __init__(self, http: ResilientClient):
        self.http = http

    def submit_appeal(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        response = self.http.request("POST", "/payer/appeals", json=payload, idempotency_key=idempotency_key)
        result: dict[str, Any] = response.json()
        return result


class PMSClient:
    """Writes back to the practice management system. OAuth2 client credentials, token cached."""

    def __init__(self, http: ResilientClient, client_id: str, client_secret: str,
                 clock: Callable[[], float] = time.monotonic):  # fmt: skip
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.clock = clock
        self._token: str | None = None
        self._expires = 0.0

    def _bearer(self, refresh: bool = False) -> str:
        if refresh or self._token is None or self.clock() >= self._expires - 30:
            response = self.http.request("POST", "/pms/oauth/token", data={
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret})  # fmt: skip
            body = response.json()
            self._token = body["access_token"]
            self._expires = self.clock() + float(body["expires_in"])
        assert self._token is not None
        return self._token

    def add_claim_note(self, claim_id: str, text: str, idempotency_key: str | None = None) -> dict[str, Any]:
        for refresh in (False, True):
            try:
                response = self.http.request("POST", f"/pms/claims/{claim_id}/notes", json={"text": text},
                                             idempotency_key=idempotency_key,
                                             headers={"Authorization": f"Bearer {self._bearer(refresh)}"})  # fmt: skip
            except UpstreamError as error:
                if error.status == 401 and not refresh:
                    continue  # token revoked early: fetch a new one once
                raise
            result: dict[str, Any] = response.json()
            return result
        raise UpstreamError("PMS rejected a freshly issued token", 401)


def sign(secret: str, body: bytes, timestamp: str) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, timestamp: str, signature: str, *, tolerance: int = 300,
                     clock: Callable[[], float] = time.time) -> bool:  # fmt: skip
    """Constant-time check, with a freshness window so a captured webhook cannot be replayed later."""
    try:
        sent = float(timestamp)
    except ValueError:
        return False
    if not math.isfinite(sent) or abs(clock() - sent) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, timestamp), signature)
