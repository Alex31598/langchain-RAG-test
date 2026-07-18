"""FastMCP server (HTTP/SSE) for the telemetry MCP.

Exposes five tools over the MCP protocol, every one gated by a shared-secret
bearer token (``MCP_AUTH_TOKEN``) checked at the HTTP entrypoint via a
constant-time :class:`SharedSecretBearerAuth`. Input is bounded at two layers:
pydantic validates ``subsystem`` against the :class:`Subsystem` enum (rejecting
unknown subsystems at the schema boundary), and the tools re-check ``window_s``
bounds and route ``execute_command`` through :mod:`telemetry_mcp.safety` (the
sole authority for parameter / range / precondition validation). Every
``execute_command`` call -- accept or reject -- produces an :class:`AuditEntry`
carrying the caller ``principal`` (resolved from the ``X-Agent-Id`` header, the
verified token, or a non-blank fallback), recorded by the simulator under its
lock so validation + audit + mutation are atomic.

The HITL gate (M1.6) layers on top of the accept path here; ``_confirm_intent``
is the hook that wires in. See ``tasks/M1-telemetry-mcp/05-fastmcp-server-auth.md``
and ``tasks/PLAN.md`` §4.2, §6.1, §8, §9.

Run with ``uv run python -m telemetry_mcp.server`` (the container CMD). The
server binds to ``0.0.0.0:8000`` inside the container; docker-compose maps it to
``127.0.0.1:8100`` (PLAN §6.1 -- localhost only, never LAN/internet).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_headers

from telemetry_mcp.models import (
    PARAMETER_SPECS,
    Command,
    CommandResult,
    SensorReading,
    Subsystem,
    SubsystemStatus,
)
from telemetry_mcp.safety import AuditLog
from telemetry_mcp.simulator import AcceleratorSimulator

__all__ = [
    "SharedSecretBearerAuth",
    "create_app",
    "resolve_principal",
    "MAX_WINDOW_S",
    "DEFAULT_PRINCIPAL",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
]

#: Bounded ``window_s`` upper limit (mirrors :data:`simulator.MAX_WINDOW_S`).
MAX_WINDOW_S: int = 3600

#: Fallback principal when neither ``X-Agent-Id`` nor the token carries a
#: usable identity. Non-blank and not ``"unknown"`` so :func:`safety.validate_command`
#: accepts it; the shared-secret model has no per-caller identity, so this
#: labels audited actions as coming from the MCP client role.
DEFAULT_PRINCIPAL: str = "mcp-client"

#: Container bind address (compose maps to 127.0.0.1:8100; PLAN §6.1).
DEFAULT_HOST: str = "0.0.0.0"
DEFAULT_PORT: int = 8000

#: ``MCP_AUTH_TOKEN`` placeholder values from ``.env.example`` that must never
#: be accepted as a real secret. Checked case-insensitively.
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset(
    {"", "replace-me", "replace-with-openssl-rand-hex-32"}
)

#: Minimum bearer-token length. ``openssl rand -hex 32`` yields 64 chars; 16 is
#: a hard floor below which a token is too weak to ship.
_MIN_TOKEN_LEN: int = 16


class SharedSecretBearerAuth(TokenVerifier):
    """Token verifier for the single shared-secret bearer token (``MCP_AUTH_TOKEN``).

    FastMCP mounts this as a ``BearerAuthBackend`` that extracts the
    ``Authorization: Bearer <token>`` header and calls :meth:`verify_token`.
    A constant-time compare (:func:`secrets.compare_digest`) guards against
    timing side channels; a mismatch returns ``None`` which FastMCP maps to
    HTTP 401. The token itself is never logged, echoed, or compared with ``==``.

    On a match, an :class:`AccessToken` with a fixed ``client_id`` (the
    :data:`DEFAULT_PRINCIPAL`) is returned -- the shared secret carries no
    per-caller identity, so the caller may further identify itself via the
    ``X-Agent-Id`` header (see :func:`resolve_principal`).
    """

    __slots__ = ("_expected_token",)

    def __init__(self, expected_token: str) -> None:
        super().__init__()
        if not expected_token:
            raise ValueError("expected_token must be a non-empty string")
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an :class:`AccessToken` iff ``token`` matches the shared secret.

        Uses :func:`secrets.compare_digest` (constant-time) so an attacker cannot
        distinguish "no header" from "wrong header" via response timing. Returns
        ``None`` on any mismatch -> FastMCP responds 401.
        """
        try:
            ok = secrets.compare_digest(token, self._expected_token)
        except TypeError:
            # ``compare_digest`` raises TypeError on non-str/bytes input; treat
            # any malformed token as a mismatch rather than a 500.
            return None
        if not ok:
            return None
        return AccessToken(
            token=token,
            client_id=DEFAULT_PRINCIPAL,
            scopes=[],
            claims={},
        )


def resolve_principal(
    headers: Mapping[str, str], access_token: AccessToken | None
) -> str:
    """Pick the principal recorded on the audit row for ``execute_command``.

    Preference order: a non-blank, non-``"unknown"`` ``X-Agent-Id`` header; the
    verified token's ``client_id``; the :data:`DEFAULT_PRINCIPAL` fallback. The
    result is always a non-blank, non-``"unknown"`` identity, so
    :func:`telemetry_mcp.safety.validate_command` never rejects the command on a
    principal-contract violation.
    """
    agent_id = (headers.get("x-agent-id") or "").strip()
    if agent_id and agent_id.lower() != "unknown":
        return agent_id
    if access_token is not None:
        client_id = (access_token.client_id or "").strip()
        if client_id and client_id.lower() != "unknown":
            return client_id
    return DEFAULT_PRINCIPAL


def _check_window_s(window_s: float) -> None:
    """Raise a :class:`ToolError` unless ``0 <= window_s <= MAX_WINDOW_S``."""
    if window_s < 0 or window_s > MAX_WINDOW_S:
        raise ToolError(
            f"window_s must be in [0, {MAX_WINDOW_S}], got {window_s!r}"
        )


def _normalize_since(since: datetime | None) -> datetime | None:
    """Attach UTC to a naive ``since`` so it compares against the sim clock."""
    if since is not None and since.tzinfo is None:
        return since.replace(tzinfo=UTC)
    return since


def create_app(
    *,
    token: str,
    simulator: AcceleratorSimulator,
    audit_log: AuditLog,
    name: str = "telemetry-mcp",
) -> FastMCP:
    """Build a :class:`FastMCP` server wired to ``simulator`` and ``audit_log``.

    The simulator and audit log are closed over by the tool functions so tests
    can inject a fixed-seed simulator and a fresh :class:`AuditLog`. Production
    ``main()`` constructs them from environment configuration.
    """
    mcp = FastMCP(
        name=name,
        auth=SharedSecretBearerAuth(token),
        strict_input_validation=True,
    )

    @mcp.tool()
    async def list_subsystems() -> dict[str, list[str]]:
        """List the available subsystems and their controllable parameters."""
        return {
            sub.value: sorted(specs) for sub, specs in PARAMETER_SPECS.items()
        }

    @mcp.tool()
    async def get_status(subsystem: Subsystem) -> SubsystemStatus:
        """Aggregated snapshot of one subsystem: latest readings + health flag."""
        try:
            return simulator.get_status(subsystem.value)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def get_sensor_logs(
        subsystem: Subsystem,
        window_s: float,
        since: datetime | None = None,
    ) -> tuple[SensorReading, ...]:
        """Recent readings for one subsystem within ``window_s`` seconds."""
        _check_window_s(window_s)
        try:
            return simulator.get_sensor_logs(
                subsystem.value, window_s, _normalize_since(since)
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def get_recent_anomalies(
        window_s: float,
        since: datetime | None = None,
    ) -> tuple[SensorReading, ...]:
        """Pre-flagged outlier readings within ``window_s`` seconds."""
        _check_window_s(window_s)
        try:
            return simulator.get_recent_anomalies(window_s, _normalize_since(since))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def execute_command(
        subsystem: Subsystem,
        parameter: str,
        value: float,
        reason: str,
    ) -> CommandResult:
        """Validate and apply a parameter change to a subsystem.

        Routes through :func:`simulator.AcceleratorSimulator.apply_command`,
        which calls :func:`telemetry_mcp.safety.validate_command` under the
        simulator lock: range + precondition interlocks run, an
        :class:`AuditEntry` with the caller principal is recorded (accept *or*
        reject), and the setpoint mutates only when the interlocks accept. The
        returned :class:`CommandResult` carries the decision and audit id.

        HITL hook: non-allow-listed parameters require confirmation before
        mutation (M1.6, ``_confirm_intent``); this tool applies immediately
        pending that gate.
        """
        principal = resolve_principal(get_http_headers(), get_access_token())
        cmd = Command(
            subsystem=subsystem,
            parameter=parameter,
            value=value,
            reason=reason,
        )
        return simulator.apply_command(cmd, principal, audit_log)

    return mcp


def _load_token() -> str:
    """Read ``MCP_AUTH_TOKEN`` from the environment and fail fast if unconfigured.

    Rejects a missing variable, a placeholder value from ``.env.example``, or a
    token shorter than :data:`_MIN_TOKEN_LEN`. The telemetry server needs no
    other secret: ``NVIDIA_NIM_*`` is consumed by the agent (M2), not here.
    """
    raw = os.environ.get("MCP_AUTH_TOKEN")
    if raw is None:
        raise SystemExit(
            "MCP_AUTH_TOKEN environment variable is required (see .env.example)."
        )
    token = raw.strip()
    if token.lower() in _PLACEHOLDER_TOKENS or len(token) < _MIN_TOKEN_LEN:
        raise SystemExit(
            "MCP_AUTH_TOKEN is not configured: replace the placeholder in "
            ".env with a strong random token, e.g. `openssl rand -hex 32`."
        )
    return token


def main() -> None:
    """Entrypoint for ``python -m telemetry_mcp.server`` (container CMD)."""
    token = _load_token()
    seed = int(os.environ.get("TELEMETRY_SIM_SEED", "0"))
    simulator = AcceleratorSimulator(seed=seed)
    audit_log = AuditLog()
    app = create_app(token=token, simulator=simulator, audit_log=audit_log)
    app.run(transport="http", host=DEFAULT_HOST, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
