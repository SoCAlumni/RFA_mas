"""Small same-PC safety boundary shared by the replaceable local stand-in services.

This is a development boundary for one OS account on one PC. It is not an OS
sandbox, network sandbox, OpenShell policy, or multi-user identity service.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import stat
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rfa_mas.contracts import StructuredError

OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MIN_SERVICE_TOKEN_LENGTH = 24

# Fixed, input-free messages. Codes are chosen by server code, never by a caller.
SAFE_MESSAGES: Mapping[str, str] = {
    "forwarded_rejected": "전달/프록시 헤더가 있는 요청은 허용되지 않습니다.",
    "loopback_required": "같은 PC의 loopback 연결만 허용됩니다.",
    "host_not_allowed": "허용된 Host/port가 아닙니다.",
    "origin_rejected": "같은 출처의 요청만 허용됩니다.",
    "origin_required": "브라우저 변경 요청에는 같은 출처 Origin이 필요합니다.",
    "unsupported_media_type": "JSON 요청만 허용됩니다.",
    "length_required": "요청 본문 길이가 필요합니다.",
    "payload_too_large": "요청 본문이 너무 큽니다.",
    "authentication_required": "유효한 서비스 인증이 필요합니다.",
    "csrf_rejected": "유효한 CSRF 확인이 필요합니다.",
    "invalid_request": "요청 형식이 올바르지 않습니다.",
    "idempotency_key_required": "유효한 Idempotency-Key가 필요합니다.",
    "idempotency_conflict": "같은 idempotency key로 다른 요청을 보낼 수 없습니다.",
    "not_found": "리소스를 찾을 수 없습니다.",
    "method_not_allowed": "허용되지 않은 method입니다.",
    "configuration_error": "로컬 서비스 설정이 올바르지 않습니다.",
    "storage_busy": "로컬 저장소가 사용 중입니다. 결과를 확인한 뒤 다시 시도하세요.",
}
GENERIC_MESSAGE = "요청을 안전하게 처리할 수 없습니다."
_SECURITY_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
)


class LocalServiceError(Exception):
    """Safe service error. The message is looked up by code and never echoes input."""

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code

    @property
    def safe_message(self) -> str:
        return SAFE_MESSAGES.get(self.code, GENERIC_MESSAGE)


def register_safe_messages(messages: Mapping[str, str]) -> None:
    """Service modules add their own fixed messages at import time."""

    SAFE_MESSAGES.update(messages)  # type: ignore[attr-defined]


def error_response(status_code: int, code: str) -> JSONResponse:
    error = StructuredError(
        code=code, retryable=False, message=SAFE_MESSAGES.get(code, GENERIC_MESSAGE)
    )
    return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))


def fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_opaque_id(value: str | None) -> bool:
    return value is not None and OPAQUE_ID.fullmatch(value) is not None


def require_idempotency_key(value: str | None) -> str:
    if value is None or not is_opaque_id(value):
        raise LocalServiceError("idempotency_key_required", 400)
    return value


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _is_loopback_peer(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        peer = ipaddress.ip_address(client[0])
    except (TypeError, ValueError):
        return False
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        return peer.ipv4_mapped.is_loopback
    return peer.is_loopback


@dataclass(frozen=True, slots=True)
class LocalServiceBoundary:
    """Server-injected installation owner, service token and exact Host allowlist.

    The owner is verified by the process that constructs the service. Request
    bodies and headers can never replace it.
    """

    owner_id: str
    service_token: SecretStr = field(repr=False)
    allowed_hosts: frozenset[str] = frozenset()
    max_body_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if not is_opaque_id(self.owner_id):
            raise ValueError("owner_id must be an opaque server identifier")
        if not isinstance(self.service_token, SecretStr):
            raise TypeError("service_token must be a SecretStr")
        if len(self.service_token.get_secret_value()) < MIN_SERVICE_TOKEN_LENGTH:
            raise ValueError("service_token is too short")
        hosts = frozenset(host.lower() for host in self.allowed_hosts)
        if not hosts:
            raise ValueError("allowed_hosts must not be empty")
        for host in hosts:
            name, separator, port = host.rpartition(":")
            if not separator or not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError("allowed_hosts entries must be exact host:port values")
            if not _is_loopback_host(name):
                raise ValueError("allowed_hosts must be loopback hosts")
        object.__setattr__(self, "allowed_hosts", hosts)
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")

    @classmethod
    def create(
        cls,
        *,
        owner_id: str,
        service_token: SecretStr,
        allowed_hosts: Iterable[str],
        max_body_bytes: int = 1_000_000,
    ) -> LocalServiceBoundary:
        return cls(
            owner_id=owner_id,
            service_token=service_token,
            allowed_hosts=frozenset(allowed_hosts),
            max_body_bytes=max_body_bytes,
        )

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.allowed_hosts)

    def bearer_matches(self, authorization: str | None) -> bool:
        expected = f"Bearer {self.service_token.get_secret_value()}".encode()
        supplied = (authorization or "").encode("utf-8", "surrogateescape")
        return hmac.compare_digest(supplied, expected)


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1") for key, value in scope.get("headers", ()) if key.lower() == name
    ]


def boundary_rejection(scope: Scope, boundary: LocalServiceBoundary) -> tuple[int, str] | None:
    """Return a safe (status, code) when a request crosses the same-PC boundary."""

    names = [key.decode("latin-1").lower() for key, _ in scope.get("headers", ())]
    if any(n in {"forwarded", "x-real-ip"} or n.startswith("x-forwarded-") for n in names):
        return 400, "forwarded_rejected"
    if not _is_loopback_peer(scope):
        return 403, "loopback_required"
    hosts = _header_values(scope, b"host")
    if len(hosts) != 1 or hosts[0].lower() not in boundary.allowed_hosts:
        return 400, "host_not_allowed"
    host = hosts[0].lower()
    origins = _header_values(scope, b"origin")
    if len(origins) > 1 or (origins and origins[0].lower() != f"http://{host}"):
        return 403, "origin_rejected"
    fetch_site = _header_values(scope, b"sec-fetch-site")
    if fetch_site and fetch_site[0].lower() not in {"same-origin", "none"}:
        return 403, "origin_rejected"
    method = scope.get("method", "GET").upper()
    if method in UNSAFE_METHODS:
        browser_markers = any(n.startswith("sec-fetch-") or n == "cookie" for n in names)
        if not origins and browser_markers:
            return 403, "origin_required"
        if _header_values(scope, b"transfer-encoding"):
            return 411, "length_required"
        lengths = _header_values(scope, b"content-length")
        if len(lengths) > 1:
            return 400, "invalid_request"
        length = lengths[0] if lengths else "0"
        if not length.isdigit():
            return 400, "invalid_request"
        if int(length) > boundary.max_body_bytes:
            return 413, "payload_too_large"
        if int(length) > 0:
            content_types = _header_values(scope, b"content-type")
            media = content_types[0].split(";", 1)[0].strip().lower() if content_types else ""
            if len(content_types) != 1 or media != "application/json":
                return 415, "unsupported_media_type"
    return None


class LocalBoundaryMiddleware:
    """Pure ASGI guard applied before routing, validation, docs and 404 handling."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        boundary: LocalServiceBoundary,
        require_bearer: bool = True,
        public_paths: frozenset[str] = frozenset({"/healthz"}),
        extra_headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> None:
        self.app = app
        self.boundary = boundary
        self.require_bearer = require_bearer
        self.public_paths = public_paths
        self.extra_headers = extra_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        rejection = boundary_rejection(scope, self.boundary)
        if rejection is None and self.require_bearer and scope.get("path") not in self.public_paths:
            authorizations = _header_values(scope, b"authorization")
            if len(authorizations) != 1 or not self.boundary.bearer_matches(authorizations[0]):
                rejection = (401, "authentication_required")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                present = {key.lower() for key, _ in headers}
                headers.extend(
                    (key, value)
                    for key, value in (*_SECURITY_HEADERS, *self.extra_headers)
                    if key not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        if rejection is not None:
            await error_response(*rejection)(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)


def install_safe_error_handlers(app: FastAPI) -> None:
    """Replace FastAPI defaults that reflect request bodies, field names or input values."""

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "invalid_request")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request")
        return error_response(exc.status_code, code)

    @app.exception_handler(LocalServiceError)
    async def _local(request: Request, exc: LocalServiceError) -> JSONResponse:
        return error_response(exc.status_code, exc.code)


def verified_owner_dependency(
    boundary: LocalServiceBoundary,
) -> Callable[[Request], Awaitable[str]]:
    """Defense in depth behind the middleware: owner comes only from server config."""

    async def dependency(request: Request) -> str:
        if not boundary.bearer_matches(request.headers.get("authorization")):
            raise LocalServiceError("authentication_required", 401)
        return boundary.owner_id

    return dependency


def _chmod_private(path: Path, expected: os.stat_result) -> None:
    """chmod 0600 by path without following symlinks where supported, else re-check."""
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, 0o600, follow_symlinks=False)
        return
    os.chmod(path, 0o600)
    after = os.lstat(path)
    if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise OSError("sqlite file replaced during permission check")


class PrivateSqlite:
    """One service-owned SQLite file with owner-only permissions.

    Refuses symlinks, hardlinks, foreign-owned files, and databases that already
    contain another service's tables (for example the core RFA database).
    """

    def __init__(self, path: Path, *, service: str, schema: tuple[str, ...]) -> None:
        self.path = Path(path)
        self.service = service
        self.schema = schema
        self.tables = frozenset(
            match.group(1)
            for statement in schema
            if (match := re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", statement))
        ) | {"service_meta"}

    def _private_files(self, *, create: bool = False, harden: bool = True) -> None:
        # P0-005A: lstat and chmod-by-path only. Closing any descriptor for the DB,
        # -wal or -shm drops every SQLite fcntl lock this process holds on it, so a
        # second process could reset the WAL index under live connections. A
        # descriptor is opened only to create a missing DB (O_EXCL).
        try:
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{self.path}{suffix}")
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    if suffix:
                        continue  # SQLite removes sidecars with its final connection.
                    if not create:
                        raise LocalServiceError("configuration_error", 503) from None
                    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW
                    try:
                        os.close(os.open(path, flags, 0o600))
                        continue
                    except FileExistsError:
                        info = os.lstat(path)
                # A sidecar being unlinked by SQLite's final close can report st_nlink 0.
                links = {1} if not suffix else {0, 1}
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink not in links
                ):
                    raise LocalServiceError("configuration_error", 503)
                if harden and info.st_nlink and stat.S_IMODE(info.st_mode) != 0o600:
                    try:
                        _chmod_private(path, info)
                    except FileNotFoundError:
                        if not suffix:
                            raise
        except OSError:
            raise LocalServiceError("configuration_error", 503) from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _refuse_foreign(self, connection: sqlite3.Connection) -> None:
        existing = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if existing - self.tables - {"sqlite_sequence"}:
            raise LocalServiceError("configuration_error", 503)
        if "service_meta" in existing:
            row = connection.execute(
                "SELECT value FROM service_meta WHERE key = 'service'"
            ).fetchone()
            if row is None or row[0] != self.service:
                raise LocalServiceError("configuration_error", 503)
        elif existing:
            raise LocalServiceError("configuration_error", 503)

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise LocalServiceError("configuration_error", 503)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Validate type/owner/links without modifying a possibly foreign file.
        self._private_files(create=True, harden=False)
        connection = self._connect()
        try:
            # Read-only inspection first: never chmod or switch the journal of
            # another service's database (for example the core RFA DB).
            connection.execute("BEGIN")
            try:
                self._refuse_foreign(connection)
            finally:
                connection.execute("ROLLBACK")
            self._private_files()
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise LocalServiceError("configuration_error", 503)
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._refuse_foreign(connection)
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS service_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                for statement in self.schema:
                    connection.execute(statement)
                connection.execute(
                    "INSERT OR IGNORE INTO service_meta (key, value) VALUES ('service', ?)",
                    (self.service,),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        self._private_files()

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        self._private_files()
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            except sqlite3.OperationalError:
                raise LocalServiceError("storage_busy", 503) from None
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()
