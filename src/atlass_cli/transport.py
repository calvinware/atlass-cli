"""Shared Data Center HTTP client: verified TLS, no redirects, no retries, sanitized errors."""

from __future__ import annotations

import json
import ssl
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import truststore

from atlass_cli import __version__
from atlass_cli.auth import Credential
from atlass_cli.contracts import AtlError

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_DETAIL_CHARS = 500
MAX_DETAIL_ITEMS = 50
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)
READ_METHODS = frozenset({"GET", "HEAD"})


def seg(value: str) -> str:
    """Encode a single URL path segment (issue key, page ID)."""
    return quote(value, safe="")


REDACTED = "[REDACTED]"


def sanitize_text(value: Any, limit: int = MAX_DETAIL_CHARS, redact: tuple[str, ...] = ()) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for secret in redact:
        text = text.replace(secret, REDACTED)
    cleaned = "".join(ch if ch.isprintable() or ch in " " else " " for ch in text)
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def sanitize_remote_errors(body: Any, redact: tuple[str, ...] = ()) -> dict[str, Any]:
    """Extract Jira/Confluence error messages into a bounded, printable, redacted structure."""
    fields: dict[str, str] = {}
    messages: list[str] = []
    if isinstance(body, dict):
        raw_fields = body.get("errors")
        if isinstance(raw_fields, dict):
            for key, value in list(raw_fields.items())[:MAX_DETAIL_ITEMS]:
                fields[sanitize_text(key, 100, redact)] = sanitize_text(value, redact=redact)
        raw_messages = body.get("errorMessages")
        if isinstance(raw_messages, list):
            messages.extend(
                sanitize_text(m, redact=redact) for m in raw_messages[:MAX_DETAIL_ITEMS]
            )
        message = body.get("message")
        if isinstance(message, str) and message:
            messages.append(sanitize_text(message, redact=redact))
    return {"fields": fields, "messages": messages}


def credential_redactions(credential: Credential | None) -> tuple[str, ...]:
    """Every form of the active secret a server could echo back, longest first."""
    if credential is None or not credential.secret:
        return ()
    header = credential.authorization_header()
    forms = {credential.secret, header, header.partition(" ")[2]}
    if credential.username is not None:
        forms.add(f"{credential.username}:{credential.secret}")
    return tuple(sorted((f for f in forms if f), key=len, reverse=True))


def build_ssl_context(ca_bundle: Path | None) -> ssl.SSLContext:
    try:
        if ca_bundle is not None:
            context = ssl.create_default_context(cafile=str(ca_bundle))
        else:
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except (ssl.SSLError, OSError) as exc:
        raise AtlError(
            "config_invalid",
            "The configured CA bundle could not be loaded.",
            "Point ca_bundle at a readable PEM file containing the team CA certificates.",
            {"ca_bundle": str(ca_bundle), "error_type": type(exc).__name__},
        ) from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _is_tls_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen = 0
    while current is not None and seen < 10:
        if isinstance(current, ssl.SSLError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


class ServiceClient:
    def __init__(
        self,
        service: str,
        base_url: str,
        credential: Credential | None,
        *,
        ca_bundle: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self.service = service
        self.base_url = base_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "User-Agent": f"atlass-cli/{__version__}",
        }
        if credential is not None:
            headers["Authorization"] = credential.authorization_header()
        self._redact = credential_redactions(credential)
        verify: ssl.SSLContext | bool = build_ssl_context(ca_bundle) if transport is None else True
        self._client = httpx.Client(
            headers=headers,
            verify=verify,
            transport=transport,
            follow_redirects=False,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ServiceClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        return self.base_url + path

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | Iterable[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        is_write = method.upper() not in READ_METHODS
        request = self._client.build_request(
            method,
            self.url(path),
            params=params,
            json=json_body,
            content=content,
            headers=headers,
        )
        try:
            response = self._client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise self._connect_failure(exc, is_write) from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise self._dispatch_failure(exc, is_write) from None
        except httpx.HTTPError as exc:
            raise self._dispatch_failure(exc, is_write) from None
        try:
            raw = self._read_body(response, is_write)
        finally:
            response.close()
        return self._handle(response, raw, is_write)

    # ------------------------------------------------------------------ failures

    def _connect_failure(self, exc: BaseException, is_write: bool) -> AtlError:
        outcome = "not_sent" if is_write else None
        if _is_tls_failure(exc):
            return AtlError(
                "tls_error",
                f"TLS verification failed for {self.service} ({self._host()}).",
                "Confirm the base_url host name, and if the server uses an internal CA set "
                f"{self.service}.ca_bundle in atl.yaml to that CA's PEM file. Certificate "
                "verification is never disabled.",
                {"service": self.service, "host": self._host(), "error_type": type(exc).__name__},
                write_outcome=outcome,
            )
        return AtlError(
            "transport_error",
            f"Could not connect to {self.service} ({self._host()}).",
            f"Check network/VPN access to {self._host()} and retry.",
            {"service": self.service, "host": self._host(), "error_type": type(exc).__name__},
            write_outcome=outcome,
        )

    def _dispatch_failure(self, exc: BaseException, is_write: bool) -> AtlError:
        if is_write:
            return self.unknown_outcome(type(exc).__name__)
        return AtlError(
            "transport_error",
            f"The connection to {self.service} failed during the request.",
            "Retry the read-only command; check network stability if it repeats.",
            {"service": self.service, "host": self._host(), "error_type": type(exc).__name__},
        )

    def unknown_outcome(self, reason: str, fix: str | None = None) -> AtlError:
        return AtlError(
            "outcome_unknown",
            f"The {self.service} write was dispatched but its outcome is unknown.",
            fix
            or "Inspect the remote item to see whether the change was applied before retrying; "
            "see 'Writes and retries' in docs/commands.md for how to inspect each write "
            "(comments and attachments need the issue view). atl does not retry writes.",
            {"service": self.service, "reason": sanitize_text(reason, 200)},
            write_outcome="unknown",
        )

    def _host(self) -> str:
        return urlsplit(self.base_url).netloc

    def _read_body(self, response: httpx.Response, is_write: bool) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise AtlError(
                        "response_invalid",
                        f"The {self.service} response exceeded {MAX_RESPONSE_BYTES} bytes.",
                        "Narrow the request (smaller --limit or more specific query).",
                        {"service": self.service},
                        write_outcome="unknown" if is_write else None,
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise self._dispatch_failure(exc, is_write) from None
        return b"".join(chunks)

    def _handle(self, response: httpx.Response, raw: bytes, is_write: bool) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            if not raw.strip():
                return None
            try:
                return json.loads(raw)
            except ValueError:
                if is_write:
                    raise self.unknown_outcome("non-JSON success response") from None
                raise AtlError(
                    "response_invalid",
                    f"{self.service} returned a non-JSON response.",
                    "Check that base_url points at the Data Center application root "
                    "(including any context path).",
                    {"service": self.service, "status": status},
                ) from None

        body: Any = None
        try:
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            body = None
        remote = sanitize_remote_errors(body, self._redact)
        details: dict[str, Any] = {"service": self.service, "status": status, **remote}
        rejected = "rejected" if is_write else None

        if 300 <= status < 400:
            location = response.headers.get("location", "")
            details["redirect_host"] = urlsplit(location).netloc or None
            if is_write:
                raise self.unknown_outcome(f"redirect {status}")
            raise AtlError(
                "redirect_refused",
                f"{self.service} answered with a redirect ({status}); atl does not follow "
                "redirects.",
                "Set base_url to the final HTTPS URL including the context path, and make sure "
                "SSO is not intercepting REST calls (use a PAT).",
                details,
            )
        if status == 401:
            raise AtlError(
                "auth_failed",
                f"{self.service} rejected the stored credential.",
                f"Run `atl login --service {self.service}` with a valid token or password.",
                details,
                write_outcome=rejected,
            )
        if status == 403:
            reason = response.headers.get("x-seraph-loginreason")
            if reason:
                details["login_reason"] = sanitize_text(reason, 100, self._redact)
            raise AtlError(
                "forbidden",
                f"{self.service} denied the request.",
                "Confirm your account has permission for this project/space. If login_reason "
                "mentions CAPTCHA, sign in through the browser once, then retry.",
                details,
                write_outcome=rejected,
            )
        if status == 404:
            raise AtlError(
                "not_found",
                f"{self.service} returned 404 Not Found.",
                "Check the key or ID and that your account can view it.",
                details,
                write_outcome=rejected,
            )
        if status == 409:
            raise AtlError(
                "conflict",
                f"{self.service} reported a conflict.",
                "Re-read the current remote state, reconcile, and resubmit.",
                details,
                write_outcome=rejected,
            )
        if status == 413:
            raise AtlError(
                "remote_limit_exceeded",
                f"{self.service} rejected the request as too large.",
                "The server or proxy limit is lower than atl's; reduce the payload size.",
                details,
                write_outcome=rejected,
            )
        if status == 429:
            details["retry_after"] = sanitize_text(
                response.headers.get("retry-after", ""), 20, self._redact
            )
            raise AtlError(
                "rate_limited",
                f"{self.service} rate-limited the request.",
                "Wait for the retry_after interval, then run the command again.",
                details,
                write_outcome=rejected,
            )
        if status >= 500:
            if is_write:
                error = self.unknown_outcome(f"server error {status}")
                error.details.update(remote)
                raise error
            raise AtlError(
                "service_error",
                f"{self.service} returned server error {status}.",
                "Retry later; contact the Atlassian administrators if it persists.",
                details,
            )
        raise AtlError(
            "remote_validation",
            f"{self.service} rejected the request ({status}).",
            "Correct the values listed in details.fields/details.messages and retry.",
            details,
            write_outcome=rejected,
        )
