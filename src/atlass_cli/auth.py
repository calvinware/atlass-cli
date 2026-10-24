"""Credential storage in the native OS keyring. There is no plaintext fallback."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol

from atlass_cli.contracts import AtlError

KEYRING_SERVICE = "atlass-cli"
MAX_SECRET_CHARS = 4096

# Backends that delegate to an OS-protected credential store. Anything else (keyrings.alt file
# backends, the null/fail backends, third-party plugins) is refused.
NATIVE_BACKENDS = frozenset(
    {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.libsecret.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
    }
)

KEYRING_FIX = (
    "Use a native OS keyring: macOS Keychain, Windows Credential Manager, or a running Linux "
    "Secret Service (GNOME Keyring/KWallet) with an unlocked session. Do not configure a "
    "plaintext keyring backend."
)


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


@dataclass(frozen=True)
class Credential:
    auth: str
    secret: str
    username: str | None = None

    def authorization_header(self) -> str:
        if self.auth == "pat":
            return f"Bearer {self.secret}"
        raw = f"{self.username}:{self.secret}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def __repr__(self) -> str:
        return f"Credential(auth={self.auth!r}, username={self.username!r}, secret='***')"


def backend_name(backend: object) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__qualname__}"


def _leaf_backends(backend: Any) -> list[Any]:
    children = getattr(backend, "backends", None)
    if backend_name(backend) == "keyring.backends.chainer.ChainerBackend" and children is not None:
        return list(children)
    return [backend]


def ensure_native_backend(backend: Any, allowed: frozenset[str] = NATIVE_BACKENDS) -> None:
    leaves = _leaf_backends(backend)
    names = [backend_name(leaf) for leaf in leaves]
    if not leaves or any(name not in allowed for name in names):
        raise AtlError(
            "keyring_unavailable",
            "No secure OS keyring backend is available.",
            KEYRING_FIX,
            {"backends": names},
        )


def account_name(service: str, base_url: str) -> str:
    return f"{service}|{base_url}"


class CredentialStore:
    def __init__(self, backend: KeyringBackend) -> None:
        self._backend = backend

    def load(self, service: str, base_url: str) -> Credential:
        account = account_name(service, base_url)
        try:
            raw = self._backend.get_password(KEYRING_SERVICE, account)
        except Exception as exc:  # noqa: BLE001 - keyring backends raise many types
            raise _keyring_error("read", exc) from None
        if raw is None:
            raise AtlError(
                "auth_missing",
                f"No stored {service} credential for {base_url}.",
                f"Run `atl login --service {service} --auth pat --token-stdin` and pipe the token "
                "on stdin, or run it in a terminal to be prompted.",
                {"service": service, "base_url": base_url},
            )
        return _decode(raw, service)

    def save(self, service: str, base_url: str, credential: Credential) -> None:
        payload = {"v": 1, "auth": credential.auth, "secret": credential.secret}
        if credential.username is not None:
            payload["username"] = credential.username
        try:
            self._backend.set_password(
                KEYRING_SERVICE, account_name(service, base_url), json.dumps(payload)
            )
        except Exception as exc:  # noqa: BLE001 - keyring backends raise many types
            raise _keyring_error("write", exc) from None


def _decode(raw: str, service: str) -> Credential:
    try:
        payload = json.loads(raw)
        auth = payload["auth"]
        secret = payload["secret"]
        username = payload.get("username")
        if auth not in ("pat", "basic") or not isinstance(secret, str) or not secret:
            raise ValueError
        if auth == "basic" and not isinstance(username, str):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise AtlError(
            "auth_missing",
            f"The stored {service} credential is unreadable.",
            f"Run `atl login --service {service}` again to replace it.",
            {"service": service},
        ) from None
    return Credential(auth=auth, secret=secret, username=username)


def _keyring_error(action: str, exc: BaseException) -> AtlError:
    return AtlError(
        "keyring_unavailable",
        f"The OS keyring could not {action} the credential.",
        KEYRING_FIX,
        {"error_type": type(exc).__name__},
    )


def default_store() -> CredentialStore:
    try:
        import keyring
    except Exception as exc:  # noqa: BLE001 - import-time backend failures vary by OS
        raise _keyring_error("load", exc) from None
    backend = keyring.get_keyring()
    ensure_native_backend(backend)
    return CredentialStore(backend)


def validate_secret(secret: str, label: str) -> str:
    cleaned = secret.rstrip("\r\n")
    if not cleaned:
        raise AtlError(
            "usage_error",
            f"The {label} is empty.",
            f"Provide a non-empty {label} on stdin or at the prompt.",
        )
    if len(cleaned) > MAX_SECRET_CHARS:
        raise AtlError(
            "usage_error",
            f"The {label} is longer than {MAX_SECRET_CHARS} characters.",
            f"Check that only the {label} was supplied.",
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise AtlError(
            "usage_error",
            f"The {label} contains control characters or multiple lines.",
            f"Supply exactly one {label} per line.",
        )
    return cleaned


def validate_username(username: str) -> str:
    cleaned = username.strip()
    if not cleaned or ":" in cleaned or any(ord(ch) < 32 for ch in cleaned) or len(cleaned) > 255:
        raise AtlError(
            "usage_error",
            "The username is empty or contains ':' or control characters.",
            "Pass a valid Data Center username with --username.",
        )
    return cleaned
