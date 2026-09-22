"""A Google access token from the service account this deployment already has.

KMS uses Application Default Credentials through `google-cloud-kms`; reading
invoices needs the same identity to call Vertex AI. Rather than add a
dependency (`google-auth` plus an HTTP transport for it), this does the one
thing needed - the service-account JWT-bearer flow - with `PyJWT` and `httpx`,
which are already here.

The credentials are the JSON file `GOOGLE_APPLICATION_CREDENTIALS` points at
(written at container start from GCP_SERVICE_ACCOUNT_JSON, see the Dockerfile).
Nothing here reads or logs the key beyond signing with it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import jwt

from api.expenses.extraction.model import ExtractionError

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_ASSERTION_LIFETIME_SECONDS = 3600
#: Refreshed this long before it expires, so a request never starts with a token
#: that lapses in flight.
_REFRESH_MARGIN_SECONDS = 120


class ServiceAccountToken:
    def __init__(
        self,
        *,
        credentials_path: str | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = credentials_path
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if (
                self._token is not None
                and self._clock() < self._expires_at - _REFRESH_MARGIN_SECONDS
            ):
                return self._token
            self._token, self._expires_at = await self._fetch()
            return self._token

    async def _fetch(self) -> tuple[str, float]:
        info = self._load()
        now = int(self._clock())
        try:
            assertion = jwt.encode(
                {
                    "iss": info["client_email"],
                    "scope": SCOPE,
                    "aud": info["token_uri"],
                    "iat": now,
                    "exp": now + _ASSERTION_LIFETIME_SECONDS,
                },
                info["private_key"],
                algorithm="RS256",
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ExtractionError("credentials_invalid", "service account key unusable") from exc

        client = self._http or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.post(
                info["token_uri"],
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        except httpx.HTTPError as exc:
            raise ExtractionError("token_unreachable", type(exc).__name__) from exc
        finally:
            if self._http is None:
                await client.aclose()

        if response.status_code != 200:
            raise ExtractionError("token_refused", f"HTTP {response.status_code}")
        try:
            body = response.json()
            return str(body["access_token"]), now + float(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise ExtractionError("token_unreadable") from exc

    def _load(self) -> dict[str, str]:
        path = self._path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not path:
            raise ExtractionError("credentials_missing", "GOOGLE_APPLICATION_CREDENTIALS unset")
        try:
            info = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ExtractionError("credentials_unreadable", "service account file") from exc
        if not isinstance(info, dict):
            raise ExtractionError("credentials_invalid", "service account file")
        return {str(k): str(v) for k, v in info.items()}
