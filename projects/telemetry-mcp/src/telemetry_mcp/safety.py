"""Command interlocks: range/precondition validation, audit log, HITL gate.

This module is the sole authority that decides whether a :class:`Command` may
mutate simulator state.

M1.2 implements the **pure interlock core** — :func:`validate_command`, which
checks a command against :data:`PARAMETER_SPECS` (range + cross-subsystem
preconditions) and returns a :class:`Decision`. The simulator's
``apply_command`` routes through this function so that no mutation happens
without safety's approval (see ``tasks/M1-telemetry-mcp/02-simulator.md``).

M1.4 extends this module with the persistent append-only audit log
(:class:`AuditEntry` per accepted *and* rejected command, recording the caller
``principal``) and returns a principal-aware :class:`CommandResult`. M1.6 adds
the human-in-the-loop gate for non-allow-listed parameters. See
``tasks/M1-telemetry-mcp/04-safety-interlocks.md`` and ``06-hitl-gate.md``.

The validation logic is intentionally pure (no state mutation, no I/O) so it is
trivially testable and safe to call under the simulator's lock.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias

from telemetry_mcp.models import (
    PARAMETER_SPECS,
    Command,
    Decision,
    ParamKind,
    Subsystem,
)

__all__ = ["validate_command", "SensorSnapshot"]

#: Snapshot of the latest sensor values a command's preconditions are checked
#: against. The simulator builds this from its current state and passes it in.
SensorSnapshot: TypeAlias = Mapping[Subsystem, Mapping[str, float]]


def validate_command(cmd: Command, sensor_snapshot: SensorSnapshot) -> Decision:
    """Validate ``cmd`` against the parameter allow-lists and live sensor state.

    Checks, in order:
    1. the subsystem has a parameter spec table,
    2. the parameter is in the subsystem's allow-list,
    3. the value is within the spec's ``[min, max]`` range (with kind check),
    4. every precondition holds against ``sensor_snapshot``.

    Returns the :class:`Decision` (``accepted`` or a specific rejection). This
    function is pure: it never raises and never mutates state. The simulator
    wraps the returned decision into a :class:`CommandResult`.
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
