"""Authenticating as the App, and as one of its installations.

A GitHub App has no long-lived token, only a private key:

    private key ──sign RS256 JWT──▶ /app/installations/{id}/access_tokens
                                    ──▶ installation token, valid ~1 hour

The key never travels and a leaked token expires on its own. Tokens are cached
per installation — minting one per call would spend the rate limit on auth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt

from bot import config

# Refresh this many seconds before the stated expiry, so a long call started
# just under the wire does not finish holding a dead token.
_RENEW_MARGIN_SECONDS = 300


def app_jwt() -> str:
    """A short-lived JWT proving we hold the App's private key.

    GitHub rejects an `exp` more than 10 minutes out and is strict about clock
    skew, hence the backdated `iat`.
    """
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": config.APP_ID},
        config.private_key(),
        algorithm="RS256",
    )


@dataclass
class _CachedToken:
    token: str
    expires_at: float


_cache: dict[int, _CachedToken] = {}


async def installation_token(installation_id: int) -> str:
    cached = _cache.get(installation_id)
    if cached and cached.expires_at - _RENEW_MARGIN_SECONDS > time.time():
        return cached.token

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{config.GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    response.raise_for_status()
    body = response.json()

    # The response states an absolute expiry; trusting it rather than assuming
    # an hour means we follow GitHub if it ever changes the lifetime.
    expires_at = _parse_expiry(body["expires_at"])
    _cache[installation_id] = _CachedToken(body["token"], expires_at)
    return body["token"]


def _parse_expiry(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def forget(installation_id: int) -> None:
    """Drop a cached token, so the next call mints a fresh one.

    Called when GitHub answers 401 with a token we believed was valid — which
    happens if the installation's permissions changed under us.
    """
    _cache.pop(installation_id, None)
