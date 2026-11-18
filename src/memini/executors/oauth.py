"""Refreshing the OAuth tokens of an mcp source at its token endpoint (RFC 6749, section 6).

:func:`token_refresher` is the ``refresher`` the credential store calls
(:func:`memini.credentials.refresh_oauth`, :func:`memini.credentials.fresh_oauth`): it sends the
refresh token to the token endpoint stored with the credential and returns the tokens the provider
issued; the store writes them as the next generation in the transaction that holds the row, so a
refresh token is never spent twice. The SDK's OAuth provider is not used: it keeps tokens in its own
storage and would refresh behind the store's back.

The token endpoint is an endpoint like any other: it must pass the egress policy
(:mod:`memini.executors.egress`): listed in ``mcp_allowed_endpoints``, https, an address that is not
internal unless its network is listed. The client authenticates with HTTP Basic when it has a
secret (``client_secret_basic``, the method every server supports), and sends its id in the form
when it has none (a public client). The answer's tokens are registered as secrets by the store
before anything could log them; no error here holds a token or the body of an answer.
"""

import base64
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import quote

import httpx2
from pydantic import SecretStr

from memini.credentials import OAuthCredential, OAuthTokens, Refresher
from memini.executors.egress import EgressPolicy
from memini.executors.transport import (
    GuardedTransport,
    Resolver,
    network_transport,
    preflight,
    system_resolver,
)

__all__ = ["DEFAULT_TOKEN_TIMEOUT", "RefreshFailed", "token_refresher"]

DEFAULT_TOKEN_TIMEOUT: Final = 30.0
"""Seconds a token endpoint may take to answer."""

_MAX_ANSWER: Final = 64 * 1024


class RefreshFailed(Exception):
    """The token endpoint did not issue tokens. The message says why (an HTTP status, an OAuth
    ``error`` code, a missing field), never a token or the answer's body."""


def _basic(client_id: str, secret: SecretStr) -> str:
    # RFC 6749, 2.3.1: id and secret are form-urlencoded before they are joined.
    pair = f"{quote(client_id, safe='')}:{quote(secret.get_secret_value(), safe='')}"
    return "Basic " + base64.b64encode(pair.encode()).decode("ascii")


def _tokens(answer: Mapping[str, Any], issued_at: datetime) -> OAuthTokens:
    token_type = answer.get("token_type")
    if isinstance(token_type, str) and token_type.lower() != "bearer":
        raise RefreshFailed("the token endpoint issued a token that is not a bearer token")
    access = answer.get("access_token")
    if not isinstance(access, str) or not access:
        raise RefreshFailed("the token endpoint's answer has no access_token")
    refresh = answer.get("refresh_token")
    expires_in = answer.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool) and expires_in > 0:
        expires_at = issued_at + timedelta(seconds=float(expires_in))
    try:
        return OAuthTokens(
            access_token=SecretStr(access),
            refresh_token=SecretStr(refresh) if isinstance(refresh, str) and refresh else None,
            expires_at=expires_at,
        )
    except ValueError as error:  # InvalidCredentialError names the field, never the value
        raise RefreshFailed(
            f"the token endpoint issued a token that cannot be stored: {error}"
        ) from None


def token_refresher(
    policy: EgressPolicy,
    *,
    timeout: float = DEFAULT_TOKEN_TIMEOUT,
    http_transport: httpx2.AsyncBaseTransport | None = None,
    resolver: Resolver = system_resolver,
) -> Refresher:
    """A refresher for :func:`memini.credentials.refresh_oauth`; see the module.

    ``http_transport`` replaces the transport that reaches the network (tests).

    The refresher raises :class:`RefreshFailed` when no tokens were issued, and
    :class:`~memini.executors.egress.EgressRefused` when the token endpoint is not allowed.
    """

    async def refresh(current: OAuthCredential) -> OAuthTokens:
        client = current.client
        endpoint = client.token_endpoint
        refresh_token = current.tokens.refresh_token
        if endpoint is None:
            raise RefreshFailed("the credential names no token endpoint to refresh it at")
        if refresh_token is None:
            raise RefreshFailed("the credential has no refresh token")
        target = policy.check_url(endpoint)
        if http_transport is None:
            await preflight(policy, target.url, resolver)
        inner = http_transport or network_transport(policy, resolver=resolver, max_connections=1)
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()}
        if client.scope:
            form["scope"] = client.scope
        headers = {"Accept": "application/json"}
        if client.client_id is not None and client.client_secret is not None:
            headers["Authorization"] = _basic(client.client_id, client.client_secret)
        elif client.client_id is not None:
            form["client_id"] = client.client_id
        issued_at = datetime.now(UTC)
        async with httpx2.AsyncClient(
            transport=GuardedTransport(inner, policy),
            timeout=httpx2.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        ) as http:
            response = await http.post(target.url, data=form, headers=headers)
            body = await response.aread()
        if response.status_code != 200:
            reason = f"HTTP {response.status_code}"
            try:
                error = cast(Mapping[str, Any], response.json()).get("error")
            except ValueError:
                error = None
            if isinstance(error, str) and error.isascii() and len(error) <= 64:
                reason += f", {error}"  # an RFC 6749 error code, such as invalid_grant
            raise RefreshFailed(f"the token endpoint did not issue tokens ({reason})")
        if len(body) > _MAX_ANSWER:
            raise RefreshFailed("the token endpoint's answer is too large")
        try:
            answer = response.json()
        except ValueError:
            raise RefreshFailed("the token endpoint's answer is not JSON") from None
        if not isinstance(answer, dict):
            raise RefreshFailed("the token endpoint's answer is not a JSON object")
        return _tokens(cast(dict[str, Any], answer), issued_at)

    return refresh
