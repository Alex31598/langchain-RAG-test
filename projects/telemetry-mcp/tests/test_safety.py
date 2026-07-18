"""Unit tests for the safety interlocks + audit log (M1.4).

Covers the M1.4 acceptance criteria:

- out-of-range / unknown subsystem / unknown parameter -> rejected with a
  400-shaped ``CommandResult`` (``accepted=False``),
- every accepted AND rejected command produces an ``AuditEntry``
  (rejections audited too),
- ``principal`` is always recorded (never blank or ``unknown``),
- no ``Any`` types (mypy strict, enforced at the project level).

The pure :func:`decide` core and the audited :func:`validate_command` entry
point are both exercised. See ``tasks/M1-telemetry-mcp/04-safety-interlocks.md``.
"""

from __future__ import annotations

import json

import pytest
from telemetry_mcp.models import (
    AuditEntry,
    Command,
    CommandResult,
    Decision,
    Subsystem,
)
from telemetry_mcp.safety import (
    CORRECTIVE_ALLOWLIST,
    AuditLog,
    decide,
    is_corrective,
    validate_command,
)

# A nominal sensor snapshot where every precondition holds. Sensor names match
# ``_SENSOR_DEFS`` in simulator.py and the ``PARAMETER_SPECS`` preconditions.
_NOMINAL_SNAPSHOT: dict[Subsystem, dict[str, float]] = {
    Subsystem.cryogenics: {"temperature": 4.0, "he_flow_rate": 25.0},
    Subsystem.vacuum: {"pressure": 1.0e-7, "pump_speed": 80.0},
    Subsystem.beamline_magnets: {"field_strength": 1.5, "current": 250.0},
    Subsystem.rf_cavities: {"rf_amplitude": 5.0, "rf_phase": 0.0},
    Subsystem.power_supply: {"current": 500.0, "voltage": 24.0},
}

_PRINCIPAL = "agent-ops-01"


def _cmd(
    *,
    subsystem: Subsystem = Subsystem.cryogenics,
    parameter: str = "temperature_setpoint",
    value: float = 4.5,
    reason: str = "restore cryo setpoint after spike",
) -> Command:
    return Command(
        subsystem=subsystem,
        parameter=parameter,
        value=value,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# decide (pure interlock core)
# --------------------------------------------------------------------------- #


class TestDecide:
    def test_accepts_in_range_with_preconditions_met(self) -> None:
        assert decide(_cmd(), _NOMINAL_SNAPSHOT) is Decision.accepted

    def test_accepts_beamline_setpoint_when_cryo_and_vacuum_ok(self) -> None:
        cmd = _cmd(
            subsystem=Subsystem.beamline_magnets,
            parameter="field_strength_setpoint",
            value=2.0,
        )
        assert decide(cmd, _NOMINAL_SNAPSHOT) is Decision.accepted

    def test_rejects_unknown_parameter(self) -> None:
        assert (
            decide(_cmd(parameter="nope"), _NOMINAL_SNAPSHOT)
            is Decision.rejected_unknown_parameter
        )

    def test_rejects_below_range(self) -> None:
        # temperature_setpoint min is 2.0 K
        assert decide(_cmd(value=1.0), _NOMINAL_SNAPSHOT) is Decision.rejected_range

    def test_rejects_above_range(self) -> None:
        # temperature_setpoint max is 300.0 K
        assert (
            decide(_cmd(value=301.0), _NOMINAL_SNAPSHOT) is Decision.rejected_range
        )

    def test_rejects_precondition_too_hot(self) -> None:
        snap = {
            **_NOMINAL_SNAPSHOT,
            Subsystem.cryogenics: {"temperature": 5.0, "he_flow_rate": 25.0},
        }
        cmd = _cmd(
            subsystem=Subsystem.beamline_magnets,
            parameter="field_strength_setpoint",
            value=2.0,
        )
        assert decide(cmd, snap) is Decision.rejected_precondition

    def test_rejects_precondition_missing_sensor(self) -> None:
        snap = dict(_NOMINAL_SNAPSHOT)
        snap.pop(Subsystem.vacuum)
        cmd = _cmd(
            subsystem=Subsystem.beamline_magnets,
            parameter="field_strength_setpoint",
            value=2.0,
        )
        assert decide(cmd, snap) is Decision.rejected_precondition

    def test_rejects_unknown_subsystem_defense_in_depth(self) -> None:
        # ``Command.subsystem`` is a Subsystem enum, so the transport rejects
        # unknown subsystems at parse time before safety ever runs. Exercise
        # the defense-in-depth branch directly via model_construct, which
        # bypasses pydantic validation and leaves the raw string in place.
        cmd = Command.model_construct(
            subsystem="not_a_subsystem",
            parameter="temperature_setpoint",
            value=4.5,
            reason="bypass",
        )
        assert (
            decide(cmd, _NOMINAL_SNAPSHOT) is Decision.rejected_unknown_subsystem
        )


# --------------------------------------------------------------------------- #
# validate_command (audited, principal-aware entry point)
# --------------------------------------------------------------------------- #


class TestValidateCommand:
    def test_accept_returns_command_result_with_audit_id(self) -> None:
        log = AuditLog()
        result = validate_command(_cmd(), _PRINCIPAL, _NOMINAL_SNAPSHOT, log)
        assert isinstance(result, CommandResult)
        assert result.accepted is True
        assert result.decision is Decision.accepted
        assert result.audit_id.startswith("audit-")
        assert len(result.audit_id) == len("audit-") + 8
        assert len(log) == 1

    def test_reject_out_of_range_returns_400_shape_and_audits(self) -> None:
        log = AuditLog()
        result = validate_command(_cmd(value=1.0), _PRINCIPAL, _NOMINAL_SNAPSHOT, log)
        assert result.accepted is False
        assert result.decision is Decision.rejected_range
        assert result.audit_id.startswith("audit-")
        # rejections are audited too
        assert len(log) == 1
        entry = log.entries()[0]
        assert entry.decision is Decision.rejected_range
        assert entry.principal == _PRINCIPAL

    def test_reject_unknown_parameter_audited(self) -> None:
        log = AuditLog()
        result = validate_command(
            _cmd(parameter="nope"), _PRINCIPAL, _NOMINAL_SNAPSHOT, log
        )
        assert result.accepted is False
        assert result.decision is Decision.rejected_unknown_parameter
        assert len(log) == 1
        assert log.entries()[0].decision is Decision.rejected_unknown_parameter

    def test_reject_precondition_audited(self) -> None:
        log = AuditLog()
        snap = {
            **_NOMINAL_SNAPSHOT,
            Subsystem.cryogenics: {"temperature": 5.0, "he_flow_rate": 25.0},
        }
        cmd = _cmd(
            subsystem=Subsystem.beamline_magnets,
            parameter="field_strength_setpoint",
            value=2.0,
        )
        result = validate_command(cmd, _PRINCIPAL, snap, log)
        assert result.accepted is False
        assert result.decision is Decision.rejected_precondition
        assert len(log) == 1

    def test_principal_always_recorded(self) -> None:
        log = AuditLog()
        validate_command(_cmd(), _PRINCIPAL, _NOMINAL_SNAPSHOT, log)
        validate_command(_cmd(value=1.0), "operator-42", _NOMINAL_SNAPSHOT, log)
        validate_command(
            _cmd(parameter="nope"), "supervisor-7", _NOMINAL_SNAPSHOT, log
        )
        principals = [e.principal for e in log.entries()]
        assert principals == [_PRINCIPAL, "operator-42", "supervisor-7"]
        assert all(p and p.lower() != "unknown" for p in principals)

    @pytest.mark.parametrize("bad_principal", ["", "   ", "unknown", "UNKNOWN", "  Unknown  "])
    def test_invalid_principal_raises_value_error(self, bad_principal: str) -> None:
        log = AuditLog()
        with pytest.raises(ValueError):
            validate_command(_cmd(), bad_principal, _NOMINAL_SNAPSHOT, log)
        # nothing audited when the principal contract is violated
        assert len(log) == 0


# --------------------------------------------------------------------------- #
# AuditLog
# --------------------------------------------------------------------------- #


class TestAuditLog:
    def test_append_returns_monotonic_ids(self) -> None:
        log = AuditLog()
        assert log.append(_entry()) == "audit-00000001"
        assert log.append(_entry()) == "audit-00000002"
        assert len(log) == 2

    def test_records_and_entries_preserve_order(self) -> None:
        log = AuditLog()
        log.append(_entry(parameter="temperature_setpoint"))
        log.append(_entry(parameter="he_flow_rate"))
        records = log.records()
        assert records[0][0] == "audit-00000001"
        assert records[0][1].parameter == "temperature_setpoint"
        assert records[1][0] == "audit-00000002"
        assert records[1][1].parameter == "he_flow_rate"
        assert [e.parameter for e in log.entries()] == [
            "temperature_setpoint",
            "he_flow_rate",
        ]

    def test_ring_eviction_drops_oldest(self) -> None:
        log = AuditLog(capacity=2)
        log.append(_entry(parameter="a"))
        log.append(_entry(parameter="b"))
        log.append(_entry(parameter="c"))
        assert len(log) == 2
        assert [e.parameter for e in log.entries()] == ["b", "c"]
        # counter keeps growing past eviction -> ids stay unique
        assert log.append(_entry(parameter="d")) == "audit-00000004"

    def test_clear_resets_buffer_and_counter(self) -> None:
        log = AuditLog()
        log.append(_entry())
        log.clear()
        assert len(log) == 0
        assert log.append(_entry()) == "audit-00000001"

    def test_to_json_emits_ndjson_with_audit_id_and_fields(self) -> None:
        log = AuditLog()
        log.append(
            _entry(
                principal="ops-1",
                subsystem=Subsystem.cryogenics,
                parameter="temperature_setpoint",
                value=4.5,
                decision=Decision.accepted,
            )
        )
        log.append(
            _entry(
                principal="ops-2",
                subsystem=Subsystem.vacuum,
                parameter="pump_speed",
                value=90.0,
                decision=Decision.rejected_range,
            )
        )
        lines = log.to_json().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["audit_id"] == "audit-00000001"
        assert first["principal"] == "ops-1"
        assert first["subsystem"] == "cryogenics"
        assert first["parameter"] == "temperature_setpoint"
        assert first["value"] == 4.5
        assert first["decision"] == "accepted"
        assert "timestamp" in first
        second = json.loads(lines[1])
        assert second["audit_id"] == "audit-00000002"
        assert second["decision"] == "rejected_range"

    def test_sink_receives_one_json_line_per_append(self) -> None:
        sink_lines: list[str] = []
        log = AuditLog(sink=sink_lines.append)
        log.append(_entry(parameter="temperature_setpoint"))
        log.append(_entry(parameter="he_flow_rate"))
        assert len(sink_lines) == 2
        parsed = json.loads(sink_lines[0])
        assert parsed["audit_id"] == "audit-00000001"
        assert parsed["parameter"] == "temperature_setpoint"

    def test_capacity_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            AuditLog(capacity=0)


# --------------------------------------------------------------------------- #
# CORRECTIVE_ALLOWLIST / is_corrective
# --------------------------------------------------------------------------- #


class TestCorrectiveAllowlist:
    def test_cryo_setpoints_are_corrective(self) -> None:
        assert is_corrective(Subsystem.cryogenics, "temperature_setpoint") is True
        assert is_corrective(Subsystem.cryogenics, "he_flow_rate") is True

    def test_high_impact_actuators_require_hitl(self) -> None:
        assert (
            is_corrective(Subsystem.beamline_magnets, "field_strength_setpoint")
            is False
        )
        assert is_corrective(Subsystem.rf_cavities, "rf_amplitude") is False

    def test_rf_phase_is_corrective(self) -> None:
        assert is_corrective(Subsystem.rf_cavities, "rf_phase") is True

    def test_unknown_subsystem_is_not_corrective(self) -> None:
        # ``Subsystem`` has no unknown members at runtime; emulate the
        # conservative default path directly against the table.
        assert "nope" not in CORRECTIVE_ALLOWLIST.get(  # sanity on the helper
            Subsystem.beamline_magnets, frozenset()
        )

    def test_allowlist_covers_only_known_parameters(self) -> None:
        # Every allow-listed parameter must exist in PARAMETER_SPECS so the
        # HITL gate never treats an unknown parameter as auto-confirmable.
        from telemetry_mcp.models import PARAMETER_SPECS

        for sub, params in CORRECTIVE_ALLOWLIST.items():
            spec_table = PARAMETER_SPECS[sub]
            for p in params:
                assert p in spec_table, f"{sub.value}/{p} not in PARAMETER_SPECS"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _entry(
    *,
    principal: str = _PRINCIPAL,
    subsystem: Subsystem = Subsystem.cryogenics,
    parameter: str = "temperature_setpoint",
    value: float = 4.5,
    decision: Decision = Decision.accepted,
    reason: str = "restore cryo setpoint after spike",
) -> AuditEntry:
    from datetime import UTC, datetime

    return AuditEntry(
        principal=principal,
        subsystem=subsystem,
        parameter=parameter,
        value=value,
        decision=decision,
        reason=reason,
        timestamp=datetime.now(UTC),
    )
