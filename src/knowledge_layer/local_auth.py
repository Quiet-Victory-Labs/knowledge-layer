"""Dependency-free, single-owner authentication for a private self-hosted install.

An operator-held bootstrap secret claims the owner exactly once. Passwords use
salted PBKDF2-HMAC-SHA256 (600,000 iterations); signed sessions are checked against
the current private owner record. This is not a multi-user directory or SSO.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from collections import deque
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from starlette.responses import JSONResponse, RedirectResponse

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.operator_secrets import operator_secret, read_owner_only_file
from knowledge_layer.origin import normalize_origin

OWNER = "local-owner"
AGENT = "local-agent"
GROUP = "knowledge-users"
STEWARDS = "knowledge-stewards"
COOKIE = "knowledge_local_session"
SESSION_SECONDS = 900
ITERATIONS = 600_000


def _equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate owner record fields")
        result[key] = value
    return result


class LocalSettings:
    allowed_group = GROUP
    steward_group = STEWARDS
    browser_session_seconds = SESSION_SECONDS

    def __init__(self):
        self.public_url = normalize_origin(os.getenv("KNOWLEDGE_PUBLIC_URL", "http://127.0.0.1:4173"))
        parsed = urlsplit(self.public_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})):
            raise ValueError("Local owner mode requires HTTPS or a loopback HTTP origin")
        self.host = parsed.netloc.lower()
        self.secure = parsed.scheme == "https"
        self.session_key = operator_secret("KNOWLEDGE_LOCAL_SESSION_KEY") or ""
        self.bootstrap_token = operator_secret("KNOWLEDGE_LOCAL_BOOTSTRAP_TOKEN") or ""
        if len(self.session_key) < 32 or len(self.bootstrap_token) < 32:
            raise ValueError("Local owner mode requires protected session and bootstrap secrets")
        raw_path = os.getenv("KNOWLEDGE_LOCAL_AUTH_FILE", "")
        self.path = Path(raw_path)
        if not raw_path or not self.path.is_absolute() or self.path.parent.resolve() != self.path.parent:
            raise ValueError("Local owner record requires a private absolute path")
        details = self.path.parent.stat()
        if details.st_uid != os.geteuid() or details.st_mode & 0o077 or not stat.S_ISDIR(details.st_mode):
            raise ValueError("Local owner directory must be owned by the runtime user and private")
        self.serializer = URLSafeTimedSerializer(self.session_key, salt="knowledge-local-owner-v1")
        self._attempts = deque()
        self._lock = Lock()
        self._hash_slots = asyncio.Semaphore(2)
        self.owner()  # Refuse an invalid retained record, never silently reclaim it.

    def owner(self):
        try:
            raw = read_owner_only_file(str(self.path), max_bytes=8192)
        except ValueError as exc:
            if not self.path.exists() and not self.path.is_symlink():
                return None
            raise ValueError("Private owner record is unavailable") from exc
        record = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(record, dict) or set(record) != {"version", "name", "salt", "hash", "session_version"}
                or type(record["version"]) is not int or record["version"] != 1
                or not isinstance(record["name"], str) or not 1 <= len(record["name"]) <= 120
                or any(not isinstance(record[key], str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                       for key in ("salt", "hash", "session_version"))):
            raise ValueError("Private owner record is invalid")
        return record

    def limit_attempt(self):
        # A global per-process budget also bounds rotating-IP password attacks.
        with self._lock:
            now = time.monotonic()
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= 12:
                raise HTTPException(429, "Too many sign-in attempts; wait one minute", headers={"Retry-After": "60"})
            self._attempts.append(now)

    def write_owner(self, record, *, create):
        raw = (json.dumps(record, sort_keys=True) + "\n").encode()
        target = self.path if create else self.path.with_name(".owner-" + secrets.token_hex(12))
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if not create:
                os.replace(target, self.path)
        finally:
            if not create:
                target.unlink(missing_ok=True)

    def identity(self, request: Request):
        record = self.owner()
        if record is None:
            raise HTTPException(401, "Claim this installation before signing in")
        try:
            session = self.serializer.loads(request.cookies.get(COOKIE, ""), max_age=SESSION_SECONDS)
            if (not isinstance(session, dict) or session.get("sub") != OWNER
                    or session.get("version") != record["session_version"]
                    or not isinstance(session.get("csrf"), str) or len(session["csrf"]) < 32):
                raise BadSignature("Invalid local session")
        except (BadSignature, ValueError, TypeError) as exc:
            raise HTTPException(401, "Sign in is required") from exc
        return session


def same_origin(request: Request, settings: LocalSettings):
    if request.headers.get("origin") != settings.public_url:
        raise HTTPException(403, "Use this installation's own origin")


def require_local_browser(request: Request, settings: LocalSettings):
    try:
        session = settings.identity(request)
    except (ValueError, OSError) as exc:
        raise HTTPException(503, "Private owner configuration is unavailable") from exc
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        same_origin(request, settings)
        supplied = request.headers.get("x-knowledge-csrf", "")
        if not _equal(supplied, session["csrf"]):
            raise HTTPException(403, "A current request token is required")
    request.scope["knowledge_actor"] = OWNER
    return {"sub": OWNER, "groups": [GROUP, STEWARDS], "csrf_token": session["csrf"]}


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=1, max_length=120)
    password: StrictStr = Field(min_length=12, max_length=1024)


class Claim(Credentials):
    bootstrap_token: StrictStr = Field(min_length=1, max_length=4096)


def _session_response(settings, record):
    csrf = secrets.token_urlsafe(32)
    response = JSONResponse({"sub": OWNER, "subject": OWNER, "groups": [GROUP, STEWARDS],
                             "steward": True, "csrf_token": csrf}, headers={"Cache-Control": "no-store"})
    value = settings.serializer.dumps({"sub": OWNER, "version": record["session_version"], "csrf": csrf})
    response.set_cookie(COOKIE, value, max_age=SESSION_SECONDS, httponly=True,
                        secure=settings.secure, samesite="strict")
    return response


def local_auth_router(settings):
    router = APIRouter(prefix="/auth", tags=["owner"])

    async def password_hash(password, salt):
        try:
            async with asyncio.timeout(5):
                async with settings._hash_slots:
                    return await asyncio.to_thread(_hash, password, salt)
        except TimeoutError as exc:
            raise HTTPException(429, "Sign-in is busy; retry shortly") from exc

    @router.post("/setup")
    async def claim(request: Request, body: Claim):
        same_origin(request, settings)
        settings.limit_attempt()
        if settings.owner() is not None:
            raise HTTPException(409, "This installation already has an owner")
        if not _equal(body.bootstrap_token, settings.bootstrap_token):
            raise HTTPException(403, "The operator bootstrap token is required")
        name = body.name.strip()
        if not name or any(ord(value) < 32 for value in name):
            raise HTTPException(422, "Use a valid owner name")
        salt = secrets.token_hex(32)
        record = {"version": 1, "name": name, "salt": salt,
                  "hash": await password_hash(body.password, salt), "session_version": secrets.token_hex(32)}
        try:
            settings.write_owner(record, create=True)
        except FileExistsError as exc:
            raise HTTPException(409, "This installation already has an owner") from exc
        return _session_response(settings, record)

    @router.post("/login")
    async def login(request: Request, body: Credentials):
        same_origin(request, settings)
        settings.limit_attempt()
        record = settings.owner()
        salt = record["salt"] if record else "0" * 64
        candidate = await password_hash(body.password, salt)
        if (record is None or not hmac.compare_digest(candidate, record["hash"])
                or body.name.strip().casefold() != record["name"].casefold()):
            raise HTTPException(401, "Owner name or password is incorrect")
        return _session_response(settings, record)

    @router.get("/login")
    def login_page():
        return RedirectResponse("/", status_code=303)

    @router.get("/me")
    def me(request: Request):
        identity = require_local_browser(request, settings)
        return JSONResponse(identity | {"subject": OWNER, "steward": True}, headers={"Cache-Control": "no-store"})

    @router.post("/logout")
    def logout(request: Request):
        require_local_browser(request, settings)
        record = settings.owner()
        record["session_version"] = secrets.token_hex(32)
        settings.write_owner(record, create=False)
        response = JSONResponse({"signed_out": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, secure=settings.secure, httponly=True, samesite="strict")
        return response

    return router


class LocalHostBoundary:
    """Prevent a loopback owner profile from being reached through another host."""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("path") in {"/api/health", "/api/ready"} and scope.get("method") in {"GET", "HEAD"}:
                await self.app(scope, receive, send)
                return
            hosts = [v.decode("latin-1").lower() for k, v in scope.get("headers", []) if k.lower() == b"host"]
            if hosts != [self.settings.host]:
                await JSONResponse({"detail": "Use the configured installation URL"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class LocalAgentBoundary:
    """An installation credential gives draft access, never the owner's session."""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        try:
            configured = self.settings.owner() is not None
            expected = operator_secret("KNOWLEDGE_AGENT_TOKEN") or ""
        except (OSError, ValueError):
            configured, expected = False, ""
        values = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
        supplied = values[0].decode("latin-1")[7:] if len(values) == 1 and values[0].startswith(b"Bearer ") else ""
        if not configured or len(expected) < 32:
            await JSONResponse({"detail": "Agent access is not configured"}, status_code=503)(scope, receive, send)
            return
        if len(supplied) > 4096 or not _equal(supplied, expected):
            await JSONResponse({"detail": "A valid installation agent credential is required"}, status_code=401)(scope, receive, send)
            return
        scope["knowledge_actor"] = AGENT
        with principal_scope(Principal(AGENT, frozenset({GROUP}), frozenset({"knowledge.agent"}))):
            await self.app(scope, receive, send)
