"""Direct Cloud Run IAP assertions, with independent local identity and CSRF policy.

Only Google's signed assertion supplies identity. Proxy convenience headers,
browser cookies and bearer tokens cannot establish authentication in this mode.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.auth import ASGIApp
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.request_budget import close_request_headers

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_JWKS_URI = "https://www.gstatic.com/iap/verify/public_key-jwk"
MAX_ASSERTION_BYTES = 16 * 1024
CLOCK_SKEW_SECONDS = 30
MAX_ASSERTION_LIFETIME = 600 + 2 * CLOCK_SKEW_SECONDS
_AUDIENCE = re.compile(r"/projects/[1-9][0-9]{0,23}/locations/[a-z][a-z0-9-]{1,62}/services/[a-z][a-z0-9-]{0,62}")
_IDENTITY_FIELDS = frozenset({"allowed_subjects", "allowed_emails", "steward_subjects",
                             "steward_emails", "agent_subjects", "agent_emails"})
_STEWARD_ENDPOINTS = frozenset({
    "create_connection", "update_connection", "refresh_connection", "cancel_refresh_job",
    "decide_proposal", "archive_proposals", "publish_source", "revoke_publication",
    "review_inventory_concept", "publish_inventory_concept", "withdraw_inventory_concept",
})


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 255 or value != value.strip()
            or re.search(r"[\x00-\x1f\x7f]", value)):
        raise ValueError(f"IAP {label} must be bounded printable text")
    return value


def _required(name: str) -> str:
    value = operator_secret(name)
    if not value:
        raise ValueError(f"{name} is required when KNOWLEDGE_AUTH_MODE=iap")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


@dataclass(frozen=True)
class IapIdentityPolicy:
    audiences: dict[str, frozenset[str]]

    @classmethod
    def from_environment(cls) -> IapIdentityPolicy:
        raw = _required("KNOWLEDGE_IAP_IDENTITIES")
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise ValueError("IAP identity policy exceeds 64 KiB")
        try:
            payload = json.loads(raw, object_pairs_hook=_unique_object)
        except (TypeError, ValueError):
            raise ValueError("IAP identity policy must be valid JSON") from None
        if (not isinstance(payload, dict) or set(payload) != _IDENTITY_FIELDS | {"version"}
                or type(payload["version"]) is not int or payload["version"] != 1):
            raise ValueError("IAP identity policy requires version 1 and explicit identity audiences")
        audiences = {}
        total = 0
        for name in _IDENTITY_FIELDS:
            values = payload[name]
            if not isinstance(values, list) or len(values) > 256:
                raise ValueError("IAP identity audiences must be bounded lists")
            members = [_text(value, "identity selector") for value in values]
            if name.endswith("_emails"):
                if any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) for value in members):
                    raise ValueError("IAP email selectors must be explicit email addresses")
                members = [value.casefold() for value in members]
            if len(set(members)) != len(members):
                raise ValueError("IAP identity audiences cannot repeat selectors")
            total += len(members)
            audiences[name] = frozenset(members)
        if total > 512 or not (audiences["allowed_subjects"] or audiences["allowed_emails"]):
            raise ValueError("IAP requires an explicit browser audience within its identity budget")
        return cls(audiences)

    def permits(self, role: str, claims: dict[str, Any]) -> bool:
        email = claims.get("email")
        return (claims["sub"] in self.audiences[role + "_subjects"]
                or isinstance(email, str) and email.casefold() in self.audiences[role + "_emails"])


class IapSettings:
    def __init__(self) -> None:
        self.audience = _required("KNOWLEDGE_IAP_AUDIENCE")
        if not _AUDIENCE.fullmatch(self.audience):
            raise ValueError("KNOWLEDGE_IAP_AUDIENCE must identify one direct Cloud Run service")
        self.session_key = _required("KNOWLEDGE_IAP_SESSION_KEY")
        if len(self.session_key) < 32:
            raise ValueError("KNOWLEDGE_IAP_SESSION_KEY must contain at least 32 characters")
        self.allowed_group = _text(_required("KNOWLEDGE_OIDC_ALLOWED_GROUP"), "general group")
        self.steward_group = _text(_required("KNOWLEDGE_OIDC_STEWARD_GROUP"), "steward group")
        if self.allowed_group == self.steward_group:
            raise ValueError("The steward group must differ from the general access group")
        self.agent_scope = os.getenv("KNOWLEDGE_OIDC_AGENT_SCOPE", "knowledge.agent")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", self.agent_scope):
            raise ValueError("The IAP agent scope must be a bounded scope name")
        self.public_url = (operator_secret("KNOWLEDGE_PUBLIC_URL") or "").rstrip("/")
        if self.public_url:
            public = urlparse(self.public_url)
            if (public.scheme != "https" or not public.hostname or public.username or public.password
                    or public.path or public.query or public.fragment):
                raise ValueError("KNOWLEDGE_PUBLIC_URL must be an HTTPS origin")
        try:
            self.browser_session_seconds = int(os.getenv("KNOWLEDGE_IAP_BROWSER_SESSION_SECONDS", "900"))
        except ValueError:
            raise ValueError("IAP browser session seconds must be 60-3600") from None
        if not 60 <= self.browser_session_seconds <= 3600:
            raise ValueError("IAP browser session seconds must be 60-3600")
        IapIdentityPolicy.from_environment()


class IapVerifier:
    """Bounded fixed-origin JWKS cache; assertions are verified on every request."""

    def __init__(self, settings: IapSettings):
        self.settings = settings
        self._keys: dict[str, Any] = {}
        self._keys_until = 0.0
        self._refresh_after = 0.0
        self._keys_lock = Lock()
        self._verify_slots = asyncio.Semaphore(4)

    def _key(self, kid: str):
        if not self._keys_lock.acquire(timeout=3):
            raise jwt.InvalidTokenError("IAP signing key lookup is busy")
        try:
            now = time.monotonic()
            if now < self._keys_until and kid in self._keys:
                return self._keys[kid]
            if now < self._refresh_after:
                raise jwt.InvalidTokenError("IAP signing keys are temporarily unavailable")
            self._refresh_after = now + 5
            try:
                fetch_deadline = time.monotonic() + 3
                with (httpx.Client(timeout=3, follow_redirects=False, trust_env=False) as client,
                      client.stream("GET", IAP_JWKS_URI) as response):
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= fetch_deadline:
                            raise ValueError
                        raw.extend(chunk)
                        if len(raw) > 64 * 1024:
                            raise ValueError
                if time.monotonic() >= fetch_deadline:
                    raise ValueError
                payload = json.loads(raw, object_pairs_hook=_unique_object)
                if (not isinstance(payload, dict) or set(payload) != {"keys"}
                        or not isinstance(payload["keys"], list) or not 1 <= len(payload["keys"]) <= 32):
                    raise ValueError
                keys = {}
                for value in payload["keys"]:
                    if (not isinstance(value, dict) or value.get("kty") != "EC"
                            or value.get("crv") != "P-256" or value.get("alg", "ES256") != "ES256"
                            or value.get("use", "sig") != "sig"):
                        raise ValueError
                    identifier = _text(value.get("kid"), "key identifier")
                    if identifier in keys:
                        raise ValueError
                    keys[identifier] = jwt.PyJWK(value, algorithm="ES256").key
                self._keys = keys
                self._keys_until = time.monotonic() + 300
            except (httpx.HTTPError, jwt.PyJWTError, TypeError, ValueError, UnicodeError):
                raise jwt.InvalidTokenError("IAP signing keys are unavailable") from None
            if kid not in self._keys:
                raise jwt.InvalidTokenError("Unknown IAP signing key")
            return self._keys[kid]

        finally:
            self._keys_lock.release()

    def verify(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not 1 <= len(token) <= MAX_ASSERTION_BYTES:
            raise jwt.InvalidTokenError("IAP assertion is unavailable")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "ES256":
            raise jwt.InvalidAlgorithmError("IAP requires ES256")
        kid = _text(header.get("kid"), "key identifier")
        claims = jwt.decode(token, self._key(kid), algorithms=["ES256"],
                            audience=self.settings.audience, issuer=IAP_ISSUER,
                            leeway=CLOCK_SKEW_SECONDS,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"], "strict_aud": True})
        if (type(claims["iat"]) is not int or type(claims["exp"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= MAX_ASSERTION_LIFETIME):
            raise jwt.InvalidTokenError("IAP assertion lifetime is invalid")
        _text(claims["sub"], "subject")
        if "email" in claims:
            _text(claims["email"], "signed email")
        return claims

    async def verify_async(self, token: str) -> dict[str, Any]:
        async with asyncio.timeout(8), self._verify_slots:
            return await asyncio.to_thread(self.verify, token)


def _header(scope: dict[str, Any], name: bytes, *, maximum: int, required: bool = True) -> str:
    values = [value for key, value in scope.get("headers", []) if key.lower() == name]
    if not values and not required:
        return ""
    if len(values) != 1 or not 1 <= len(values[0]) <= maximum:
        raise HTTPException(status_code=401, detail="The required request header is invalid")
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(status_code=401, detail="The required request header is invalid") from None
    if value != value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
        raise HTTPException(status_code=401, detail="The required request header is invalid")
    return value


class IapBoundary:
    def __init__(self, app: ASGIApp, settings: IapSettings):
        self.app, self.settings = app, settings
        self.verifier = IapVerifier(settings)

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path = scope.get("path", "")
        if path in {"/api/health", "/api/ready"} and scope.get("method") in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return
        try:
            token = _header(scope, b"x-goog-iap-jwt-assertion", maximum=MAX_ASSERTION_BYTES)
            # Real IAP can include these convenience headers, but they never
            # supply identity or roles. Refuse ambiguous or oversized variants.
            for name in (b"x-goog-authenticated-user-email", b"x-goog-authenticated-user-id"):
                _header(scope, name, maximum=1024, required=False)
            claims = await self.verifier.verify_async(token)
            policy = IapIdentityPolicy.from_environment()
            agent = path == "/agent" or path.startswith("/agent/")
            if not policy.permits("agent" if agent else "allowed", claims):
                raise HTTPException(status_code=403, detail="This verified identity is not allowed on this surface")
            groups = {self.settings.allowed_group}
            if policy.permits("steward", claims):
                groups.add(self.settings.steward_group)
            principal = Principal(claims["sub"], frozenset(groups),
                                  frozenset({self.settings.agent_scope}) if agent else frozenset())
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                               headers=close_request_headers(scope))(scope, receive, send)
            return
        except (jwt.PyJWTError, TypeError, ValueError, OSError):
            await JSONResponse({"detail": "A valid Google IAP signed assertion and current identity policy are required"},
                               status_code=401, headers=close_request_headers(scope))(scope, receive, send)
            return
        scope["knowledge_iap_identity"] = {"sub": principal.subject, "groups": sorted(groups)}
        scope["knowledge_actor"] = principal.subject
        with principal_scope(principal):
            await self.app(scope, receive, send)


def require_iap_browser(request: Request, settings: IapSettings) -> dict[str, Any]:
    identity = request.scope.get("knowledge_iap_identity")
    if not isinstance(identity, dict):
        raise HTTPException(status_code=401, detail="A verified IAP identity is required")
    endpoint = request.scope.get("endpoint")
    if (getattr(endpoint, "__name__", None) in _STEWARD_ENDPOINTS
            and settings.steward_group not in identity["groups"]):
        raise HTTPException(status_code=403, detail="The required organization role is missing")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not settings.public_url:
            raise HTTPException(status_code=503, detail="The public request origin is not configured")
        try:
            supplied = _header(request.scope, b"x-knowledge-csrf", maximum=256)
            origin = _header(request.scope, b"origin", maximum=2048)
        except HTTPException:
            raise HTTPException(status_code=403, detail="A valid CSRF token and request origin are required") from None
        session = request.session
        expected = session.get("knowledge_csrf")
        if (session.get("knowledge_iap_subject") != identity["sub"]
                or type(session.get("knowledge_iap_expires")) not in {int, float}
                or session["knowledge_iap_expires"] <= time.time()
                or not isinstance(expected, str) or not expected or not hmac.compare_digest(supplied, expected)
                or origin != settings.public_url):
            raise HTTPException(status_code=403, detail="A valid identity-bound CSRF token and request origin are required")
    return identity


def iap_router(settings: IapSettings) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.get("/login")
    def login():
        return RedirectResponse("/", status_code=303)

    @router.get("/me")
    def me(request: Request):
        identity = require_iap_browser(request, settings)
        if (request.session.get("knowledge_iap_subject") != identity["sub"]
                or not isinstance(request.session.get("knowledge_csrf"), str)
                or type(request.session.get("knowledge_iap_expires")) not in {int, float}
                or request.session["knowledge_iap_expires"] <= time.time()):
            request.session.clear()
            request.session.update(knowledge_iap_subject=identity["sub"],
                                   knowledge_iap_expires=time.time() + settings.browser_session_seconds,
                                   knowledge_csrf=secrets.token_urlsafe(32))
        return {"subject": identity["sub"], "groups": identity["groups"],
                "steward": settings.steward_group in identity["groups"],
                "csrf_token": request.session["knowledge_csrf"]}

    @router.post("/logout")
    def logout(request: Request):
        require_iap_browser(request, settings)
        request.session.clear()
        return {"signed_out": True}

    return router
