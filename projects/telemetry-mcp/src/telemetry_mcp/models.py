"""Pydantic models for subsystems, sensors, commands, and the audit log.

This module is the **single source of truth** for the telemetry MCP allow-lists:

- :data:`SUBSYSTEMS` — the set of valid subsystem identifiers.
- :data:`PARAMETER_SPECS` — per-subsystem command-parameter allow-list with
  range bounds, units, and cross-subsystem preconditions.

``safety.py`` validates ``Command`` instances against these tables; no other
module should hardcode subsystem or parameter literals. All models are pydantic
v2 ``BaseModel`` with strict, non-``Any`` field types and ``extra="forbid"`` so
unexpected fields are rejected at the boundary.

See ``tasks/PLAN.md`` §4.1, §4.2 and ``tasks/M1-telemetry-mcp/01-models.md``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr

__all__ = [
    "SUBSYSTEMS",
    "PARAMETER_SPECS",
    "Subsystem",
    "Health",
    "Decision",
    "ParamKind",
    "NumericValue",
    "SensorReading",
    "SubsystemStatus",
    "Command",
    "CommandResult",
    "AuditEntry",
    "Precondition",
    "ParameterSpec",
]


class Subsystem(StrEnum):
    """Simulated accelerator subsystems (canonical allow-list)."""

    beamline_magnets = "beamline_magnets"
    rf_cavities = "rf_cavities"
    cryogenics = "cryogenics"
    vacuum = "vacuum"
    power_supply = "power_supply"


#: Allow-list of subsystem identifiers, derived from :class:`Subsystem` so the
#: enum remains the single source of truth. ``safety.py`` and ``server.py``
#: validate incoming subsystem strings against this set.
SUBSYSTEMS: frozenset[str] = frozenset(s.value for s in Subsystem)


#: Strict numeric value accepted for sensor readings and command values.
#: ``bool`` is intentionally rejected by both ``StrictInt`` and ``StrictFloat``.
NumericValue = StrictInt | StrictFloat


class SensorReading(BaseModel):
    """A single timestamped sensor measurement from one subsystem."""

    model_config = ConfigDict(extra="forbid")

    subsystem: Subsystem
    sensor_name: StrictStr
    value: NumericValue
    unit: StrictStr
    timestamp: datetime


class Health(StrEnum):
    """Aggregated health flag for a subsystem snapshot."""

    nominal = "nominal"
    warning = "warning"
    fault = "fault"
    unknown = "unknown"


class SubsystemStatus(BaseModel):
    """Aggregated snapshot of a subsystem: recent readings + health flag."""

    model_config = ConfigDict(extra="forbid")

    subsystem: Subsystem
    readings: tuple[SensorReading, ...] = ()
    health: Health = Health.unknown
    updated_at: datetime


class Command(BaseModel):
    """A request to mutate a subsystem parameter.

    ``reason`` is a mandatory, caller-supplied justification recorded in the
    audit log; ``safety.py`` validates ``parameter`` and ``value`` against
    :data:`PARAMETER_SPECS`.
    """

    model_config = ConfigDict(extra="forbid")

    subsystem: Subsystem
    parameter: StrictStr
    value: NumericValue
    reason: StrictStr


class Decision(StrEnum):
    """Interlock / HITL outcome for a command, recorded in the audit log."""

    accepted = "accepted"
    rejected_unknown_subsystem = "rejected_unknown_subsystem"
    rejected_unknown_parameter = "rejected_unknown_parameter"
    rejected_range = "rejected_range"
    rejected_precondition = "rejected_precondition"
    pending_hitl = "pending_hitl"
    rejected_hitl = "rejected_hitl"


class CommandResult(BaseModel):
    """Result returned to the caller after interlock / HITL processing."""

    model_config = ConfigDict(extra="forbid")

    accepted: StrictBool
    decision: Decision
    audit_id: StrictStr
    timestamp: datetime


class AuditEntry(BaseModel):
    """Immutable audit-log row capturing who/what/when/decision for a command."""

    model_config = ConfigDict(extra="forbid")

    principal: StrictStr
    subsystem: Subsystem
    parameter: StrictStr
    value: NumericValue
    decision: Decision
    reason: StrictStr
    timestamp: datetime


class ParamKind(StrEnum):
    """Value type a parameter expects (drives strict coercion in ``safety.py``)."""

    int = "int"
    float = "float"


class Precondition(BaseModel):
    """A cross-subsystem sensor constraint that must hold before a command runs.

    The referenced ``subsystem``/``sensor_name`` reading must lie within
    ``[min, max]`` (inclusive) at validation time. ``safety.py`` evaluates each
    entry against the current simulator state.
    """

    model_config = ConfigDict(extra="forbid")

    subsystem: Subsystem
    sensor_name: StrictStr
    min: StrictFloat
    max: StrictFloat


class ParameterSpec(BaseModel):
    """Allow-list spec for one command parameter.

    ``safety.py`` coerces ``Command.value`` according to ``kind``, checks it
    against ``[min, max]`` (inclusive), and verifies every ``preconditions``
    entry against the live sensor state before accepting the command.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ParamKind
    min: StrictFloat
    max: StrictFloat
    unit: StrictStr
    preconditions: tuple[Precondition, ...] = ()


#: Per-subsystem parameter allow-list. Keys are :class:`Subsystem` members
#: (which hash identically to their string values, so ``PARAMETER_SPECS["vacuum"]``
#: also works). This is the only place parameter names, ranges, units, and
#: preconditions are defined — ``safety.py`` and ``server.py`` look up here.
PARAMETER_SPECS: dict[Subsystem, dict[str, ParameterSpec]] = {
    Subsystem.beamline_magnets: {
        "field_strength_setpoint": ParameterSpec(
            kind=ParamKind.float,
            min=0.0,
            max=3.5,
            unit="T",
            preconditions=(
                Precondition(
                    subsystem=Subsystem.cryogenics,
                    sensor_name="temperature",
                    min=0.0,
                    max=4.5,
                ),
                Precondition(
                    subsystem=Subsystem.vacuum,
                    sensor_name="pressure",
                    min=0.0,
                    max=1.0e-6,
                ),
            ),
        ),
    },
    Subsystem.rf_cavities: {
        "rf_amplitude": ParameterSpec(
            kind=ParamKind.float,
            min=0.0,
            max=10.0,
            unit="MV/m",
            preconditions=(
                Precondition(
                    subsystem=Subsystem.cryogenics,
                    sensor_name="temperature",
                    min=0.0,
                    max=4.5,
                ),
            ),
        ),
        "rf_phase": ParameterSpec(
            kind=ParamKind.float,
            min=-180.0,
            max=180.0,
            unit="deg",
            preconditions=(
                Precondition(
                    subsystem=Subsystem.cryogenics,
                    sensor_name="temperature",
                    min=0.0,
                    max=4.5,
                ),
            ),
        ),
    },
    Subsystem.cryogenics: {
        "temperature_setpoint": ParameterSpec(
            kind=ParamKind.float,
            min=2.0,
            max=300.0,
            unit="K",
            preconditions=(),
        ),
        "he_flow_rate": ParameterSpec(
            kind=ParamKind.float,
            min=0.0,
            max=50.0,
            unit="g/s",
            preconditions=(),
        ),
    },
    Subsystem.vacuum: {
        "pressure_setpoint": ParameterSpec(
            kind=ParamKind.float,
            min=1.0e-9,
            max=1.0e-3,
            unit="mbar",
            preconditions=(),
        ),
        "pump_speed": ParameterSpec(
            kind=ParamKind.float,
            min=0.0,
            max=100.0,
            unit="percent",
            preconditions=(),
        ),
    },
    Subsystem.power_supply: {
        "current_setpoint": ParameterSpec(
            kind=ParamKind.float,
            min=0.0,
            max=1000.0,
            unit="A",
            preconditions=(),
        ),
        "voltage_setpoint": ParameterSpec(
            kind=ParamKind.float,
            min=0.0,
            max=50.0,
            unit="V",
            preconditions=(),
        ),
    },
}
