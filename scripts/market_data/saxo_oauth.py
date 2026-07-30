#!/usr/bin/env python3
"""OAuth helpers for Saxo OpenAPI.

Secrets are read only from environment variables or a local token file that is
excluded from Git. Refresh tokens rotate on every successful refresh, so the
new token bundle is written atomically to persistent local storage.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import ProviderConfigurationError, ProviderError


DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
DEFAULT_TOKEN_FILE = ".runtime/saxo-token.json"


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: int
    refresh_token_expires_at: int | None

    @classmethod
    def from_response(cls, payload: dict[str, Any], *, now: int | None = None) -> "TokenBundle":
        current = int(time.time()) if now is None else int(now)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError("Saxo OAuth response has no access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise ProviderError("Saxo OAuth response has invalid expires_in")
        refresh_token = payload.get("refresh_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise ProviderError("Saxo OAuth response has invalid refresh_token")
        refresh_expires_in = payload.get("refresh_token_expires_in")
        refresh_expires_at = None
        if isinstance(refresh_expires_in, (int, float)) and refresh_expires_in > 0:
            refresh_expires_at = current + int(refresh_expires_in)
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_at=current + int(expires_in),
            refresh_token_expires_at=refresh_expires_at,
        )

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "TokenBundle":
        try:
            return cls(
                access_token=str(document["accessToken"]),
                refresh_token=(
                    str(document["refreshToken"])
                    if document.get("refreshToken")
                    else None
                ),
                token_type=str(document.get("tokenType") or "Bearer"),
                expires_at=int(document["expiresAt"]),
                refresh_token_expires_at=(
                    int(document["refreshTokenExpiresAt"])
                    if document.get("refreshTokenExpiresAt") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderConfigurationError("Invalid Saxo token file") from exc

    def as_document(self) -> dict[str, Any]:
        return {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "tokenType": self.token_type,
            "expiresAt": self.expires_at,
            "refreshTokenExpiresAt": self.refresh_token_expires_at,
        }


def auth_base_url(environment: str) -> str:
    normalized = environment.lower()
    if normalized == "sim":
        return "https://sim.logonvalidation.net"
    if normalized == "live":
        return "https://live.logonvalidation.net"
    raise ProviderConfigurationError("SAXO_ENV must be sim or live")


def make_state() -> str:
    return secrets.token_urlsafe(32)


def authorization_url(
    *, app_key: str, redirect_uri: str, environment: str, state: str
) -> str:
    if not app_key:
        raise ProviderConfigurationError("SAXO_APP_KEY is not configured")
    params = urlencode(
        {
            "response_type": "code",
            "client_id": app_key,
            "state": state,
            "redirect_uri": redirect_uri,
        }
    )
    return f"{auth_base_url(environment)}/authorize?{params}"


def _post_form(
    url: str,
    form: dict[str, str],
    *,
    app_key: str,
    app_secret: str,
    timeout: float = 25.0,
) -> dict[str, Any]:
    credentials = base64.b64encode(f"{app_key}:{app_secret}".encode("utf-8")).decode("ascii")
    request = Request(
        url,
        data=urlencode(form).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "polsinelli-tracker/1.1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"Saxo OAuth HTTP {exc.code}: {body[:300]}") from exc
    except URLError as exc:
        raise ProviderError(f"Saxo OAuth connection failed: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError("Saxo OAuth returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError("Saxo OAuth returned a non-object response")
    return payload


def exchange_authorization_code(
    *,
    app_key: str,
    app_secret: str,
    code: str,
    redirect_uri: str,
    environment: str,
    requester: Callable[..., dict[str, Any]] = _post_form,
    now: int | None = None,
) -> TokenBundle:
    if not code:
        raise ProviderConfigurationError("Missing Saxo authorization code")
    payload = requester(
        f"{auth_base_url(environment)}/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        app_key=app_key,
        app_secret=app_secret,
    )
    return TokenBundle.from_response(payload, now=now)


def refresh_token_bundle(
    *,
    app_key: str,
    app_secret: str,
    refresh_token: str,
    redirect_uri: str,
    environment: str,
    requester: Callable[..., dict[str, Any]] = _post_form,
    now: int | None = None,
) -> TokenBundle:
    if not refresh_token:
        raise ProviderConfigurationError("Missing Saxo refresh token")
    payload = requester(
        f"{auth_base_url(environment)}/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": redirect_uri,
        },
        app_key=app_key,
        app_secret=app_secret,
    )
    return TokenBundle.from_response(payload, now=now)


def load_token_file(path: Path) -> TokenBundle:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProviderConfigurationError(f"Saxo token file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderConfigurationError(f"Invalid Saxo token JSON: {path}") from exc
    if not isinstance(document, dict):
        raise ProviderConfigurationError("Saxo token file must contain an object")
    return TokenBundle.from_document(document)


def save_token_file(path: Path, bundle: TokenBundle) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(bundle.as_document(), indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def resolve_access_token(
    *,
    environment: str,
    token_file: Path,
    app_key: str | None = None,
    app_secret: str | None = None,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    now: int | None = None,
    refresh_margin_seconds: int = 120,
    refresher: Callable[..., TokenBundle] = refresh_token_bundle,
) -> str:
    direct = os.getenv("SAXO_ACCESS_TOKEN")
    if direct:
        return direct

    current = int(time.time()) if now is None else int(now)
    bundle = load_token_file(token_file)
    if bundle.expires_at > current + refresh_margin_seconds:
        return bundle.access_token

    key = app_key or os.getenv("SAXO_APP_KEY")
    secret = app_secret or os.getenv("SAXO_APP_SECRET")
    if not key or not secret:
        raise ProviderConfigurationError(
            "Saxo token expired and SAXO_APP_KEY/SAXO_APP_SECRET are not configured"
        )
    if not bundle.refresh_token:
        raise ProviderConfigurationError("Saxo token file has no refresh token")
    if (
        bundle.refresh_token_expires_at is not None
        and bundle.refresh_token_expires_at <= current
    ):
        raise ProviderConfigurationError("Saxo refresh token has expired; login again")

    refreshed = refresher(
        app_key=key,
        app_secret=secret,
        refresh_token=bundle.refresh_token,
        redirect_uri=redirect_uri,
        environment=environment,
        now=current,
    )
    save_token_file(token_file, refreshed)
    return refreshed.access_token
