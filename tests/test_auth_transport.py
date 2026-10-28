from __future__ import annotations

import base64
import json
import ssl
from pathlib import Path

import httpx
import pytest

from tests.conftest import (
    CONFLUENCE_TOKEN,
    JIRA,
    JIRA_TOKEN,
    FakeKeyring,
    FakeServer,
    Harness,
    confluence_path,
    jira_path,
)
from yojo import transport
from yojo.auth import (
    NATIVE_BACKENDS,
    Credential,
    CredentialStore,
    ensure_native_backend,
)
from yojo.contracts import YojoError
from yojo.transport import ServiceClient, build_ssl_context, sanitize_remote_errors

MYSELF = jira_path("/rest/api/2/myself")
CURRENT_USER = confluence_path("/rest/api/user/current")


# --------------------------------------------------------------------------- keyring


def _backend(module: str, name: str) -> object:
    return type(name, (), {"__module__": module})()


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("keyring.backends.fail", "Keyring"),
        ("keyring.backends.null", "Keyring"),
        ("keyrings.alt.file", "PlaintextKeyring"),
        ("keyrings.alt.file", "EncryptedKeyring"),
        ("some.plugin", "Keyring"),
    ],
)
def test_non_native_backends_fail_closed(module: str, name: str) -> None:
    with pytest.raises(YojoError) as info:
        ensure_native_backend(_backend(module, name))
    assert info.value.code == "keyring_unavailable"
    assert info.value.exit_code == 3


def test_chainer_requires_all_native() -> None:
    chainer_cls = type("ChainerBackend", (), {"__module__": "keyring.backends.chainer"})
    chainer = chainer_cls()
    chainer.backends = [
        _backend("keyring.backends.macOS", "Keyring"),
        _backend("keyrings.alt.file", "PlaintextKeyring"),
    ]
    with pytest.raises(YojoError):
        ensure_native_backend(chainer)
    chainer.backends = []
    with pytest.raises(YojoError):
        ensure_native_backend(chainer)
    chainer.backends = [_backend("keyring.backends.macOS", "Keyring")]
    ensure_native_backend(chainer)


@pytest.mark.parametrize("qualified", sorted(NATIVE_BACKENDS))
def test_native_backends_accepted(qualified: str) -> None:
    module, _, name = qualified.rpartition(".")
    ensure_native_backend(_backend(module, name))


def test_store_round_trip_and_separation() -> None:
    keyring = FakeKeyring()
    store = CredentialStore(keyring)
    store.save("jira", JIRA, Credential(auth="pat", secret="a"))
    store.save("jira", "https://other.example.test/jira", Credential(auth="pat", secret="b"))
    store.save("confluence", JIRA, Credential(auth="basic", secret="c", username="u"))
    assert store.load("jira", JIRA).secret == "a"
    assert store.load("jira", "https://other.example.test/jira").secret == "b"
    assert store.load("confluence", JIRA).username == "u"
    assert len(keyring.data) == 3


def test_missing_and_corrupt_credentials() -> None:
    keyring = FakeKeyring()
    store = CredentialStore(keyring)
    with pytest.raises(YojoError) as info:
        store.load("jira", JIRA)
    assert info.value.code == "auth_missing"
    assert "yojo login" in info.value.fix
    keyring.data[("atlass-cli", f"jira|{JIRA}")] = "not json"
    with pytest.raises(YojoError) as info:
        store.load("jira", JIRA)
    assert info.value.code == "auth_missing"


def test_keyring_exceptions_are_redacted() -> None:
    class Broken(FakeKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise RuntimeError("dbus error with secret-ish detail")

    with pytest.raises(YojoError) as info:
        CredentialStore(Broken()).load("jira", JIRA)
    assert info.value.code == "keyring_unavailable"
    assert "secret-ish" not in json.dumps(info.value.envelope())


def test_credential_repr_hides_secret() -> None:
    assert "topsecret" not in repr(Credential(auth="pat", secret="topsecret"))


# --------------------------------------------------------------------------- login


def test_login_pat_stdin_verifies_then_stores(harness: Harness) -> None:
    harness.write_config()
    harness.server.add("GET", MYSELF, {"name": "jdoe", "displayName": "J Doe"})
    result = harness.run(
        "login", "--service", "jira", "--token-stdin", stdin=(JIRA_TOKEN + "\n").encode()
    )
    assert result.code == 0
    assert result.data["services"] == [
        {"service": "jira", "auth": "pat", "base_url": JIRA, "user": "jdoe", "status": "stored"}
    ]
    request = harness.server.requests[0]
    assert request.headers["authorization"] == f"Bearer {JIRA_TOKEN}"
    assert str(request.url) == JIRA + "/rest/api/2/myself"
    stored = json.loads(harness.stored("jira") or "")
    assert stored["secret"] == JIRA_TOKEN
    assert JIRA_TOKEN not in result.stdout + result.stderr


def test_login_rejected_credential_is_not_stored(harness: Harness) -> None:
    harness.write_config()
    harness.server.add("GET", MYSELF, {"errorMessages": ["nope"]}, status=401)
    result = harness.run("login", "--service", "jira", "--token-stdin", stdin=b"bad-token\n")
    assert result.code == 3
    assert result.error["code"] == "auth_failed"
    assert harness.stored("jira") is None
    assert "bad-token" not in result.stdout


def test_login_without_tty_requires_stdin_flag(harness: Harness) -> None:
    harness.write_config()
    result = harness.run("login", "--service", "jira", stdin=b"token\n")
    assert result.code == 2
    assert "--token-stdin" in result.error["fix"]
    assert harness.server.requests == []


def test_login_tty_prompt_goes_to_stderr_as_json(harness: Harness) -> None:
    harness.write_config()
    harness.server.add("GET", MYSELF, {"name": "jdoe"})
    result = harness.run("login", "--service", "jira", tty=True, hidden=[JIRA_TOKEN])
    assert result.code == 0
    prompts = [json.loads(line) for line in result.stderr.splitlines()]
    assert prompts == [
        {
            "prompt": {
                "kind": "token",
                "service": "jira",
                "message": (
                    "jira personal access token for https://jira.example.test/jira (input hidden)"
                ),
            }
        }
    ]
    assert JIRA_TOKEN not in result.stderr + result.stdout


def test_login_basic_with_username(harness: Harness) -> None:
    harness.write_config()
    harness.server.add("GET", MYSELF, {"name": "jdoe"})
    result = harness.run(
        "login",
        "--service",
        "jira",
        "--auth",
        "basic",
        "--username",
        "jdoe",
        "--password-stdin",
        stdin=b"pa:ss\n",
    )
    assert result.code == 0
    expected = "Basic " + base64.b64encode(b"jdoe:pa:ss").decode()
    assert harness.server.requests[0].headers["authorization"] == expected
    stored = json.loads(harness.stored("jira") or "")
    assert stored == {"v": 1, "auth": "basic", "secret": "pa:ss", "username": "jdoe"}


@pytest.mark.parametrize(
    "argv",
    [
        ["--auth", "basic", "--password-stdin"],
        ["--auth", "pat", "--password-stdin"],
        ["--auth", "basic", "--username", "u", "--token-stdin"],
        ["--auth", "pat", "--username", "u", "--token-stdin"],
        ["--auth", "basic", "--username", "a:b", "--password-stdin"],
    ],
)
def test_login_flag_combinations(harness: Harness, argv: list[str]) -> None:
    harness.write_config()
    result = harness.run("login", "--service", "jira", *argv, stdin=b"secret\n")
    assert result.code == 2
    assert harness.server.requests == []


def test_login_rejects_multiline_or_empty_secret(harness: Harness) -> None:
    harness.write_config()
    assert harness.run("login", "--service", "jira", "--token-stdin", stdin=b"\n").code == 2
    assert harness.run("login", "--service", "jira", "--token-stdin", stdin=b"").code == 2
    assert harness.server.requests == []


def test_login_all_reports_each_service_and_fails(harness: Harness) -> None:
    harness.write_config()
    harness.server.add("GET", MYSELF, {"name": "jdoe"})
    harness.server.add("GET", CURRENT_USER, {"type": "anonymous"})
    stdin = f"{JIRA_TOKEN}\n{CONFLUENCE_TOKEN}\n".encode()
    result = harness.run("login", "--service", "all", "--token-stdin", stdin=stdin)
    assert result.code == 3
    assert result.error["code"] == "login_failed"
    services = {s["service"]: s for s in result.error["details"]["services"]}
    assert services["jira"]["status"] == "stored"
    assert services["confluence"]["status"] == "failed"
    assert services["confluence"]["error"]["code"] == "auth_failed"
    assert harness.stored("jira") is not None
    assert harness.stored("confluence") is None
    confluence_request = harness.server.requests[1]
    assert confluence_request.headers["authorization"] == f"Bearer {CONFLUENCE_TOKEN}"
    assert JIRA_TOKEN not in result.stdout and CONFLUENCE_TOKEN not in result.stdout


def test_login_all_success(harness: Harness) -> None:
    harness.write_config()
    harness.server.add("GET", MYSELF, {"name": "jdoe"})
    harness.server.add("GET", CURRENT_USER, {"type": "known", "username": "jdoe"})
    stdin = f"{JIRA_TOKEN}\n{CONFLUENCE_TOKEN}\n".encode()
    result = harness.run("login", "--service", "all", "--token-stdin", stdin=stdin)
    assert result.code == 0
    assert [s["status"] for s in result.data["services"]] == ["stored", "stored"]


def test_login_all_requires_both_services(harness: Harness) -> None:
    harness.write_config("version: 1\nconfluence:\n  base_url: https://wiki.example.test\n")
    result = harness.run("login", "--service", "all", "--token-stdin", stdin=b"a\nb\n")
    assert result.error["code"] == "service_not_configured"


def test_login_keyring_unavailable_fails_before_network(harness: Harness) -> None:
    harness.write_config()

    def unavailable() -> CredentialStore:
        raise YojoError("keyring_unavailable", "no keyring", "use a native keyring")

    result = harness.run(
        "login", "--service", "jira", "--token-stdin", stdin=b"tok\n", store_factory=unavailable
    )
    assert result.code == 3
    assert result.error["code"] == "keyring_unavailable"
    assert harness.server.requests == []


def test_commands_require_stored_credentials(harness: Harness) -> None:
    harness.write_config()
    result = harness.run("jira", "search", "--jql", "project = EXAMPLE")
    assert result.code == 3
    assert result.error["code"] == "auth_missing"
    assert harness.server.requests == []


# --------------------------------------------------------------------------- transport


def _client(server: FakeServer) -> ServiceClient:
    return ServiceClient(
        "jira", JIRA, Credential(auth="pat", secret=JIRA_TOKEN), transport=server.transport()
    )


def test_redirects_are_refused(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/search"),
        status=302,
        headers={"location": "https://evil.example.test/login?token=abc"},
    )
    result = harness.run("jira", "search", "--jql", "project = EXAMPLE")
    assert result.code == 6
    assert result.error["code"] == "redirect_refused"
    assert result.error["details"]["redirect_host"] == "evil.example.test"
    assert "token=abc" not in result.stdout
    assert len(harness.server.requests) == 1


def test_tls_failure_maps_to_tls_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as exc:
            raise httpx.ConnectError("handshake failed", request=request) from exc

    server = FakeServer().add("GET", jira_path("/rest/api/2/myself"), handler=fail)
    with pytest.raises(YojoError) as info:
        _client(server).get("/rest/api/2/myself")
    assert info.value.code == "tls_error"
    assert "ca_bundle" in info.value.fix
    assert "never disabled" in info.value.fix


def test_connect_error_is_transport_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server = FakeServer().add("POST", jira_path("/rest/api/2/issue"), handler=fail)
    with pytest.raises(YojoError) as info:
        _client(server).request("POST", "/rest/api/2/issue", json_body={})
    assert info.value.code == "transport_error"
    assert info.value.write_outcome == "not_sent"


@pytest.mark.parametrize(
    "exc_type", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.WriteError]
)
def test_write_disconnect_is_outcome_unknown_and_not_retried(
    exc_type: type[httpx.HTTPError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise exc_type("boom", request=request)  # type: ignore[call-arg]

    server = FakeServer().add("POST", jira_path("/rest/api/2/issue"), handler=fail)
    with pytest.raises(YojoError) as info:
        _client(server).request("POST", "/rest/api/2/issue", json_body={})
    assert info.value.code == "outcome_unknown"
    assert info.value.exit_code == 6
    assert "before retrying" in info.value.fix
    assert len(server.requests) == 1


def test_read_timeout_on_get_is_transport_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    server = FakeServer().add("GET", jira_path("/rest/api/2/myself"), handler=fail)
    with pytest.raises(YojoError) as info:
        _client(server).get("/rest/api/2/myself")
    assert info.value.code == "transport_error"


def test_server_errors() -> None:
    server = FakeServer()
    server.add("GET", jira_path("/rest/api/2/myself"), {"message": "oops"}, status=503)
    server.add("POST", jira_path("/rest/api/2/issue"), {"message": "oops"}, status=500)
    client = _client(server)
    with pytest.raises(YojoError) as info:
        client.get("/rest/api/2/myself")
    assert info.value.code == "service_error"
    with pytest.raises(YojoError) as info:
        client.request("POST", "/rest/api/2/issue", json_body={})
    assert info.value.code == "outcome_unknown"


@pytest.mark.parametrize("status", [408, 499])
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_uncertain_write_status_is_outcome_unknown(status: int, method: str) -> None:
    server = FakeServer().add(
        method, jira_path("/rest/api/2/issue"), {"message": "x"}, status=status
    )
    with pytest.raises(YojoError) as info:
        _client(server).request(method, "/rest/api/2/issue", json_body={})
    assert info.value.code == "outcome_unknown"
    assert info.value.exit_code == 6
    assert info.value.write_outcome == "unknown"
    assert info.value.details["reason"] == f"HTTP {status}"
    assert "before retrying" in info.value.fix
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [408, 499])
def test_uncertain_read_status_is_service_error(status: int) -> None:
    server = FakeServer().add("GET", jira_path("/rest/api/2/myself"), {"m": "x"}, status=status)
    with pytest.raises(YojoError) as info:
        _client(server).get("/rest/api/2/myself")
    assert info.value.code == "service_error"
    assert info.value.exit_code == 6


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "remote_validation"),
        (409, "conflict"),
        (413, "remote_limit_exceeded"),
        (429, "rate_limited"),
    ],
)
def test_definite_rejections_stay_rejected(status: int, code: str) -> None:
    server = FakeServer().add("POST", jira_path("/rest/api/2/issue"), {"m": "x"}, status=status)
    with pytest.raises(YojoError) as info:
        _client(server).request("POST", "/rest/api/2/issue", json_body={})
    assert info.value.code == code
    assert info.value.write_outcome == "rejected"


def test_field_errors_are_sanitized() -> None:
    body = {
        "errorMessages": ["bad\x1b[31m value"],
        "errors": {"customfield_10100": "x" * 2000, "summary": "required\r\n"},
    }
    server = FakeServer().add("POST", jira_path("/rest/api/2/issue"), body, status=400)
    with pytest.raises(YojoError) as info:
        _client(server).request("POST", "/rest/api/2/issue", json_body={})
    error = info.value
    assert error.code == "remote_validation"
    assert error.exit_code == 5
    assert error.write_outcome == "rejected"
    fields = error.details["fields"]
    assert len(fields["customfield_10100"]) == 500
    assert "\r" not in fields["summary"] and "\n" not in fields["summary"]
    assert "\x1b" not in error.details["messages"][0]


def test_sanitize_remote_errors_bounds_items() -> None:
    body = {"errors": {f"f{i}": "e" for i in range(100)}}
    assert len(sanitize_remote_errors(body)["fields"]) == 50


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "auth_failed"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (413, "remote_limit_exceeded"),
        (429, "rate_limited"),
        (422, "remote_validation"),
    ],
)
def test_status_mapping(status: int, code: str) -> None:
    server = FakeServer().add(
        "GET", jira_path("/rest/api/2/myself"), {"errorMessages": []}, status=status
    )
    with pytest.raises(YojoError) as info:
        _client(server).get("/rest/api/2/myself")
    assert info.value.code == code
    assert JIRA_TOKEN not in json.dumps(info.value.envelope())


def test_non_json_response() -> None:
    server = FakeServer().add(
        "GET",
        jira_path("/rest/api/2/myself"),
        handler=lambda r: httpx.Response(200, text="<html>login</html>"),
    )
    with pytest.raises(YojoError) as info:
        _client(server).get("/rest/api/2/myself")
    assert info.value.code == "response_invalid"


def _oversized(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b" " * 2048)


def test_oversized_write_response_is_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 1024)
    server = FakeServer().add("POST", jira_path("/rest/api/2/issue"), handler=_oversized)
    with pytest.raises(YojoError) as info:
        _client(server).request("POST", "/rest/api/2/issue", json_body={})
    assert info.value.code == "outcome_unknown"
    assert info.value.exit_code == 6
    assert info.value.write_outcome == "unknown"
    assert "before retrying" in info.value.fix
    assert "Narrow" not in info.value.fix
    assert len(server.requests) == 1


def test_oversized_read_response_is_response_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 1024)
    server = FakeServer().add("GET", MYSELF, handler=_oversized)
    with pytest.raises(YojoError) as info:
        _client(server).get("/rest/api/2/myself")
    assert info.value.code == "response_invalid"
    assert info.value.write_outcome is None
    assert "Narrow" in info.value.fix


def test_ssl_context_is_verified() -> None:
    context = build_ssl_context(None)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_invalid_ca_bundle(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate")
    with pytest.raises(YojoError) as info:
        build_ssl_context(bad)
    assert info.value.code == "config_invalid"


def test_real_client_does_not_follow_redirects() -> None:
    client = ServiceClient("jira", JIRA, None)
    try:
        assert client._client.follow_redirects is False
    finally:
        client.close()


@pytest.mark.parametrize("auth", ["pat", "basic"])
@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_reflected_credentials_are_redacted(auth: str, status: int) -> None:
    username = "svc-user" if auth == "basic" else None
    credential = Credential(auth=auth, secret=JIRA_TOKEN, username=username)
    header = credential.authorization_header()
    encoded = header.partition(" ")[2]
    body = {
        "errorMessages": [f"Bad header: Authorization: {header}", f"token={JIRA_TOKEN}"],
        "errors": {f"x-{encoded}": f"echo {encoded}", "summary": {"seen": JIRA_TOKEN}},
        "message": f"credential {encoded}",
    }
    server = FakeServer().add(
        "POST",
        jira_path("/rest/api/2/issue"),
        body,
        status=status,
        headers={"x-seraph-loginreason": f"AUTHENTICATED_FAILED {JIRA_TOKEN}"},
    )
    client = ServiceClient("jira", JIRA, credential, transport=server.transport())
    with pytest.raises(YojoError) as info:
        client.request("POST", "/rest/api/2/issue", json_body={})
    rendered = json.dumps(info.value.envelope())
    forms = [JIRA_TOKEN, encoded]
    if auth == "basic":
        forms.append(base64.b64decode(encoded).decode())
    for secret in forms:
        assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert sorted(info.value.details["fields"]) == ["summary", "x-[REDACTED]"]


def test_reflected_credential_is_redacted_through_cli(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/search"),
        {"errorMessages": [f"Unexpected token Bearer {JIRA_TOKEN}"]},
        status=400,
    )
    result = harness.run("jira", "search", "--jql", "project = EXAMPLE")
    assert result.code == 5
    assert JIRA_TOKEN not in result.stdout + result.stderr
    assert result.error["details"]["messages"] == ["Unexpected token [REDACTED]"]
