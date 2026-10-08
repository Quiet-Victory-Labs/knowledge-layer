"""Optional single-organization OIDC boundary for the self-hosted knowledge service.

The browser uses an authorization-code + PKCE session. Agent HTTP/MCP requests
use audience-bound JWT access tokens. Source-level ACL mirroring is a separate
qualification gate; this module grants access to one configured organization.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import jwt
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from knowledge_layer.access import principal_from_claims, principal_scope
from knowledge_layer.auth import ASGIApp
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.request_budget import close_request_headers


class OidcSettings:
    def __init__(self) -> None:
        self.issuer = _required("KNOWLEDGE_OIDC_ISSUER").rstrip("/")
        self.client_id = _required("KNOWLEDGE_OIDC_CLIENT_ID")
        self.client_secret = _required("KNOWLEDGE_OIDC_CLIENT_SECRET")
        self.audience = _required("KNOWLEDGE_OIDC_AUDIENCE")
        self.jwks_uri = _required("KNOWLEDGE_OIDC_JWKS_URI")
        self.session_key = _required("KNOWLEDGE_OIDC_SESSION_KEY")
        self.public_url = _required("KNOWLEDGE_PUBLIC_URL").rstrip("/")
        self.allowed_group = _required("KNOWLEDGE_OIDC_ALLOWED_GROUP")
        self.steward_group = _required("KNOWLEDGE_OIDC_STEWARD_GROUP")
        self.group_claim = os.getenv("KNOWLEDGE_OIDC_GROUP_CLAIM", "groups")
        self.agent_scope = os.getenv("KNOWLEDGE_OIDC_AGENT_SCOPE", "knowledge.agent")
        self.login_scopes = os.getenv("KNOWLEDGE_OIDC_LOGIN_SCOPES", "openid profile email").strip()
        scope_names = self.login_scopes.split()
        if (not 1 <= len(scope_names) <= 12 or "openid" not in scope_names
                or len(scope_names) != len(set(scope_names))
                or any(not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", name) for name in scope_names)):
            raise ValueError("KNOWLEDGE_OIDC_LOGIN_SCOPES must include openid and use unique safe scopes")
        try:
            self.browser_session_seconds = int(os.getenv("KNOWLEDGE_OIDC_BROWSER_SESSION_SECONDS", "900"))
        except ValueError as exc:
            raise ValueError("KNOWLEDGE_OIDC_BROWSER_SESSION_SECONDS must be 60–3600") from exc
        if not 60 <= self.browser_session_seconds <= 3600:
            raise ValueError("KNOWLEDGE_OIDC_BROWSER_SESSION_SECONDS must be 60–3600")
        for label, value in (("issuer", self.issuer), ("jwks_uri", self.jwks_uri), ("public_url", self.public_url)):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError(f"OIDC {label} must be an HTTPS URL without embedded credentials")
        if len(self.session_key) < 32:
            raise ValueError("KNOWLEDGE_OIDC_SESSION_KEY must contain at least 32 characters")
        if self.allowed_group == self.steward_group:
            raise ValueError("The steward group must differ from the general access group")
        if urlparse(self.public_url).path not in ("", "/"):
            raise ValueError("KNOWLEDGE_PUBLIC_URL must be an origin without a path")


def _required(name: str) -> str:
    value = (operator_secret(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required when KNOWLEDGE_AUTH_MODE=oidc")
    return value


def _groups(value: Any) -> set[str]:
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    if isinstance(value, str):
        return set(value.split())
    return set()


def browser_identity(request: Request, settings: OidcSettings, *, steward: bool = False) -> dict[str, Any]:
    identity = request.session.get("knowledge_identity")
    if not isinstance(identity, dict) or identity.get("iss") != settings.issuer or not identity.get("sub"):
        raise HTTPException(status_code=401, detail="Sign in is required")
    if not isinstance(identity.get("expires_at"), (int, float)) or identity["expires_at"] <= time.time():
        request.session.clear()
        raise HTTPException(status_code=401, detail="The sign-in session expired")
    groups = _groups(identity.get("groups"))
    if settings.allowed_group not in groups or (steward and settings.steward_group not in groups):
        raise HTTPException(status_code=403, detail="The required organization role is missing")
    return identity


def require_browser(request: Request, settings: OidcSettings) -> dict[str, Any]:
    endpoint = request.scope.get("endpoint")
    identity = browser_identity(request, settings)
    request.scope["knowledge_actor"] = identity["sub"]
    if getattr(endpoint, "__name__", None) in {
        "create_connection", "update_connection", "refresh_connection",
        "cancel_refresh_job", "decide_proposal", "archive_proposals",
        "publish_source", "revoke_publication",
        "review_inventory_concept", "publish_inventory_concept", "withdraw_inventory_concept",
    } and settings.steward_group not in _groups(identity.get("groups")):
        raise HTTPException(status_code=403, detail="The required organization role is missing")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected = request.session.get("knowledge_csrf", "")
        supplied = request.headers.get("x-knowledge-csrf", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="A valid CSRF token is required")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != settings.public_url:
            raise HTTPException(status_code=403, detail="The request origin is not allowed")
    return identity


class BrowserStaticBoundary:
    def __init__(self, app: ASGIApp, settings: OidcSettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            browser_identity(Request(scope, receive), self.settings)
        except HTTPException:
            await RedirectResponse("/auth/login", status_code=303)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def oidc_router(settings: OidcSettings) -> APIRouter:
    oauth = OAuth()
    oauth.register(
        name="company", client_id=settings.client_id, client_secret=settings.client_secret,
        server_metadata_url=f"{settings.issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": settings.login_scopes, "code_challenge_method": "S256"},
    )
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.get("/login")
    async def login(request: Request):
        request.session.clear()
        return await oauth.company.authorize_redirect(request, f"{settings.public_url}/auth/callback")

    @router.get("/callback")
    async def callback(request: Request):
        try:
            token = await oauth.company.authorize_access_token(request)
        except Exception as exc:
            request.session.clear()
            raise HTTPException(status_code=401, detail="OIDC sign-in failed") from exc
        claims = token.get("userinfo")
        if not isinstance(claims, dict) or claims.get("iss") != settings.issuer or not claims.get("sub"):
            request.session.clear()
            raise HTTPException(status_code=401, detail="The identity token is invalid")
        audience = claims.get("aud")
        if settings.client_id not in (audience if isinstance(audience, list) else [audience]):
            request.session.clear()
            raise HTTPException(status_code=401, detail="The identity token audience is invalid")
        groups = _groups(claims.get(settings.group_claim))
        if settings.allowed_group not in groups:
            request.session.clear()
            raise HTTPException(status_code=403, detail="The required organization role is missing")
        try:
            identity_expires_at = float(claims["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            request.session.clear()
            raise HTTPException(status_code=401, detail="The identity token expiry is invalid") from exc
        if identity_expires_at <= time.time():
            request.session.clear()
            raise HTTPException(status_code=401, detail="The identity token expired")
        request.session.clear()
        request.session["knowledge_identity"] = {
            "sub": claims["sub"], "iss": settings.issuer,
            "groups": sorted(groups),
            "expires_at": min(time.time() + settings.browser_session_seconds, identity_expires_at),
        }
        request.session["knowledge_csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=303)

    @router.get("/me")
    def me(request: Request) -> dict[str, Any]:
        identity = browser_identity(request, settings)
        return {"subject": identity["sub"], "groups": identity["groups"],
                "steward": settings.steward_group in identity["groups"],
                "csrf_token": request.session["knowledge_csrf"]}

    @router.post("/logout")
    def logout(request: Request):
        require_browser(request, settings)
        request.session.clear()
        return {"signed_out": True}

    return router


class OidcAgentBoundary:
    """Accept only signed, audience-bound access JWTs with the agent scope."""

    def __init__(self, app: ASGIApp, settings: OidcSettings):
        self.app = app
        self.settings = settings
        self.jwks = jwt.PyJWKClient(settings.jwks_uri, cache_jwk_set=True, lifespan=300, timeout=3)
        self._verify_slots = asyncio.Semaphore(4)
        self._jwks_failure_until = 0.0

    def verify(self, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in {"RS256", "ES256"}:
            raise jwt.InvalidAlgorithmError("Unsupported agent JWT algorithm")
        key = self.jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256", "ES256"],
            audience=self.settings.audience, issuer=self.settings.issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
        scopes = _groups(claims.get("scope")) | _groups(claims.get("scp"))
        if self.settings.agent_scope not in scopes:
            raise jwt.InvalidTokenError("Agent scope is missing")
        return claims

    async def verify_async(self, token: str) -> dict[str, Any]:
        # PyJWKClient can fetch keys over the network. Keep that work off the
        # ASGI event loop and avoid repeated fetches during an IdP outage.
        async with self._verify_slots:
            if time.monotonic() < self._jwks_failure_until:
                raise jwt.PyJWKClientConnectionError("OIDC signing keys are temporarily unavailable")
            try:
                return await asyncio.to_thread(self.verify, token)
            except jwt.PyJWKClientConnectionError:
                self._jwks_failure_until = time.monotonic() + 5
                raise

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = next((value for key, value in scope.get("headers", []) if key.lower() == b"authorization"), b"")
        token = supplied.removeprefix(b"Bearer ").decode("utf-8", "ignore") if supplied.startswith(b"Bearer ") else ""
        try:
            claims = await self.verify_async(token) if token else None
        except (jwt.PyJWTError, ValueError, OSError):
            claims = None
        if claims is None:
            await JSONResponse(
                {"detail": "A valid scoped OIDC access token is required"}, status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{self.settings.public_url}/.well-known/oauth-protected-resource"',
                         **close_request_headers(scope)},
            )(scope, receive, send)
            return
        scope["knowledge_identity"] = claims
        try:
            principal = principal_from_claims(claims, group_claim=self.settings.group_claim)
        except ValueError:
            await JSONResponse({"detail": "The access token has an invalid principal"}, status_code=401,
                               headers=close_request_headers(scope))(scope, receive, send)
            return
        scope["knowledge_actor"] = principal.subject
        with principal_scope(principal):
            await self.app(scope, receive, send)
