"""Tests for the FastMCP server: bearer auth, input bounding, audit hook (M1.5).

Covers the M1.5 acceptance criteria:

- no token -> 401 on the MCP endpoint; wrong token -> 401 (HTTP auth layer,
  exercised via ``starlette.testclient.TestClient`` against ``app.http_app()``),
- bad ``subsystem`` / ``window_s`` / ``parameter`` -> a clear client error
  (pydantic enum rejection or ``ToolError``),
- ``execute_command`` always produces an ``AuditEntry`` (accept *and* reject),
  with the caller ``principal`` resolved from ``X-Agent-Id`` / token / fallback,
- ``MCP_AUTH_TOKEN`` is compared constant-time and never echoed.

Tool logic is driven through the in-memory ``fastmcp.Client`` (which calls the
real tool functions through the MCP protocol without binding a port). The HTTP
auth gate is tested separately because the in-memory transport bypasses HTTP
middleware. See ``tasks/M1-telemetry-mcp/05-fastmcp-server-auth.md``.
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

# ``starlette.testclient`` emits a StarletteDeprecationWarning at import time
# (it wants ``httpx2``); the upgrade is a test-infra concern, not relevant to
# the server under test, so silence it at import.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from telemetry_mcp.models import Decision, Subsystem
from telemetry_mcp.safety import AuditLog
from telemetry_mcp.server import (
    DEFAULT_PRINCIPAL,
    MAX_WINDOW_S,
    SharedSecretBearerAuth,
    _load_token,
    create_app,
    resolve_principal,
)
from telemetry_mcp.simulator import AcceleratorSimulator

_GOOD_TOKEN = "x" * 32  # meets the 16-char minimum


def _make_app(
    *, token: str = _GOOD_TOKEN, seed: int = 0
) -> tuple[object, AcceleratorSimulator, AuditLog]:
    """Build a (server, simulator, audit_log) triple for one test."""
    sim = AcceleratorSimulator(seed=seed)
    log = AuditLog()
    app = create_app(token=token, simulator=sim, audit_log=log)
    return app, sim, log


# --------------------------------------------------------------------------- #
# SharedSecretBearerAuth
# --------------------------------------------------------------------------- #


class TestSharedSecretBearerAuth:
    @pytest.mark.asyncio
    async def test_valid_token_returns_access_token(self) -> None:
        auth = SharedSecretBearerAuth(_GOOD_TOKEN)
        result = await auth.verify_token(_GOOD_TOKEN)
        assert result is not None
        assert result.client_id == DEFAULT_PRINCIPAL
        assert result.scopes == []

    @pytest.mark.asyncio
    async def test_wrong_token_returns_none(self) -> None:
        auth = SharedSecretBearerAuth(_GOOD_TOKEN)
        assert await auth.verify_token("wrong-token") is None

    @pytest.mark.asyncio
    async def test_empty_token_returns_none(self) -> None:
        auth = SharedSecretBearerAuth(_GOOD_TOKEN)
        assert await auth.verify_token("") is None

    @pytest.mark.asyncio
    async def test_malformed_token_does_not_raise(self) -> None:
        auth = SharedSecretBearerAuth(_GOOD_TOKEN)
        # ``compare_digest`` rejects non-str/bytes; verify_token swallows it.
        assert await auth.verify_token(None) is None  # type: ignore[arg-type]

    def test_empty_expected_token_rejected(self) -> None:
        with pytest.raises(ValueError):
            SharedSecretBearerAuth("")

    @pytest.mark.asyncio
    async def test_token_never_in_access_token_client_id(self) -> None:
        # The secret must not leak into the auditable identity field.
        auth = SharedSecretBearerAuth(_GOOD_TOKEN)
        result = await auth.verify_token(_GOOD_TOKEN)
        assert result is not None
        assert _GOOD_TOKEN not in result.client_id


# --------------------------------------------------------------------------- #
# resolve_principal
# --------------------------------------------------------------------------- #


class _FakeToken:
    """Minimal stand-in for ``AccessToken`` for principal-resolution tests."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id


class TestResolvePrincipal:
    def test_x_agent_id_wins(self) -> None:
        assert resolve_principal({"x-agent-id": "agent-7"}, None) == "agent-7"

    def test_x_agent_id_case_insensitive_header(self) -> None:
        # ``get_http_headers`` lowercases names; the lookup is on the lowercased key.
        assert resolve_principal({"x-agent-id": "Agent-9"}, None) == "Agent-9"

    def test_blank_x_agent_id_falls_back_to_token(self) -> None:
        assert (
            resolve_principal({"x-agent-id": "   "}, _FakeToken("ops-1")) == "ops-1"
        )

    def test_unknown_x_agent_id_falls_back_to_token(self) -> None:
        assert (
            resolve_principal({"x-agent-id": "unknown"}, _FakeToken("ops-2"))
            == "ops-2"
        )

    def test_no_header_no_token_uses_default(self) -> None:
        assert resolve_principal({}, None) == DEFAULT_PRINCIPAL

    def test_blank_token_client_id_uses_default(self) -> None:
        assert resolve_principal({}, _FakeToken("  ")) == DEFAULT_PRINCIPAL

    def test_unknown_token_client_id_uses_default(self) -> None:
        assert resolve_principal({}, _FakeToken("unknown")) == DEFAULT_PRINCIPAL

    def test_result_is_never_blank_or_unknown(self) -> None:
        cases = [
            ({"x-agent-id": "ok"}, None),
            ({}, _FakeToken("ok")),
            ({}, None),
            ({"x-agent-id": ""}, _FakeToken("")),
            ({"x-agent-id": "UNKNOWN"}, _FakeToken("Unknown")),
        ]
        for headers, token in cases:
            principal = resolve_principal(headers, token)
            assert principal.strip()
            assert principal.lower() != "unknown"


# --------------------------------------------------------------------------- #
# _load_token (env fail-fast)
# --------------------------------------------------------------------------- #


class TestLoadToken:
    def test_valid_token_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", _GOOD_TOKEN)
        assert _load_token() == _GOOD_TOKEN

    def test_missing_env_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            _load_token()

    @pytest.mark.parametrize("placeholder", ["", "replace-me", "replace-with-openssl-rand-hex-32"])
    def test_placeholder_exits(
        self, monkeypatch: pytest.MonkeyPatch, placeholder: str
    ) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", placeholder)
        with pytest.raises(SystemExit):
            _load_token()

    def test_short_token_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
        with pytest.raises(SystemExit):
            _load_token()

    def test_placeholder_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", "REPLACE-ME")
        with pytest.raises(SystemExit):
            _load_token()


# --------------------------------------------------------------------------- #
# HTTP auth gate (starlette TestClient against the real ASGI app)
# --------------------------------------------------------------------------- #


@pytest.fixture
def http_client() -> TestClient:
    app, _sim, _log = _make_app()
    # Suppress the starlette/httpx deprecation warning from TestClient; it is
    # unrelated to the server under test and would otherwise noise up output.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = TestClient(app.http_app())
        with client:  # run ASGI lifespan startup/shutdown
            yield client


_MCP_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.1"},
    },
}


class TestHttpAuthGate:
    def test_no_token_returns_401(self, http_client: TestClient) -> None:
        r = http_client.post("/mcp", json=_MCP_INIT_BODY)
        assert r.status_code == 401
        assert r.headers.get("www-authenticate", "").lower().startswith("bearer")

    def test_wrong_token_returns_401(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/mcp",
            headers={"Authorization": "Bearer definitely-wrong"},
            json=_MCP_INIT_BODY,
        )
        assert r.status_code == 401

    def test_good_token_not_401(self, http_client: TestClient) -> None:
        r = http_client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {_GOOD_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json=_MCP_INIT_BODY,
        )
        assert r.status_code != 401
        # A successful MCP initialize returns 200 with an event-stream / json body.
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Tools (in-memory fastmcp.Client -> real tool functions via MCP protocol)
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client_and_log() -> AsyncIterator[tuple[Client, AcceleratorSimulator, AuditLog]]:
    app, sim, log = _make_app()
    # Advance the sim so log/anomaly queries have data.
    for _ in range(3):
        sim.tick(1.0)
    async with Client(app) as client:
        yield client, sim, log


class TestListSubsystems:
    @pytest.mark.asyncio
    async def test_lists_all_subsystems_and_parameters(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, _log = client_and_log
        result = await client.call_tool("list_subsystems", {})
        assert not result.is_error
        data = result.structured_content
        assert set(data) == {s.value for s in Subsystem}
        assert "temperature_setpoint" in data["cryogenics"]


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_returns_subsystem_status(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, _log = client_and_log
        result = await client.call_tool("get_status", {"subsystem": "cryogenics"})
        assert not result.is_error
        data = result.structured_content
        assert data["subsystem"] == "cryogenics"
        assert "readings" in data
        assert "health" in data


class TestGetSensorLogs:
    @pytest.mark.asyncio
    async def test_returns_readings(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, _log = client_and_log
        result = await client.call_tool(
            "get_sensor_logs", {"subsystem": "cryogenics", "window_s": 10}
        )
        assert not result.is_error
        # FastMCP wraps tuple/list returns in {"result": [...]}.
        readings = result.structured_content["result"]
        assert isinstance(readings, list)
        assert len(readings) >= 2  # two cryo sensors ticked 3x
        assert all(r["subsystem"] == "cryogenics" for r in readings)

    @pytest.mark.asyncio
    async def test_window_over_max_raises_tool_error(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, _log = client_and_log
        with pytest.raises(ToolError, match=r"window_s must be in"):
            await client.call_tool(
                "get_sensor_logs",
                {"subsystem": "cryogenics", "window_s": MAX_WINDOW_S + 1},
            )

    @pytest.mark.asyncio
    async def test_negative_window_raises_tool_error(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, _log = client_and_log
        with pytest.raises(ToolError, match=r"window_s must be in"):
            await client.call_tool(
                "get_sensor_logs",
                {"subsystem": "cryogenics", "window_s": -1},
            )

    @pytest.mark.asyncio
    async def test_since_filter_excludes_future(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, sim, _log = client_and_log
        # A ``since`` one second after the sim clock excludes every reading.
        future = sim._clock + timedelta(seconds=1)
        result = await client.call_tool(
            "get_sensor_logs",
            {
                "subsystem": "cryogenics",
                "window_s": 3600,
                "since": future.isoformat(),
            },
        )
        assert not result.is_error
        assert result.structured_content["result"] == []

    @pytest.mark.asyncio
    async def test_since_naive_datetime_treated_as_utc(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, sim, _log = client_and_log
        # A naive ``since`` (no offset) must be normalized to UTC, not crash.
        naive = (sim._clock - timedelta(seconds=1)).replace(tzinfo=None)
        result = await client.call_tool(
            "get_sensor_logs",
            {
                "subsystem": "cryogenics",
                "window_s": 3600,
                "since": naive.isoformat(),
            },
        )
        assert not result.is_error
        assert len(result.structured_content["result"]) >= 1


class TestGetRecentAnomalies:
    @pytest.mark.asyncio
    async def test_returns_list(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, _log = client_and_log
        result = await client.call_tool("get_recent_anomalies", {"window_s": 60})
        assert not result.is_error
        anomalies = result.structured_content["result"]
        assert isinstance(anomalies, list)

    @pytest.mark.asyncio
    async def test_bad_window_raises(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, _log = client_and_log
        with pytest.raises(ToolError, match=r"window_s must be in"):
            await client.call_tool(
                "get_recent_anomalies", {"window_s": MAX_WINDOW_S + 1}
            )


class TestExecuteCommand:
    @pytest.mark.asyncio
    async def test_accept_returns_command_result_and_audits(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, log = client_and_log
        result = await client.call_tool(
            "execute_command",
            {
                "subsystem": "cryogenics",
                "parameter": "temperature_setpoint",
                "value": 4.5,
                "reason": "restore after spike",
            },
        )
        assert not result.is_error
        data = result.structured_content
        assert data["accepted"] is True
        assert data["decision"] == Decision.accepted.value
        assert data["audit_id"].startswith("audit-")
        # audit recorded with the fallback principal (no X-Agent-Id in-memory)
        assert len(log) == 1
        entry = log.entries()[0]
        assert entry.decision is Decision.accepted
        assert entry.principal == DEFAULT_PRINCIPAL

    @pytest.mark.asyncio
    async def test_out_of_range_rejected_but_audited(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, log = client_and_log
        result = await client.call_tool(
            "execute_command",
            {
                "subsystem": "cryogenics",
                "parameter": "temperature_setpoint",
                "value": 1.0,  # below the 2.0 K minimum
                "reason": "out of range test",
            },
        )
        # Rejections are returned (not raised): the CommandResult carries the
        # decision, so the caller sees a clear 400-shape without a transport error.
        assert not result.is_error
        data = result.structured_content
        assert data["accepted"] is False
        assert data["decision"] == Decision.rejected_range.value
        assert len(log) == 1
        assert log.entries()[0].decision is Decision.rejected_range

    @pytest.mark.asyncio
    async def test_unknown_parameter_rejected_but_audited(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, log = client_and_log
        result = await client.call_tool(
            "execute_command",
            {
                "subsystem": "cryogenics",
                "parameter": "nope",
                "value": 4.5,
                "reason": "unknown param test",
            },
        )
        assert not result.is_error
        data = result.structured_content
        assert data["accepted"] is False
        assert data["decision"] == Decision.rejected_unknown_parameter.value
        assert len(log) == 1
        assert log.entries()[0].decision is Decision.rejected_unknown_parameter

    @pytest.mark.asyncio
    async def test_unknown_subsystem_raises_clear_error(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, log = client_and_log
        with pytest.raises(ToolError, match=r"is not one of"):
            await client.call_tool(
                "execute_command",
                {
                    "subsystem": "not_a_subsystem",
                    "parameter": "temperature_setpoint",
                    "value": 4.5,
                    "reason": "bad subsystem",
                },
            )
        # Schema rejection happens before the tool body -> nothing audited.
        assert len(log) == 0

    @pytest.mark.asyncio
    async def test_accept_and_reject_both_audited(
        self, client_and_log: tuple[Client, AcceleratorSimulator, AuditLog]
    ) -> None:
        client, _sim, log = client_and_log
        await client.call_tool(
            "execute_command",
            {
                "subsystem": "cryogenics",
                "parameter": "temperature_setpoint",
                "value": 4.5,
                "reason": "ok",
            },
        )
        await client.call_tool(
            "execute_command",
            {
                "subsystem": "cryogenics",
                "parameter": "temperature_setpoint",
                "value": 1.0,
                "reason": "bad",
            },
        )
        assert len(log) == 2
        decisions = [e.decision for e in log.entries()]
        assert decisions == [Decision.accepted, Decision.rejected_range]


class TestExecuteCommandPrincipal:
    @pytest.mark.asyncio
    async def test_x_agent_id_recorded_as_principal(
        self,
        client_and_log: tuple[Client, AcceleratorSimulator, AuditLog],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _sim, log = client_and_log
        # Simulate an authenticated HTTP request carrying X-Agent-Id. The server
        # resolves the principal from headers at call time via this module global.
        import telemetry_mcp.server as server_mod

        monkeypatch.setattr(
            server_mod, "get_http_headers", lambda: {"x-agent-id": "agent-ops-01"}
        )
        await client.call_tool(
            "execute_command",
            {
                "subsystem": "cryogenics",
                "parameter": "temperature_setpoint",
                "value": 4.5,
                "reason": "agent corrective action",
            },
        )
        assert len(log) == 1
        assert log.entries()[0].principal == "agent-ops-01"

    @pytest.mark.asyncio
    async def test_token_client_id_recorded_when_no_header(
        self,
        client_and_log: tuple[Client, AcceleratorSimulator, AuditLog],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _sim, log = client_and_log
        import telemetry_mcp.server as server_mod

        class _Tok:
            client_id = "operator-42"

        monkeypatch.setattr(server_mod, "get_http_headers", lambda: {})
        monkeypatch.setattr(server_mod, "get_access_token", lambda: _Tok())
        await client.call_tool(
            "execute_command",
            {
                "subsystem": "cryogenics",
                "parameter": "temperature_setpoint",
                "value": 4.5,
                "reason": "operator action",
            },
        )
        assert log.entries()[0].principal == "operator-42"
