"""Offline tests for the shared core. No real BigFix server is ever contacted."""

from __future__ import annotations

import json
import sys
import uuid

import httpx
import pytest
from bfrd_mobile_core import analysis
from bfrd_mobile_core.connections import Connection, ConnectionStore
from bfrd_mobile_core.evaluate import Evaluator, parse_computers
from bfrd_mobile_core.secrets import KeychainSecretStore, MemorySecretStore


@pytest.fixture(autouse=True)
def _app_data(tmp_path, monkeypatch):
    # Not HOME: on macOS a HOME without a login keychain makes Security.framework
    # wait on a GUI dialog. Everything the core writes goes under BFRD_APP_DATA.
    monkeypatch.setenv("BFRD_APP_DATA", str(tmp_path))


# -- analysis ----------------------------------------------------------------


def test_client_relevance_runs_as_client():
    a = analysis.analyze("name of operating system")
    assert a.dialects == ("client",)
    assert a.type_summary == "singular string"


def test_session_relevance_runs_as_session():
    a = analysis.analyze("names of bes computers")
    assert a.dialects == ("session",)


def test_highlight_spans_cover_keywords_and_strings():
    text = 'names of bes computers whose (name of it contains "web")'
    kinds = {(text[s.start : s.end], s.kind) for s in analysis.analyze(text).spans}
    assert ("whose", "keyword") in kinds
    assert ('"web"', "string") in kinds
    assert ("bes", "keyword") not in kinds


def test_parse_error_and_typo_finding():
    a = analysis.analyze("name of operating sytem whose (it")
    assert a.parse_error and "close" in a.parse_error
    b = analysis.analyze("name of operating sytem")
    assert b.findings[0].code == "unknown-inspector"
    assert "operating system" in b.findings[0].suggestions
    assert any(s.kind == "warning" for s in b.spans)


def test_blank_text():
    assert analysis.analyze("  ").summary == "Type some relevance."


# -- connections / secrets ---------------------------------------------------


def test_parse_computers():
    assert parse_computers("12, 34") == ([12, 34], None)
    assert parse_computers('bes computers whose (name of it = "a")') == (
        [],
        'bes computers whose (name of it = "a")',
    )
    assert parse_computers("") == ([], None)


def test_connection_store_roundtrip_never_holds_secrets(tmp_path):
    store = ConnectionStore(tmp_path / "c.json")
    store.upsert(Connection("lab", backend="rest", server="bes.example", username="op"))
    assert store.load()[0].server == "bes.example"
    assert "password" not in (tmp_path / "c.json").read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="Keychain")
def test_keychain_roundtrip():
    store = KeychainSecretStore(service=f"bfrd-spike-test-{uuid.uuid4()}")
    try:
        assert store.get("a") is None
        store.set("a", "s3cret")
        assert store.get("a") == "s3cret"
        store.set("a", "changed")  # duplicate -> update
        assert store.get("a") == "changed"
    finally:
        store.delete("a")
    assert store.get("a") is None


# -- evaluation against a fake REST API ---------------------------------------


def _fake_rest(answer):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"result": [answer], "type": "integer", "plural": False, "evaltime_ms": 1.0},
        )

    return seen, (lambda _conn: httpx.MockTransport(handler))


async def test_session_rows_from_fake_rest(tmp_path):
    secrets = MemorySecretStore()
    conn = Connection("lab", backend="rest", server="bes.example", username="op")
    secrets.set(conn.secret_account, "pw")
    seen, factory = _fake_rest(42)
    ev = Evaluator(secrets, known_certs_path=str(tmp_path / "k.toml"), transport_factory=factory)
    rows = [r async for r in ev.session("number of bes computers", [conn])]
    assert rows[-1].ok and rows[-1].answers == ("42",)
    assert seen and seen[0].headers["authorization"].startswith("Basic ")


async def test_session_with_no_connection_says_how_to_add_one(tmp_path):
    ev = Evaluator(MemorySecretStore(), known_certs_path=str(tmp_path / "k.toml"))
    rows = [r async for r in ev.session("now", [])]
    assert rows[0].error_kind == "config"


async def test_fastquery_needs_targets(tmp_path):
    ev = Evaluator(MemorySecretStore(), known_certs_path=str(tmp_path / "k.toml"))
    rows = await ev.fastquery("name of operating system", Connection("saas1"))
    assert rows[0].error_kind == "config"
    rows = await ev.fastquery("now", Connection("wr", backend="webreports", computers="1"))
    assert "SaaS or REST" in rows[0].error


def test_totp_prompt_never_sends_an_empty_code(tmp_path):
    from bfrd_mobile_core.evaluate import TotpRequired

    ev = Evaluator(MemorySecretStore(), known_certs_path=str(tmp_path / "k.toml"))
    with pytest.raises(TotpRequired):
        ev._totp("TOTP code")
    ev.totp_code = "123456"
    assert ev._totp("TOTP code") == "123456"
    with pytest.raises(TotpRequired):  # one-shot
        ev._totp("TOTP code")


async def test_untrusted_cert_is_never_auto_accepted(tmp_path):
    from bigfix_remote_session_relevance import CertInfo

    ev = Evaluator(MemorySecretStore(), known_certs_path=str(tmp_path / "k.toml"))
    info = CertInfo("bes.example:52311", "ab" * 32, "PEM")
    assert await ev._on_untrusted(info) is False
    assert "bes.example:52311" in ev.pending_certs


# -- bridge --------------------------------------------------------------------


def test_bridge_is_json_in_json_out(monkeypatch):
    from bfrd_mobile_core import bridge

    monkeypatch.setattr(bridge, "default_secret_store", MemorySecretStore)
    bridge._state.clear()

    out = json.loads(bridge.analyze(json.dumps({"text": "names of bes computers"})))
    assert out["dialects"] == ["session"]
    assert "session" in out["summary"]
    err = json.loads(bridge.trust_cert(json.dumps({"host_port": "nope:1"})))
    assert "KeyError" in err["error"]
