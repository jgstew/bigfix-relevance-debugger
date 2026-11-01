"""Tests for bridge.py: the versioned JSON API the iOS app calls.

Offline: session relevance and BigFix SaaS answer from ``httpx.MockTransport``
fakes through the bridge's ``transport_factory``, and the trust test runs a
self-signed TLS server on 127.0.0.1 (the session library applies trust only
to its own TLS, never to an injected transport).
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from bigfix_remote_session_relevance import MemorySecretStore

from bigfix_relevance_debugger import bridge
from bigfix_relevance_debugger.bridge import BRIDGE_VERSION, METHODS, Bridge

SESSION_FIXTURES = Path(__file__).parent / "fixtures" / "session_rest"
DEPLOYMENT_QUERY = "sha256 of gather url of bes license"


def rest_body(name: str) -> Any:
    return json.loads((SESSION_FIXTURES / f"{name}.json").read_text())


SUBMITTED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<BESAPI><ClientQuery Resource="https://x/api/clientquery/7"><ID>7</ID></ClientQuery></BESAPI>'
)
ANSWERED = {
    "results": [{"computerID": 11, "computerName": "web", "isFailure": False, "result": "Linux"}]
}


def deployment(name: str) -> dict[str, Any]:
    return {**rest_body("deployment_id"), "result": [f"dep-{name}"]}


class Servers:
    """Fake BigFix servers behind ``httpx.MockTransport``, by connection name.

    ``answers[name]`` is a response body, or an async callable taking the
    relevance. Records each (connection, path, body) it was sent.
    """

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.sent: list[tuple[str, str, Any]] = []
        self.saas_logins: list[dict[str, Any]] = []
        self.auth: list[tuple[str, str | None]] = []

    def transport(self, conn) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            self.auth.append((conn.name, request.headers.get("authorization")))
            if request.url.path == "/api/clientquery":
                self.sent.append((conn.name, request.url.path, request.content.decode()))
                return httpx.Response(200, text=SUBMITTED)
            if request.url.path == "/api/clientqueryresults/7":
                return httpx.Response(200, json=ANSWERED)
            body: Any = json.loads(request.content) if request.content else None
            self.sent.append((conn.name, request.url.path, body))
            if request.url.path == "/auth/v1/user/login":
                return self._saas_login(body["data"])
            relevance = body["relevance"]
            if relevance == DEPLOYMENT_QUERY:
                # One deployment per fake server, so each gets its own result.
                return httpx.Response(200, json=deployment(conn.name))
            answer: Any = self.answers.get(conn.name, rest_body("plural_tuples"))
            if callable(answer):
                answer = await self.answers[conn.name](relevance)
            return httpx.Response(200, json=answer)

        return httpx.MockTransport(handle)

    def _saas_login(self, data: dict[str, Any]) -> httpx.Response:
        """An MFA account: challenges without a code, accepts "123456"."""
        self.saas_logins.append(data)
        if data.get("totp") != "123456":
            return httpx.Response(401, json={"data": {"errorMessage": "totp_required"}})
        return httpx.Response(
            200,
            json={
                "status": 200,
                "data": {"iat": 0, "exp": 3600, "pids": {"bf": {"1": {}, "2": {}}}},
            },
            headers=[
                ("set-cookie", "accessToken=ACCESS; Path=/; HttpOnly; Secure"),
                ("set-cookie", "refreshToken=REFRESH; Path=/; HttpOnly; Secure"),
            ],
        )


@pytest.fixture
def servers() -> Servers:
    return Servers()


@pytest.fixture
def make_bridge(tmp_path, servers):
    made: list[Bridge] = []

    def make(**kwargs: Any) -> Bridge:
        kwargs.setdefault("data_dir", tmp_path / "app")
        kwargs.setdefault("secrets", MemorySecretStore())
        kwargs.setdefault("transport_factory", servers.transport)
        kwargs.setdefault("selfcheck_network", False)
        made.append(Bridge(**kwargs))
        return made[-1]

    yield make
    for b in made:
        b.close()


@pytest.fixture
def b(make_bridge) -> Bridge:
    return make_bridge()


def call(target: Bridge, method: str, **args: Any) -> dict[str, Any]:
    reply = json.loads(target.call(method, json.dumps(args)))
    assert reply["version"] == BRIDGE_VERSION
    return reply


def ok(target: Bridge, method: str, **args: Any) -> Any:
    reply = call(target, method, **args)
    assert reply["ok"] is True, reply
    return reply["result"]


def failed(target: Bridge, method: str, **args: Any) -> dict[str, str]:
    reply = call(target, method, **args)
    assert reply["ok"] is False, reply
    assert set(reply["error"]) == {"type", "message"}
    return reply["error"]


def rest(name: str, server: str = "https://bf.example.com:52311", **fields: Any) -> dict:
    return {"name": name, "backend": "rest", "server": server, "username": "api", **fields}


def save(target: Bridge, connection: dict, password: str | None = "pw") -> None:
    args: dict[str, Any] = {"connection": connection}
    if password is not None:
        args["password"] = password
    ok(target, "save_connection", **args)


def drain(target: Bridge, run_id: str, *, limit_s: float = 10.0) -> list[dict[str, Any]]:
    """Every poll until done; the polls themselves, so tests can see the streaming."""
    polls = []
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        polls.append(ok(target, "poll_run", run_id=run_id, wait_ms=2000))
        if polls[-1]["done"]:
            return polls
    raise AssertionError(f"run {run_id} never finished: {polls}")


def rows(polls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for p in polls for row in p["rows"]]


# --- the envelope -------------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(METHODS))
def test_every_method_answers_in_the_envelope_even_with_no_args(b, method):
    reply = json.loads(b.call(method, "{}"))

    assert reply["version"] == BRIDGE_VERSION
    assert isinstance(reply["ok"], bool)
    assert ("result" in reply) if reply["ok"] else set(reply["error"]) == {"type", "message"}


@pytest.mark.parametrize("args_json", ["{not json", "[]", '"text"', "null", "3"])
def test_bad_json_is_an_error_not_an_exception(b, args_json):
    reply = json.loads(b.call("analyze", args_json))

    assert reply["ok"] is False
    assert reply["error"]["type"] == "bad_request"


def test_an_unknown_method_is_an_error(b):
    assert failed(b, "rm_rf")["type"] == "unknown_method"


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("analyze", {"text": 3}),
        ("analyze", {"text": "x", "mode": "sideways"}),
        ("poll_run", {"run_id": "nope"}),
        ("poll_run", {"run_id": "x", "wait_ms": "soon"}),
        ("cancel_run", {"run_id": "nope"}),
        ("start_run", {"text": "now", "dialects": ["quantum"]}),
        ("start_run", {"text": "now", "timeout_s": -1}),
        ("save_connection", {"connection": {"name": "x"}}),
        ("save_connection", {"connection": rest("x"), "password": 7}),
        ("delete_connection", {"name": "missing"}),
        ("trust_cert", {"host_port": "nobody.example:443", "sha256": "0" * 64}),
        ("trust_cert", {"host_port": "nobody.example:443"}),
        ("poll_run", {"run_id": "x", "wait_ms": float("nan")}),
        ("start_run", {"text": "now", "timeout_s": float("inf")}),
        ("save_connection", {"connection": rest("x", "ftp://x")}),
        ("saas_sign_in", {"host": "login.bigfix.com"}),
    ],
)
def test_bad_args_are_errors(b, method, args):
    error = failed(b, method, **args)

    assert error["message"]


def test_an_unexpected_exception_is_an_error_too(b, monkeypatch):
    def boom(text, mode):
        raise RuntimeError("analyzer exploded")

    monkeypatch.setattr(bridge, "summarize", boom)

    assert failed(b, "analyze", text="x") == {
        "type": "RuntimeError",
        "message": "analyzer exploded",
    }


def test_module_call_never_raises_and_builds_nothing_for_bad_input(monkeypatch):
    def refuse(**kwargs):
        raise AssertionError("a bad request built the bridge")

    monkeypatch.setattr(bridge, "Bridge", refuse)

    assert json.loads(bridge.call("analyze", "{nope"))["error"]["type"] == "bad_request"
    assert json.loads(bridge.call("nope", "{}"))["error"]["type"] == "unknown_method"
    assert json.loads(bridge.call(None, "{}"))["ok"] is False  # type: ignore[arg-type]


def test_module_call_uses_one_shared_bridge(monkeypatch, make_bridge):
    shared = make_bridge()
    monkeypatch.setattr(bridge, "_default", shared)

    assert json.loads(bridge.call("info", "{}"))["result"]["data_dir"] == str(shared.data_dir)


@pytest.fixture
def default_bridge(tmp_path, monkeypatch):
    """Build the process's bridge for real, in ``tmp_path``; the test fakes its secret store."""
    monkeypatch.setattr(bridge, "_default", None)
    monkeypatch.setattr(bridge, "app_data_dir", lambda: tmp_path / "app")
    yield
    if bridge._default is not None:
        bridge._default.close()


class OsStore(MemorySecretStore):
    """Stands in for the Keychain."""

    name = "keychain"


def test_the_process_bridge_keeps_secrets_in_the_os_store(monkeypatch, default_bridge):
    """The library's ``default_secret_store()``: the Keychain on iOS and macOS."""
    os_store = OsStore()
    monkeypatch.setattr(bridge, "default_secret_store", lambda: os_store)

    secrets = bridge._bridge().secrets
    secrets.set("a", "pw")

    assert secrets.name == "keychain"
    assert os_store.get("a") == "pw"
    assert ok(bridge._bridge(), "info")["secrets"] == "keychain"


def test_the_process_bridge_keeps_secrets_in_memory_without_an_os_store(
    monkeypatch, default_bridge
):
    """Android, or a desktop with no usable keyring: the library finds no store."""
    monkeypatch.setattr(bridge, "default_secret_store", lambda: None)

    secrets = bridge._bridge().secrets
    secrets.set("a", "pw")

    assert secrets.name == "memory"
    assert secrets.get("a") == "pw"


def test_the_process_bridge_picks_up_an_os_store_that_appears_later(monkeypatch, default_bridge):
    """A keychain locked at launch: the library finds none, then finds it on a later
    call. New secrets go to it; one kept in memory meanwhile is still found."""
    found: list[OsStore] = []
    monkeypatch.setattr(bridge, "default_secret_store", lambda: found[0] if found else None)
    secrets = bridge._bridge().secrets
    secrets.set("early", "pw")

    os_store = OsStore()
    found.append(os_store)
    secrets.set("late", "pw2")

    assert secrets.name == "keychain"
    assert os_store.get("late") == "pw2"
    assert os_store.get("early") is None
    assert secrets.get("early") == "pw"
    secrets.delete("early")
    assert secrets.get("early") is None


# --- info, analyze --------------------------------------------------------------------


def test_info(b):
    info = ok(b, "info")

    assert info["bridge_version"] == BRIDGE_VERSION
    assert set(info["versions"]) == {
        "bigfix-relevance-debugger",
        "bigfix-relevance-analyzer",
        "bigfix-remote-client-relevance",
        "bigfix-remote-session-relevance",
    }
    assert info["secrets"] == "memory"
    assert info["python"] and info["platform"]


def test_analyze_gives_the_analysis_and_highlight_spans(b):
    result = ok(b, "analyze", text="names of bes computers")

    assert result["run_dialects"] == ["session"]
    assert result["parse_error"] is None
    assert result["type_summary"]
    spans = result["highlight"]
    assert spans and all(set(s) == {"start", "end", "kind"} for s in spans)
    assert {"keyword", "property"} <= {s["kind"] for s in spans}
    assert [s["start"] for s in spans] == sorted(s["start"] for s in spans)


def test_analyze_takes_the_dialect_picker(b):
    result = ok(b, "analyze", text="names of bes computers", mode="client")

    assert result["run_dialects"] == ["client"]


def test_analyze_reports_a_parse_error_as_a_result(b):
    result = ok(b, "analyze", text="names of (")

    assert result["parse_error"]["message"]


# --- connections ------------------------------------------------------------------------


def test_connections_round_trip_and_the_file_has_no_secret(b):
    save(b, rest("root", computer_ids=[1, 2]), password="sekrit-pw")
    save(b, {"name": "cloud", "backend": "saas", "server": "login.bigfix.com"}, password=None)

    result = ok(b, "connections")

    assert [c["name"] for c in result["connections"]] == ["root", "cloud"]
    assert result["connections"][0]["computer_ids"] == [1, 2]
    assert result["has_secret"] == {"root": True, "cloud": False}
    assert "saas" in result["backends"]
    assert "sekrit-pw" not in (b.data_dir / "connections.json").read_text(encoding="utf-8")


def test_delete_connection(b):
    save(b, rest("root"))

    ok(b, "delete_connection", name="root")

    assert ok(b, "connections")["connections"] == []


def test_a_password_inside_the_connection_is_refused(b):
    error = failed(b, "save_connection", connection={**rest("x"), "password": "pw"})

    assert "password" in error["message"]


# --- runs -------------------------------------------------------------------------------


def test_a_run_streams_session_rows_and_tallies_them(b, servers):
    async def slow(relevance):
        await asyncio.sleep(0.3)
        return rest_body("plural_tuples")

    servers.answers["slow"] = slow
    save(b, rest("fast"))
    save(b, rest("slow", "https://lab.example.com:52311"))

    run_id = ok(b, "start_run", text='(1, "a")', dialects=["session"])["run_id"]
    polls = drain(b, run_id)

    got = rows(polls)
    assert [r["display_host"] for r in got] == ["fast", "slow"]  # completion order
    assert all(r["dialect"] == "session" and r["ok"] for r in got)
    # Streamed: the fast deployment's row came back before the run was done.
    first = next(p for p in polls if p["rows"])
    assert not first["done"]
    tally = polls[-1]["tally"]
    assert tally["systems"] == 2
    assert tally["rows"] and set(tally["rows"][0]) == {"key", "kind", "label", "systems"}
    assert polls[-1]["pending_certs"] == []
    assert polls[-1]["totp_needed"] is False


def test_rows_are_sent_once(b):
    save(b, rest("root"))
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    polls = drain(b, run_id)

    assert len(rows(polls)) == 1


def test_a_finished_run_is_forgotten_once_polled(b):
    save(b, rest("root"))
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    drain(b, run_id)

    assert failed(b, "poll_run", run_id=run_id)["type"] == "unknown_run"


def test_no_connection_gives_the_config_row(b):
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]

    [row] = rows(drain(b, run_id))

    assert row["status"] == "config" and not row["ok"]
    assert "No BigFix server" in row["error"]


def test_dialects_default_to_what_the_analysis_says(b, servers):
    save(b, rest("root"))

    run_id = ok(b, "start_run", text="names of bes computers")["run_id"]

    assert [r["dialect"] for r in rows(drain(b, run_id))] == ["session"]


def test_client_relevance_with_no_fast_query_computers_says_so(b):
    run_id = ok(b, "start_run", text="name of operating system", dialects=["client"])["run_id"]

    [row] = rows(drain(b, run_id))

    assert row["dialect"] == "client" and row["status"] == "config"
    assert "Fast Query" in row["error"]


def test_cancel_run_stops_a_run(b, servers):
    started = threading.Event()

    async def hang(relevance):
        started.set()
        await asyncio.sleep(30)

    servers.answers["root"] = hang
    save(b, rest("root"))
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    assert started.wait(5)

    t0 = time.monotonic()
    ok(b, "cancel_run", run_id=run_id)
    last = ok(b, "poll_run", run_id=run_id, wait_ms=5000)

    assert last["done"] and last["cancelled"]
    assert time.monotonic() - t0 < 3


def test_poll_waits_at_most_wait_ms(b, servers):
    async def hang(relevance):
        await asyncio.sleep(30)

    servers.answers["root"] = hang
    save(b, rest("root"))
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]

    t0 = time.monotonic()
    result = ok(b, "poll_run", run_id=run_id, wait_ms=200)

    assert result["rows"] == [] and not result["done"]
    assert 0.15 < time.monotonic() - t0 < 2
    ok(b, "cancel_run", run_id=run_id)


def test_saving_a_connection_reaches_the_next_run(b):
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    assert [r["status"] for r in rows(drain(b, run_id))] == ["config"]

    save(b, rest("root"))
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]

    assert [r["status"] for r in rows(drain(b, run_id))] == ["ok"]


def test_the_password_comes_from_the_secret_store(b, servers):
    save(b, rest("root"), password="from-the-store")

    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    drain(b, run_id)

    expected = httpx.BasicAuth("api", "from-the-store")._auth_header
    assert servers.auth and all(header == expected for _, header in servers.auth)


@pytest.fixture
def fast_polling(monkeypatch):
    from bigfix_remote_client_relevance.transports import fastquery

    # The first results check would otherwise wait POLL_FIRST_S (2 s).
    monkeypatch.setattr(fastquery, "POLL_FIRST_S", 0.0)


def test_client_relevance_runs_through_fast_query(b, servers, fast_polling):
    save(b, rest("root", computer_ids=[11]), password="fq-pw")

    run_id = ok(b, "start_run", text="name of operating system", dialects=["client"])["run_id"]
    polls = drain(b, run_id)

    [row] = rows(polls)
    assert row["dialect"] == "client" and row["ok"]
    assert (row["host"], row["answers"]) == ("web", ["Linux"])
    assert ("root", "/api/clientquery") in [(n, path) for n, path, _ in servers.sent]
    expected = httpx.BasicAuth("api", "fq-pw")._auth_header
    assert all(header == expected for _, header in servers.auth)
    assert polls[-1]["tally"]["systems"] == 1


def test_both_dialects_in_one_run(b, servers, fast_polling):
    save(b, rest("root", computer_ids=[11]))

    run_id = ok(b, "start_run", text="now", dialects=["session", "client"])["run_id"]

    assert sorted(r["dialect"] for r in rows(drain(b, run_id))) == ["client", "session"]


# --- SaaS: TOTP and sign-in -----------------------------------------------------------------

SAAS = {"name": "cloud", "backend": "saas", "server": "login.bigfix.com", "username": "me@x.com"}


def test_a_saas_account_wanting_a_totp_code_says_so(b, servers):
    save(b, SAAS)

    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    polls = drain(b, run_id)

    assert polls[-1]["totp_needed"] is True
    assert [r["error_code"] for r in rows(polls)] == ["totp_required"]
    assert all("totp" not in login for login in servers.saas_logins)


def test_a_run_with_a_totp_code_answers_the_challenge(b, servers):
    save(b, SAAS)
    first = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    drain(b, first)

    run_id = ok(b, "start_run", text="now", dialects=["session"], totp="123456")["run_id"]
    polls = drain(b, run_id)

    assert servers.saas_logins[-1]["totp"] == "123456"
    assert polls[-1]["totp_needed"] is False


def test_saas_sign_in_lists_the_servers(b, servers):
    challenged = ok(b, "saas_sign_in", host="login.bigfix.com", email="me@x.com", password="pw")
    assert challenged["error"]["code"] == "totp_required"
    assert challenged["servers"] == []

    signed_in = ok(
        b, "saas_sign_in", host="login.bigfix.com", email="me@x.com", password="pw", totp="123456"
    )

    assert signed_in["error"] is None
    assert signed_in["servers"] == ["bf1", "bf2"]
    assert [c["sid"] for c in signed_in["connections"]] == ["bf1", "bf2"]
    assert all("password" not in c for c in signed_in["connections"])


# --- trust ---------------------------------------------------------------------------------


def _self_signed(directory: Path) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_root_server(tmp_path) -> Iterator[str]:
    """A REST root server on 127.0.0.1 with a self-signed certificate; yields host:port."""
    cert, key = _self_signed(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            name = "deployment_id" if body["relevance"] == DEPLOYMENT_QUERY else "plural_tuples"
            data = json.dumps(rest_body(name)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address) -> None:
            pass  # refused handshakes are expected

    httpd = Server(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(
        httpd.socket, server_side=True, do_handshake_on_connect=False
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_first_use_certificate_is_pending_until_trusted(make_bridge, tls_root_server):
    """Deferred trust: refuse and record, show the fingerprint, trust_cert, run again."""
    b = make_bridge(transport_factory=None)  # real TLS: trust applies only to it
    save(b, rest("root", f"https://{tls_root_server}"))

    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    polls = drain(b, run_id)

    [refused] = rows(polls)
    assert refused["status"] == "tls"
    [pending] = polls[-1]["pending_certs"]
    assert pending["host_port"] == tls_root_server
    assert len(pending["sha256"]) == 64 and ":" in pending["fingerprint"]

    error = failed(b, "trust_cert", host_port=tls_root_server, sha256="0" * 64)
    assert error["type"] == "changed"  # not the certificate the user was shown
    ok(b, "trust_cert", host_port=tls_root_server, sha256=pending["sha256"])
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    polls = drain(b, run_id)

    assert [r["status"] for r in rows(polls)] == ["ok"]
    assert polls[-1]["pending_certs"] == []
    assert tls_root_server in (b.data_dir / "known_certs.toml").read_text(encoding="utf-8")


# --- selfcheck ---------------------------------------------------------------------------------


def test_selfcheck_passes_offline(b):
    result = ok(b, "selfcheck")

    lines = result["lines"]
    assert not [line for line in lines if line.startswith("FAIL")], lines
    assert any(line.startswith("SKIP tls") for line in lines)
    assert result["passed"] == result["total"] - result["skipped"]


def test_selfcheck_reports_a_failure_as_a_line(make_bridge):
    class Broken(MemorySecretStore):
        def get(self, account: str) -> str | None:
            raise OSError("keychain locked")

    result = ok(make_bridge(secrets=Broken()), "selfcheck")

    assert any(
        line.startswith("FAIL secrets") and "keychain locked" in line for line in result["lines"]
    )


# --- the loop --------------------------------------------------------------------------------


def test_close_stops_the_loop_thread(make_bridge):
    b = make_bridge()
    ok(b, "start_run", text="now", dialects=["session"])
    thread = b._thread
    assert thread is not None and thread.is_alive()

    b.close()

    assert not thread.is_alive()


# --- review fixes -------------------------------------------------------------------------


def test_a_broken_connections_file_ends_the_run_with_an_error(b):
    b.data_dir.mkdir(parents=True, exist_ok=True)
    (b.data_dir / "connections.json").write_text("{not json", encoding="utf-8")

    run_id = ok(b, "start_run", text="name of operating system", dialects=["client"])["run_id"]
    last = drain(b, run_id, limit_s=5)[-1]

    assert last["done"] and "connections.json" in last["error"]


def test_one_failing_half_does_not_stop_the_other(b, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("fast query exploded")
        yield  # an async generator

    monkeypatch.setattr(bridge, "stream_results", broken)
    save(b, rest("root", computer_ids=[11]))

    run_id = ok(b, "start_run", text="now", dialects=["session", "client"])["run_id"]
    polls = drain(b, run_id)

    assert [r["dialect"] for r in rows(polls)] == ["session"]
    assert "fast query exploded" in polls[-1]["error"]


def test_a_fast_query_totp_challenge_says_totp_needed(b, monkeypatch):
    from bigfix_remote_client_relevance import ClientRelevanceResult

    async def challenged(text, targets, opts, **kwargs):
        yield ClientRelevanceResult(
            host="web",
            transport="fastquery",
            client_relevance=text,
            error="authentication: this account requires a TOTP code (MFA)",
            error_kind="transport",
        )

    monkeypatch.setattr(bridge, "stream_results", challenged)
    save(b, {**SAAS, "computer_ids": [11]})

    run_id = ok(b, "start_run", text="name of operating system", dialects=["client"])["run_id"]

    assert drain(b, run_id)[-1]["totp_needed"] is True


def test_a_run_with_a_code_gives_fast_query_a_fresh_saas_pool(b, monkeypatch):
    """A remembered totp_required in the old pool would refuse the code unsent."""
    pools = []

    async def record(text, targets, opts, *, session, **kwargs):
        pools.append(session.saas_sessions)
        return
        yield

    monkeypatch.setattr(bridge, "stream_results", record)
    save(b, {**SAAS, "computer_ids": [11]})

    for totp in (None, None, "123456"):
        args = {"text": "x", "dialects": ["client"]} | ({"totp": totp} if totp else {})
        drain(b, ok(b, "start_run", **args)["run_id"])

    assert pools[0] is pools[1] and pools[2] is not pools[1]


def test_the_no_server_row_speaks_to_the_app_not_the_cli(b):
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]

    [row] = rows(drain(b, run_id))

    assert "--setup" not in row["error"] and "besapi" not in row["error"]
    assert "connection" in row["error"].lower()


def test_two_polls_racing_a_finishing_run_both_answer(b, servers):
    release = threading.Event()

    async def held(relevance):
        while not release.is_set():
            await asyncio.sleep(0.01)
        return rest_body("plural_tuples")

    servers.answers["root"] = held
    save(b, rest("root"))
    run_id = ok(b, "start_run", text="now", dialects=["session"])["run_id"]
    replies: list[dict] = []
    threads = [
        threading.Thread(
            target=lambda: replies.append(call(b, "poll_run", run_id=run_id, wait_ms=5000))
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(10)

    assert len(replies) == 2
    assert all(r["ok"] or r["error"]["type"] == "unknown_run" for r in replies), replies
