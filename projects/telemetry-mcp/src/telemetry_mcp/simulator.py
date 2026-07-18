"""Deterministic accelerator state machine + sensor log emission.

``AcceleratorSimulator`` models the five subsystems declared in
:data:`telemetry_mcp.models.SUBSYSTEMS`, advances their state on each
:func:`tick`, emits timestamped :class:`SensorReading` log lines into a bounded
ring buffer, and mutates state via :meth:`apply_command` — which routes the
range + precondition decision through :func:`telemetry_mcp.safety.validate_command`
(no mutation happens without safety's approval, and every decision is audited
with the caller principal).

See ``tasks/PLAN.md`` §4.1, §4.2 and ``tasks/M1-telemetry-mcp/02-simulator.md``.
"""

from __future__ import annotations

import random
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from telemetry_mcp.models import (
    SUBSYSTEMS,
    Command,
    CommandResult,
    Health,
    SensorReading,
    Subsystem,
    SubsystemStatus,
)
from telemetry_mcp.safety import AuditLog, validate_command

if TYPE_CHECKING:
    from telemetry_mcp.scenarios import Scenario

__all__ = ["AcceleratorSimulator", "MAX_WINDOW_S", "DEFAULT_BUFFER_CAP"]

#: Upper bound on ``window_s`` for log/anomaly queries (PLAN §4.2).
MAX_WINDOW_S: int = 3600

#: Default ring-buffer capacity (number of retained readings across all sensors).
DEFAULT_BUFFER_CAP: int = 10_000

#: Fixed simulation epoch so timestamps are deterministic under a given seed.
_START_EPOCH: datetime = datetime(2024, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _SensorDef:
    """Static definition of one sensor: unit, nominal, noise, anomaly band."""

    name: str
    unit: str
    nominal: float
    noise_std: float
    #: A reading is flagged anomalous when ``|value - nominal| > anomaly_tol``.
    anomaly_tol: float


#: Per-subsystem sensor definitions. Sensor names referenced in
#: ``PARAMETER_SPECS`` preconditions (``cryogenics.temperature``,
#: ``vacuum.pressure``) MUST match exactly here — they are the join keys between
#: the allow-list spec and the live sensor state.
_SENSOR_DEFS: dict[Subsystem, tuple[_SensorDef, ...]] = {
    Subsystem.beamline_magnets: (
        _SensorDef("field_strength", "T", 1.5, 0.01, 0.2),
        _SensorDef("current", "A", 250.0, 1.0, 20.0),
    ),
    Subsystem.rf_cavities: (
        _SensorDef("rf_amplitude", "MV/m", 5.0, 0.05, 1.0),
        _SensorDef("rf_phase", "deg", 0.0, 0.5, 10.0),
    ),
    Subsystem.cryogenics: (
        _SensorDef("temperature", "K", 4.0, 0.02, 0.3),
        _SensorDef("he_flow_rate", "g/s", 25.0, 0.2, 3.0),
    ),
    Subsystem.vacuum: (
        _SensorDef("pressure", "mbar", 1.0e-7, 1.0e-8, 5.0e-8),
        _SensorDef("pump_speed", "percent", 80.0, 0.5, 5.0),
    ),
    Subsystem.power_supply: (
        _SensorDef("current", "A", 500.0, 2.0, 30.0),
        _SensorDef("voltage", "V", 24.0, 0.1, 2.0),
    ),
}

#: O(1) sensor lookup keyed by ``(subsystem, sensor_name)``.
_SENSOR_LOOKUP: dict[Subsystem, dict[str, _SensorDef]] = {
    sub: {d.name: d for d in defs} for sub, defs in _SENSOR_DEFS.items()
}

#: Maps a command parameter (a key in ``PARAMETER_SPECS``) to the sensor whose
#: setpoint it controls. ``apply_command`` uses this only after safety accepts.
_PARAM_TO_SENSOR: dict[Subsystem, dict[str, str]] = {
    Subsystem.beamline_magnets: {"field_strength_setpoint": "field_strength"},
    Subsystem.rf_cavities: {"rf_amplitude": "rf_amplitude", "rf_phase": "rf_phase"},
    Subsystem.cryogenics: {
        "temperature_setpoint": "temperature",
        "he_flow_rate": "he_flow_rate",
    },
    Subsystem.vacuum: {"pressure_setpoint": "pressure", "pump_speed": "pump_speed"},
    Subsystem.power_supply: {
        "current_setpoint": "current",
        "voltage_setpoint": "voltage",
    },
}


class AcceleratorSimulator:
    """Deterministic, thread-safe simulated particle-accelerator state machine.

    Same ``seed`` produces an identical sensor-log sequence (determinism). The
    ring buffer is capped at ``buffer_cap`` readings. All public methods take a
    ``threading.Lock`` so the instance is safe to share across FastMCP workers.
    """

    def __init__(self, *, seed: int, buffer_cap: int = DEFAULT_BUFFER_CAP) -> None:
        if buffer_cap <= 0:
            raise ValueError("buffer_cap must be positive")
        self._rng = random.Random(seed)
        self._buffer: deque[SensorReading] = deque(maxlen=buffer_cap)
        self._clock: datetime = _START_EPOCH
        self._lock = threading.Lock()
        # Per-sensor setpoints (what a command moves) seeded to nominal.
        self._setpoints: dict[Subsystem, dict[str, float]] = {
            sub: {d.name: d.nominal for d in defs} for sub, defs in _SENSOR_DEFS.items()
        }
        # Per-sensor fault bias injected by scenarios (additive on top of the
        # setpoint during reading generation). Scenarios modify this via
        # ``_inject_bias``; it is always zero in a clean simulator.
        self._bias: dict[Subsystem, dict[str, float]] = {
            sub: {d.name: 0.0 for d in defs} for sub, defs in _SENSOR_DEFS.items()
        }
        # Latest reading per sensor (initial conditions at the epoch, not in the
        # log buffer). Used for status snapshots and precondition checks.
        self._latest_reading: dict[Subsystem, dict[str, SensorReading]] = {
            sub: {
                d.name: SensorReading(
                    subsystem=sub,
                    sensor_name=d.name,
                    value=d.nominal,
                    unit=d.unit,
                    timestamp=self._clock,
                )
                for d in defs
            }
            for sub, defs in _SENSOR_DEFS.items()
        }
        # Step counter and registered fault scenarios (M1.3).
        self._step_count: int = 0
        self._scenarios: list[tuple[int, Scenario]] = []

    # ------------------------------------------------------------------ state

    def tick(self, dt: float) -> None:
        """Advance the simulation by ``dt`` seconds and emit one reading per sensor."""
        if dt < 0:
            raise ValueError("dt must be non-negative")
        with self._lock:
            self._clock = self._clock + timedelta(seconds=dt)
            self._fire_scenarios()
            self._step_count += 1
            # Iterate in fixed insertion order so the RNG stream is deterministic.
            for sub, defs in _SENSOR_DEFS.items():
                for d in defs:
                    value = (
                        self._setpoints[sub][d.name]
                        + self._bias[sub][d.name]
                        + self._rng.gauss(0.0, d.noise_std)
                    )
                    reading = SensorReading(
                        subsystem=sub,
                        sensor_name=d.name,
                        value=value,
                        unit=d.unit,
                        timestamp=self._clock,
                    )
                    self._buffer.append(reading)
                    self._latest_reading[sub][d.name] = reading

    # ----------------------------------------------------------------- queries

    def get_status(self, subsystem: str) -> SubsystemStatus:
        """Aggregated snapshot of one subsystem: latest readings + health flag."""
        sub = self._coerce_subsystem(subsystem)
        with self._lock:
            latest = self._latest_reading[sub]
            readings = tuple(
                latest[d.name] for d in _SENSOR_DEFS[sub] if d.name in latest
            )
            return SubsystemStatus(
                subsystem=sub,
                readings=readings,
                health=self._compute_health(sub),
                updated_at=self._clock,
            )

    def get_sensor_logs(
        self,
        subsystem: str,
        window_s: float,
        since: datetime | None = None,
    ) -> tuple[SensorReading, ...]:
        """Recent readings for one subsystem within ``window_s`` seconds."""
        sub = self._coerce_subsystem(subsystem)
        self._check_window(window_s)
        with self._lock:
            cutoff = self._clock - timedelta(seconds=window_s)
            return tuple(
                r
                for r in self._buffer
                if r.subsystem == sub
                and r.timestamp >= cutoff
                and (since is None or r.timestamp >= since)
            )

    def get_recent_anomalies(
        self,
        window_s: float,
        since: datetime | None = None,
    ) -> tuple[SensorReading, ...]:
        """Pre-flagged outlier readings within ``window_s`` seconds."""
        self._check_window(window_s)
        with self._lock:
            cutoff = self._clock - timedelta(seconds=window_s)
            return tuple(
                r
                for r in self._buffer
                if r.timestamp >= cutoff
                and (since is None or r.timestamp >= since)
                and self._is_anomaly(r)
            )

    # ------------------------------------------------------------ commands

    def apply_command(
        self, cmd: Command, principal: str, audit_log: AuditLog
    ) -> CommandResult:
        """Validate ``cmd`` via safety, audit the decision, and mutate iff accepted.

        Builds the live sensor snapshot under ``self._lock``, routes the command
        through :func:`telemetry_mcp.safety.validate_command` (which runs the
        pure interlocks, writes an :class:`AuditEntry` with ``principal`` to
        ``audit_log``, and returns a :class:`CommandResult`), and applies the
        setpoint mutation only when ``result.accepted``. The validate + audit +
        mutate sequence runs atomically under the lock so no other thread can
        change the sensor state between the precondition check and the mutation
        (no TOCTOU window). This method never rejects a command on its own --
        all interlock decisions come from :mod:`telemetry_mcp.safety`.
        """
        with self._lock:
            snapshot: dict[Subsystem, dict[str, float]] = {
                sub: {name: r.value for name, r in vals.items()}
                for sub, vals in self._latest_reading.items()
            }
            result = validate_command(cmd, principal, snapshot, audit_log)
            if result.accepted:
                sensor_name = _PARAM_TO_SENSOR[cmd.subsystem][cmd.parameter]
                self._setpoints[cmd.subsystem][sensor_name] = float(cmd.value)
            return result

    # ------------------------------------------------------------ helpers

    def register_scenario(self, at_step: int, scenario: Scenario) -> None:
        """Register a fault scenario to activate at step ``at_step``.

        Called by :func:`telemetry_mcp.scenarios.inject`. The scenario's
        ``apply`` callback fires on each tick whose step count falls in
        ``[at_step, at_step + scenario.duration)``.
        """
        if at_step < 0:
            raise ValueError("at_step must be non-negative")
        with self._lock:
            self._scenarios.append((at_step, scenario))

    def _inject_bias(self, subsystem: Subsystem, sensor_name: str, delta: float) -> None:
        """Internal hook for scenarios: add ``delta`` to a sensor's fault bias.

        This bypasses the safety interlocks *by design* — scenarios inject
        sensor drift, never destructive commands. Must be called under
        ``self._lock`` (i.e. from within :meth:`tick` or ``scenario.apply``).
        """
        sensors = _SENSOR_LOOKUP.get(subsystem)
        if sensors is None or sensor_name not in sensors:
            raise ValueError(f"unknown sensor: {subsystem.value}/{sensor_name}")
        self._bias[subsystem][sensor_name] += float(delta)

    def _set_bias(self, subsystem: Subsystem, sensor_name: str, value: float) -> None:
        """Internal hook for scenarios: set a sensor's fault bias to ``value``.

        Unlike :meth:`_inject_bias` (additive, for drifts), this sets an
        absolute bias each tick — used by spikes and ripple scenarios whose
        bias must oscillate rather than accumulate. Must be called under
        ``self._lock``.
        """
        sensors = _SENSOR_LOOKUP.get(subsystem)
        if sensors is None or sensor_name not in sensors:
            raise ValueError(f"unknown sensor: {subsystem.value}/{sensor_name}")
        self._bias[subsystem][sensor_name] = float(value)

    def _fire_scenarios(self) -> None:
        """Fire all active registered scenarios for the current step.

        Must be called under ``self._lock`` (from :meth:`tick`).
        """
        for at_step, scenario in self._scenarios:
            offset = self._step_count - at_step
            if 0 <= offset < scenario.duration:
                scenario.apply(self, offset)

    @staticmethod
    def _coerce_subsystem(subsystem: str) -> Subsystem:
        """Validate a subsystem string against :data:`SUBSYSTEMS` before any buffer access."""
        if subsystem not in SUBSYSTEMS:
            raise ValueError(f"unknown subsystem: {subsystem!r}")
        return Subsystem(subsystem)

    @staticmethod
    def _check_window(window_s: float) -> None:
        if window_s < 0 or window_s > MAX_WINDOW_S:
            raise ValueError(
                f"window_s must be in [0, {MAX_WINDOW_S}], got {window_s}"
            )

    def _compute_health(self, sub: Subsystem) -> Health:
        latest = self._latest_reading[sub]
        for d in _SENSOR_DEFS[sub]:
            reading = latest.get(d.name)
            if reading is not None and self._is_anomaly(reading):
                return Health.fault
        return Health.nominal

    @staticmethod
    def _is_anomaly(reading: SensorReading) -> bool:
        defs = _SENSOR_LOOKUP.get(reading.subsystem)
        if defs is None:
            return False
        d = defs.get(reading.sensor_name)
        if d is None:
            return False
        return abs(reading.value - d.nominal) > d.anomaly_tol
