"""Command interlocks: range/precondition validation, audit log, HITL allow-list.

This module is the sole authority that decides whether a :class:`Command` may
mutate simulator state. It exposes two layers:

- :func:`decide` -- the **pure interlock core**. It checks a command against
  :data:`PARAMETER_SPECS` (range + cross-subsystem preconditions) and returns a
  :class:`Decision`. It has no side effects and never raises on a rejection;
  the simulator calls it under its lock for defense-in-depth re-validation
  (see ``tasks/M1-telemetry-mcp/02-simulator.md``).

- :func:`validate_command` -- the **audited, principal-aware entry point** used
  by the MCP server (M1.5). It calls :func:`decide`, records an
  :class:`AuditEntry` (who / what / when / decision) on the supplied
  :class:`AuditLog` for *every* accepted *and* rejected command, and returns a
  :class:`CommandResult` carrying the audit id. ``principal`` is always
  recorded and must be a non-empty, non-``"unknown"`` identity supplied by the
  authenticated transport; an invalid principal is a fail-closed contract
  violation (raises ``ValueError``), not an interlock rejection.

:data:`CORRECTIVE_ALLOWLIST` and :func:`is_corrective` define the narrow set of
"clearly corrective" parameters the human-in-the-loop gate (M1.6) lets the
agent apply without explicit confirmation. Interlocks check *ranges*; the HITL
gate checks *intent* (PLAN §4.2).

See ``tasks/M1-telemetry-mcp/04-safety-interlocks.md`` and ``06-hitl-gate.md``.
This is the highest-risk module in the repo (PLAN §4.2, §8): keep the interlock
logic pure and the audit log append-only.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TypeAlias

from telemetry_mcp.models import (
    PARAMETER_SPECS,
    AuditEntry,
    Command,
    CommandResult,
    Decision,
    ParamKind,
    Subsystem,
)

__all__ = [
    "SensorSnapshot",
    "decide",
    "validate_command",
    "AuditLog",
    "DEFAULT_AUDIT_CAPACITY",
    "CORRECTIVE_ALLOWLIST",
    "is_corrective",
]

#: Snapshot of the latest sensor values a command's preconditions are checked
#: against. The simulator builds this from its current state and passes it in.
SensorSnapshot: TypeAlias = Mapping[Subsystem, Mapping[str, float]]

#: Sentinel rejected by :func:`validate_command` as the caller principal. The
#: authenticated transport must supply a real identity; ``"unknown"`` would
#: hide who issued a mutating command and is therefore fail-closed.
_UNKNOWN_PRINCIPAL: str = "unknown"

#: Default ring capacity for :class:`AuditLog` (kept in memory). Ample for a
#: demo session; older entries are evicted oldest-first but remain in the
#: structured-JSON sink if one is attached.
DEFAULT_AUDIT_CAPACITY: int = 10_000


def decide(cmd: Command, sensor_snapshot: SensorSnapshot) -> Decision:
    """Purely decide whether ``cmd`` passes the interlocks.

    Checks, in order:
    1. the subsystem has a parameter spec table,
    2. the parameter is in the subsystem's allow-list,
    3. the value is within the spec's ``[min, max]`` range (with kind check),
    4. every precondition holds against ``sensor_snapshot``.

    Returns the :class:`Decision` (``accepted`` or a specific rejection). This
    function is pure: it never raises and never mutates state. The simulator
    calls it under its lock for defense-in-depth re-validation;
    :func:`validate_command` wraps it with the audit log + principal handling.
    """
    specs = PARAMETER_SPECS.get(cmd.subsystem)
    if specs is None:
        return Decision.rejected_unknown_subsystem

    spec = specs.get(cmd.parameter)
    if spec is None:
        return Decision.rejected_unknown_parameter

    value = float(cmd.value)
    if spec.kind is ParamKind.int and not value.is_integer():
        return Decision.rejected_range
    if not spec.min <= value <= spec.max:
        return Decision.rejected_range

    for pre in spec.preconditions:
        readings = sensor_snapshot.get(pre.subsystem)
        if readings is None or pre.sensor_name not in readings:
            return Decision.rejected_precondition
        if not pre.min <= float(readings[pre.sensor_name]) <= pre.max:
            return Decision.rejected_precondition

    return Decision.accepted


class AuditLog:
    """Append-only audit log of every command decision.

    Stores ``(audit_id, AuditEntry)`` records in a bounded in-memory ring
    buffer and, if a ``sink`` is attached, emits one structured JSON line per
    append (the persistent representation). The audit id is a monotonic
    ``"audit-XXXXXXXX"`` counter that stays unique even after old entries are
    evicted from the ring.

    Thread-safe: all public methods take an internal lock so the log can be
    shared across FastMCP workers. The ``sink`` callable is invoked *under* the
    lock to preserve global ordering -- it must be non-blocking (e.g. a
    buffered file ``write``) and must never re-enter the log. A ``sink`` error
    propagates (fail-loud for an audit persistence failure) after the entry has
    been stored in the in-memory ring.
    """

    __slots__ = ("_buffer", "_sink", "_lock", "_counter")

    _buffer: deque[tuple[str, AuditEntry]]
    _sink: Callable[[str], None] | None
    _lock: threading.Lock
    _counter: int

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_AUDIT_CAPACITY,
        sink: Callable[[str], None] | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buffer = deque(maxlen=capacity)
        self._sink = sink
        self._lock = threading.Lock()
        self._counter = 0

    def append(self, entry: AuditEntry) -> str:
        """Record ``entry`` and return its monotonic audit id (``"audit-XXXXXXXX"``)."""
        with self._lock:
            self._counter += 1
            audit_id = f"audit-{self._counter:08d}"
            self._buffer.append((audit_id, entry))
            if self._sink is not None:
                self._sink(_record_to_json(audit_id, entry))
            return audit_id

    def entries(self) -> tuple[AuditEntry, ...]:
        """Snapshot of the stored entries (no audit ids), oldest-first."""
        with self._lock:
            return tuple(entry for _, entry in self._buffer)

    def records(self) -> tuple[tuple[str, AuditEntry], ...]:
        """Snapshot of ``(audit_id, entry)`` pairs, oldest-first."""
        with self._lock:
            return tuple(self._buffer)

    def to_json(self) -> str:
        """All stored records as newline-delimited JSON (one object per line)."""
        with self._lock:
            return "\n".join(
                _record_to_json(audit_id, entry) for audit_id, entry in self._buffer
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    def clear(self) -> None:
        """Drop every record and reset the audit-id counter (test/admin hook)."""
        with self._lock:
            self._buffer.clear()
            self._counter = 0


def _record_to_json(audit_id: str, entry: AuditEntry) -> str:
    """Serialize one audit record to a single JSON-lines string."""
    payload: dict[str, object] = {
        "audit_id": audit_id,
        "principal": entry.principal,
        "subsystem": entry.subsystem.value,
        "parameter": entry.parameter,
        "value": entry.value,
        "decision": entry.decision.value,
        "reason": entry.reason,
        "timestamp": entry.timestamp.isoformat(),
    }
    return json.dumps(payload, separators=(",", ":"))


def validate_command(
    cmd: Command,
    principal: str,
    sensor_snapshot: SensorSnapshot,
    audit_log: AuditLog,
) -> CommandResult:
    """Validate ``cmd`` and record an audited decision for ``principal``.

    Routes the command through :func:`decide` for range + precondition
    interlocks, then writes an :class:`AuditEntry` (with ``principal`` and the
    resulting :class:`Decision`) to ``audit_log`` regardless of the outcome --
    rejections are audited too -- and returns a :class:`CommandResult` carrying
    the audit id. Never raises on an interlock rejection; the caller maps
    ``accepted=False`` to HTTP 400.

    ``principal`` must be a non-empty string other than ``"unknown"``. It is
    supplied by the authenticated transport (M1.5) and recorded on every audit
    row; a blank/unknown principal is a fail-closed contract violation and
    raises ``ValueError`` rather than silently auditing an unidentifiable
    mutating action.
    """
    if not _is_valid_principal(principal):
        raise ValueError(
            "principal must be a non-empty string other than 'unknown'"
        )

    decision = decide(cmd, sensor_snapshot)
    timestamp = datetime.now(UTC)
    entry = AuditEntry(
        principal=principal,
        subsystem=cmd.subsystem,
        parameter=cmd.parameter,
        value=cmd.value,
        decision=decision,
        reason=cmd.reason,
        timestamp=timestamp,
    )
    audit_id = audit_log.append(entry)
    return CommandResult(
        accepted=decision is Decision.accepted,
        decision=decision,
        audit_id=audit_id,
        timestamp=timestamp,
    )


def _is_valid_principal(principal: str) -> bool:
    """True iff ``principal`` identifies a caller we are willing to audit."""
    if not isinstance(principal, str):  # pragma: no cover - mypy-enforced
        return False
    stripped = principal.strip()
    return bool(stripped) and stripped.lower() != _UNKNOWN_PRINCIPAL


#: Per-subsystem "clearly corrective" parameter allow-list used by the HITL
#: gate (M1.6). A command whose ``parameter`` is in this set may be applied by
#: the agent without explicit human confirmation once the interlocks accept it;
#: anything else requires a signed confirmation token (PLAN §4.2).
#:
#: This is intentionally **narrow**: the two highest-impact actuators --
#: ``beamline_magnets.field_strength_setpoint`` and ``rf_cavities.rf_amplitude``
#: -- are *not* auto-confirmable, because a wrong setpoint there can dump beam
#: energy or quench a magnet string even within range. The remaining setpoints
#: (cryo, vacuum, power-supply restoration, RF phase) are routine corrective
#: levers the diagnostic agent (M2) uses to counter the named fault scenarios
#: in ``scenarios.py``.
CORRECTIVE_ALLOWLIST: dict[Subsystem, frozenset[str]] = {
    Subsystem.beamline_magnets: frozenset(),
    Subsystem.rf_cavities: frozenset({"rf_phase"}),
    Subsystem.cryogenics: frozenset({"temperature_setpoint", "he_flow_rate"}),
    Subsystem.vacuum: frozenset({"pump_speed", "pressure_setpoint"}),
    Subsystem.power_supply: frozenset({"current_setpoint", "voltage_setpoint"}),
}


def is_corrective(subsystem: Subsystem, parameter: str) -> bool:
    """True iff ``(subsystem, parameter)`` is on the HITL corrective allow-list.

    Unknown subsystems are conservatively *not* corrective (returns ``False``)
    so the HITL gate (M1.6) defaults to requiring confirmation.
    """
    return parameter in CORRECTIVE_ALLOWLIST.get(subsystem, frozenset())
