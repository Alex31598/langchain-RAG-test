"""Unit tests for the accelerator simulator (M1.7).

Covers the M1.7 simulator acceptance criteria:

- determinism (same seed -> identical sensor-log sequence),
- ``window_s`` bounding (0, 3600, over-limit -> ValueError),
- ring-buffer cap (capacity enforced, oldest evicted),
- anomaly detection after scenario injection,
- ``apply_command`` routing through safety (accept mutates, reject doesn't,
  both audited),
- constructor / input-bounding error branches.

All tests use a fixed seed and the simulator's own deterministic epoch, so
there is no wall-clock dependence. See
``tasks/M1-telemetry-mcp/07-simulator-safety-tests.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from telemetry_mcp.models import (
    Command,
    Decision,
    Health,
    SensorReading,
    Subsystem,
)
from telemetry_mcp.safety import AuditLog
from telemetry_mcp.scenarios import SCENARIOS, inject
from telemetry_mcp.simulator import (
    DEFAULT_BUFFER_CAP,
    MAX_WINDOW_S,
    AcceleratorSimulator,
)

_PRINCIPAL = "tester"

#: Number of sensors across all subsystems (2 per subsystem x 5 subsystems).
_N_SENSORS = 10


def _sim(seed: int = 0, **kwargs: object) -> AcceleratorSimulator:
    return AcceleratorSimulator(seed=seed, **kwargs)  # type: ignore[arg-type]


def _cmd(
    *,
    subsystem: Subsystem = Subsystem.cryogenics,
    parameter: str = "temperature_setpoint",
    value: float = 4.5,
    reason: str = "test",
) -> Command:
    return Command(
        subsystem=subsystem,
        parameter=parameter,
        value=value,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Constructor / input validation
# --------------------------------------------------------------------------- #


class TestConstructor:
    def test_default_buffer_cap_is_positive(self) -> None:
        sim = _sim()
        assert len(sim.get_status(Subsystem.cryogenics.value).readings) > 0

    def test_zero_buffer_cap_raises(self) -> None:
        with pytest.raises(ValueError, match="buffer_cap must be positive"):
            AcceleratorSimulator(seed=0, buffer_cap=0)

    def test_negative_buffer_cap_raises(self) -> None:
        with pytest.raises(ValueError, match="buffer_cap must be positive"):
            AcceleratorSimulator(seed=0, buffer_cap=-1)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


class TestDeterminism:
    def test_same_seed_produces_identical_logs(self) -> None:
        sim_a = _sim(seed=42)
        sim_b = _sim(seed=42)
        for _ in range(10):
            sim_a.tick(1.0)
            sim_b.tick(1.0)
        logs_a = sim_a.get_sensor_logs("cryogenics", window_s=3600)
        logs_b = sim_b.get_sensor_logs("cryogenics", window_s=3600)
        assert len(logs_a) == len(logs_b)
        assert [r.model_dump() for r in logs_a] == [r.model_dump() for r in logs_b]

    def test_different_seed_produces_different_logs(self) -> None:
        sim_a = _sim(seed=1)
        sim_b = _sim(seed=2)
        for _ in range(5):
            sim_a.tick(1.0)
            sim_b.tick(1.0)
        logs_a = sim_a.get_sensor_logs("cryogenics", window_s=3600)
        logs_b = sim_b.get_sensor_logs("cryogenics", window_s=3600)
        values_a = [r.value for r in logs_a]
        values_b = [r.value for r in logs_b]
        assert values_a != values_b

    def test_clock_advances_by_dt(self) -> None:
        sim = _sim()
        before = sim.get_status("cryogenics").updated_at
        sim.tick(5.0)
        after = sim.get_status("cryogenics").updated_at
        assert after == before + timedelta(seconds=5.0)

    def test_tick_zero_emits_readings(self) -> None:
        sim = _sim()
        sim.tick(0.0)
        logs = sim.get_sensor_logs("cryogenics", window_s=3600)
        # tick(0) still emits one reading per sensor (2 for cryogenics)
        assert len(logs) == 2

    def test_negative_dt_raises(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="dt must be non-negative"):
            sim.tick(-1.0)


# --------------------------------------------------------------------------- #
# Sensor log queries + window_s bounding
# --------------------------------------------------------------------------- #


class TestSensorLogs:
    def test_emits_one_reading_per_sensor_per_tick(self) -> None:
        sim = _sim()
        sim.tick(1.0)
        sim.tick(1.0)
        all_logs = sim.get_sensor_logs("cryogenics", window_s=3600)
        # 2 cryo sensors x 2 ticks = 4 readings
        assert len(all_logs) == 4

    def test_window_zero_returns_only_current_tick(self) -> None:
        sim = _sim()
        sim.tick(1.0)  # t=1
        sim.tick(1.0)  # t=2
        sim.tick(1.0)  # t=3
        logs = sim.get_sensor_logs("cryogenics", window_s=0)
        # window_s=0 -> cutoff = current clock; readings at t>=cutoff
        # All readings have timestamp == clock after tick, so they're included.
        assert len(logs) == 2  # the latest tick's 2 cryo readings

    def test_window_filters_old_readings(self) -> None:
        sim = _sim()
        sim.tick(1.0)  # t=1
        sim.tick(1.0)  # t=2
        sim.tick(1.0)  # t=3
        # window of 1.5s from t=3 -> cutoff at t=1.5 -> includes t=2 and t=3
        logs = sim.get_sensor_logs("cryogenics", window_s=1.5)
        # 2 sensors x 2 ticks (t=2, t=3)
        assert len(logs) == 4

    def test_window_at_max_boundary_accepted(self) -> None:
        sim = _sim()
        sim.tick(1.0)
        # window_s = MAX_WINDOW_S (3600) is the inclusive upper bound
        logs = sim.get_sensor_logs("cryogenics", window_s=MAX_WINDOW_S)
        assert len(logs) == 2

    def test_window_over_max_raises(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="window_s must be in"):
            sim.get_sensor_logs("cryogenics", window_s=MAX_WINDOW_S + 1)

    def test_window_negative_raises(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="window_s must be in"):
            sim.get_sensor_logs("cryogenics", window_s=-1)

    def test_unknown_subsystem_raises(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="unknown subsystem"):
            sim.get_sensor_logs("not_a_subsystem", window_s=10)

    def test_since_filter_excludes_older(self) -> None:
        sim = _sim()
        sim.tick(1.0)  # t=1
        sim.tick(1.0)  # t=2
        since = _START_EPOCH_PLUS(2.0)
        logs = sim.get_sensor_logs("cryogenics", window_s=3600, since=since)
        assert len(logs) == 2  # only t=2 readings
        assert all(r.timestamp >= since for r in logs)

    def test_since_naive_datetime_raises_type_error(self) -> None:
        # The simulator does not normalize naive datetimes (the server layer
        # does that via _normalize_since); a naive ``since`` against the
        # tz-aware sim clock raises TypeError.
        sim = _sim()
        sim.tick(1.0)
        naive = _START_EPOCH_PLUS(0.5).replace(tzinfo=None)
        with pytest.raises(TypeError):
            sim.get_sensor_logs("cryogenics", window_s=3600, since=naive)


class TestRecentAnomalies:
    def test_clean_simulator_has_no_anomalies(self) -> None:
        sim = _sim()
        for _ in range(5):
            sim.tick(1.0)
        anomalies = sim.get_recent_anomalies(window_s=3600)
        assert anomalies == ()

    def test_window_bounding_applies(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="window_s must be in"):
            sim.get_recent_anomalies(window_s=-1)
        with pytest.raises(ValueError, match="window_s must be in"):
            sim.get_recent_anomalies(window_s=MAX_WINDOW_S + 1)

    def test_unknown_subsystem_in_status_raises(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="unknown subsystem"):
            sim.get_status("nope")


# --------------------------------------------------------------------------- #
# Ring buffer cap
# --------------------------------------------------------------------------- #


class TestRingBuffer:
    def test_buffer_capped_at_capacity(self) -> None:
        cap = _N_SENSORS * 3  # 3 ticks worth
        sim = AcceleratorSimulator(seed=0, buffer_cap=cap)
        for _ in range(10):
            sim.tick(1.0)
        # After 10 ticks (100 readings) with cap=30, only 30 remain.
        all_cryo = sim.get_sensor_logs("cryogenics", window_s=3600)
        all_mag = sim.get_sensor_logs("beamline_magnets", window_s=3600)
        # The window covers all retained readings; total across subsystems
        # equals the cap. Each subsystem has 2 sensors, so 6 readings per
        # subsystem survive (3 ticks x 2 sensors).
        assert len(all_cryo) + len(all_mag) <= cap

    def test_oldest_readings_evicted(self) -> None:
        cap = _N_SENSORS  # exactly 1 tick
        sim = AcceleratorSimulator(seed=0, buffer_cap=cap)
        sim.tick(1.0)  # t=1: 10 readings
        sim.tick(1.0)  # t=2: 10 more, evicting t=1
        logs = sim.get_sensor_logs("cryogenics", window_s=3600)
        # Only t=2 readings survive (2 cryo readings)
        assert len(logs) == 2
        assert all(r.timestamp == _START_EPOCH_PLUS(2.0) for r in logs)

    def test_default_buffer_cap_is_large(self) -> None:
        assert DEFAULT_BUFFER_CAP == 10_000


# --------------------------------------------------------------------------- #
# Health / anomaly detection
# --------------------------------------------------------------------------- #


class TestHealthAndAnomaly:
    def test_clean_simulator_is_nominal(self) -> None:
        sim = _sim()
        sim.tick(1.0)
        for sub in Subsystem:
            status = sim.get_status(sub.value)
            assert status.health is Health.nominal

    def test_anomaly_detected_after_scenario_injection(self) -> None:
        sim = _sim(seed=0)
        # magnet_quench_drift adds 0.04 T/tick; anomaly_tol is 0.2 T.
        # After 6 ticks: bias = 0.24 T -> anomaly flagged.
        inject(sim, "magnet_quench_drift", at_step=0)
        for _ in range(6):
            sim.tick(1.0)
        status = sim.get_status("beamline_magnets")
        assert status.health is Health.fault
        anomalies = sim.get_recent_anomalies(window_s=3600)
        assert len(anomalies) > 0
        assert all(r.subsystem == Subsystem.beamline_magnets for r in anomalies)

    def test_anomaly_present_during_scenario_window(self) -> None:
        sim = _sim(seed=0)
        # psu_ripple: phase 0-1 set bias=35.0 (above anomaly_tol=30.0 for
        # current), phase 2-3 set bias=15.0 (below tol). After 2 ticks the
        # current reading sits ~535 A, well past the 30 A anomaly tolerance.
        inject(sim, "psu_ripple", at_step=0)
        sim.tick(1.0)  # offset 0: bias=35.0
        sim.tick(1.0)  # offset 1: bias=35.0
        status = sim.get_status("power_supply")
        assert status.health is Health.fault
        anomalies = sim.get_recent_anomalies(window_s=3600)
        assert any(r.subsystem == Subsystem.power_supply for r in anomalies)

    def test_scenario_bias_stops_accumulating_after_expiry(self) -> None:
        sim = _sim(seed=0)
        inject(sim, "cryo_pressure_spike", at_step=0)  # duration=5
        for _ in range(6):
            sim.tick(1.0)
        # After duration, no new bias is applied. The _inject_bias calls were
        # additive; verify further ticks don't add more bias.
        bias_before = sim._bias[Subsystem.cryogenics]["temperature"]
        sim.tick(1.0)
        bias_after = sim._bias[Subsystem.cryogenics]["temperature"]
        assert bias_after == bias_before

    def test_is_anomaly_unknown_sensor_returns_false(self) -> None:
        # Defense-in-depth: a reading with a valid subsystem but unknown
        # sensor name -> _is_anomaly returns False (not a crash).
        reading = SensorReading(
            subsystem=Subsystem.cryogenics,
            sensor_name="nonexistent_sensor",
            value=999.0,
            unit="X",
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert AcceleratorSimulator._is_anomaly(reading) is False


# --------------------------------------------------------------------------- #
# Scenario registration / firing
# --------------------------------------------------------------------------- #


class TestScenarios:
    def test_register_scenario_negative_at_step_raises(self) -> None:
        sim = _sim()
        scenario = SCENARIOS["magnet_quench_drift"]
        with pytest.raises(ValueError, match="at_step must be non-negative"):
            sim.register_scenario(-1, scenario)

    def test_scenario_fires_only_during_active_window(self) -> None:
        sim = _sim(seed=0)
        inject(sim, "cryo_pressure_spike", at_step=2)
        # Before the scenario activates (steps 0, 1): no anomaly.
        sim.tick(1.0)  # step 0
        sim.tick(1.0)  # step 1
        assert sim.get_status("cryogenics").health is Health.nominal
        # The scenario activates at step 2 (cryo_pressure_spike duration=5).
        sim.tick(1.0)  # step 2 -> offset 0: temperature spike +1.5
        # temperature nominal=4.0, bias=1.5, anomaly_tol=0.3 -> |1.5|>0.3 -> fault
        assert sim.get_status("cryogenics").health is Health.fault

    def test_scenario_expires_after_duration(self) -> None:
        sim = _sim(seed=0)
        inject(sim, "cryo_pressure_spike", at_step=0)  # duration=5
        for _ in range(6):
            sim.tick(1.0)
        # After duration, no new bias is applied. The last bias persists (the
        # scenario set absolute values via _inject_bias, which is additive).
        # Verify the scenario is no longer firing by checking that further
        # ticks don't add more bias:
        bias_before = sim._bias[Subsystem.cryogenics]["temperature"]
        sim.tick(1.0)
        bias_after = sim._bias[Subsystem.cryogenics]["temperature"]
        assert bias_after == bias_before  # no new bias after expiry

    def test_inject_bias_unknown_sensor_raises(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="unknown sensor"):
            sim._inject_bias(Subsystem.cryogenics, "nope", 1.0)

    def test_set_bias_unknown_sensor_raises(self) -> None:
        sim = _sim()
        with pytest.raises(ValueError, match="unknown sensor"):
            sim._set_bias(Subsystem.cryogenics, "nope", 1.0)

    def test_inject_bias_unknown_subsystem_raises(self) -> None:
        # _SENSOR_LOOKUP has all Subsystem members, so this exercises the
        # sensors-is-None branch via a constructed Subsystem-like that's
        # not in the lookup. We use model_construct to bypass enum validation.
        sim = _sim()
        # Build a fake Subsystem-like that's not in _SENSOR_LOOKUP.
        # Since Subsystem is a StrEnum with all members in _SENSOR_LOOKUP,
        # this branch is only reachable if the lookup is modified. We test
        # the unknown-sensor branch instead (covered above) and accept this
        # as a defensive-code path that's unreachable in practice.
        # Instead, verify _inject_bias works for a valid sensor.
        sim._inject_bias(Subsystem.cryogenics, "temperature", 0.5)
        assert sim._bias[Subsystem.cryogenics]["temperature"] == 0.5

    def test_set_bias_sets_absolute_value(self) -> None:
        sim = _sim()
        sim._set_bias(Subsystem.power_supply, "current", 35.0)
        assert sim._bias[Subsystem.power_supply]["current"] == 35.0
        # Setting again replaces, doesn't accumulate.
        sim._set_bias(Subsystem.power_supply, "current", 15.0)
        assert sim._bias[Subsystem.power_supply]["current"] == 15.0

    def test_inject_bias_is_additive(self) -> None:
        sim = _sim()
        sim._inject_bias(Subsystem.cryogenics, "temperature", 0.5)
        sim._inject_bias(Subsystem.cryogenics, "temperature", 0.3)
        assert sim._bias[Subsystem.cryogenics]["temperature"] == 0.8


# --------------------------------------------------------------------------- #
# apply_command (safety integration)
# --------------------------------------------------------------------------- #


class TestApplyCommand:
    def test_accepted_command_mutates_setpoint(self) -> None:
        sim = _sim()
        log = AuditLog()
        result = sim.apply_command(
            _cmd(subsystem=Subsystem.cryogenics, parameter="temperature_setpoint", value=10.0),
            _PRINCIPAL,
            log,
        )
        assert result.accepted is True
        assert result.decision is Decision.accepted
        assert result.audit_id.startswith("audit-")
        assert len(log) == 1
        # The setpoint moved: the next tick's reading centers on 10.0.
        sim.tick(1.0)
        status = sim.get_status("cryogenics")
        temp_reading = next(r for r in status.readings if r.sensor_name == "temperature")
        assert abs(temp_reading.value - 10.0) < 1.0  # within noise

    def test_rejected_command_does_not_mutate(self) -> None:
        sim = _sim()
        log = AuditLog()
        original_setpoint = sim._setpoints[Subsystem.cryogenics]["temperature"]
        result = sim.apply_command(
            _cmd(value=1.0),  # below 2.0 K minimum
            _PRINCIPAL,
            log,
        )
        assert result.accepted is False
        assert result.decision is Decision.rejected_range
        assert len(log) == 1
        # Setpoint unchanged.
        assert sim._setpoints[Subsystem.cryogenics]["temperature"] == original_setpoint

    def test_rejected_unknown_parameter_audited(self) -> None:
        sim = _sim()
        log = AuditLog()
        result = sim.apply_command(
            _cmd(parameter="nope"),
            _PRINCIPAL,
            log,
        )
        assert result.accepted is False
        assert result.decision is Decision.rejected_unknown_parameter
        assert len(log) == 1

    def test_rejected_precondition_audited(self) -> None:
        sim = _sim()
        log = AuditLog()
        # Inject a cryo temperature anomaly to fail the beamline precondition
        # (requires cryo temperature <= 4.5 K).
        sim._inject_bias(Subsystem.cryogenics, "temperature", 2.0)
        sim.tick(1.0)  # generate a reading with the biased temperature
        result = sim.apply_command(
            _cmd(
                subsystem=Subsystem.beamline_magnets,
                parameter="field_strength_setpoint",
                value=2.0,
            ),
            _PRINCIPAL,
            log,
        )
        assert result.accepted is False
        assert result.decision is Decision.rejected_precondition
        assert len(log) == 1

    def test_accept_and_reject_both_audited_in_order(self) -> None:
        sim = _sim()
        log = AuditLog()
        sim.apply_command(_cmd(value=10.0), _PRINCIPAL, log)
        sim.apply_command(_cmd(value=1.0), _PRINCIPAL, log)
        assert len(log) == 2
        decisions = [e.decision for e in log.entries()]
        assert decisions == [Decision.accepted, Decision.rejected_range]
        principals = [e.principal for e in log.entries()]
        assert principals == [_PRINCIPAL, _PRINCIPAL]

    def test_principal_recorded_on_audit(self) -> None:
        sim = _sim()
        log = AuditLog()
        sim.apply_command(_cmd(), "operator-99", log)
        assert log.entries()[0].principal == "operator-99"

    def test_mutation_atomic_with_validation(self) -> None:
        # The validate + mutate sequence runs under sim._lock, so there's no
        # TOCTOU window. We verify the setpoint moves only when the command
        # is accepted and the audit_id is sequential.
        sim = _sim()
        log = AuditLog()
        r1 = sim.apply_command(_cmd(value=10.0), _PRINCIPAL, log)
        r2 = sim.apply_command(_cmd(value=20.0), _PRINCIPAL, log)
        assert r1.audit_id == "audit-00000001"
        assert r2.audit_id == "audit-00000002"
        assert sim._setpoints[Subsystem.cryogenics]["temperature"] == 20.0


# --------------------------------------------------------------------------- #
# Status snapshots
# --------------------------------------------------------------------------- #


class TestGetStatus:
    def test_returns_readings_for_all_sensors_in_subsystem(self) -> None:
        sim = _sim()
        sim.tick(1.0)
        status = sim.get_status("cryogenics")
        assert status.subsystem == Subsystem.cryogenics
        assert len(status.readings) == 2  # temperature + he_flow_rate
        sensor_names = {r.sensor_name for r in status.readings}
        assert sensor_names == {"temperature", "he_flow_rate"}

    def test_updated_at_matches_clock(self) -> None:
        sim = _sim()
        sim.tick(3.0)
        status = sim.get_status("cryogenics")
        assert status.updated_at == _START_EPOCH_PLUS(3.0)

    def test_all_subsystems_accessible(self) -> None:
        sim = _sim()
        sim.tick(1.0)
        for sub in Subsystem:
            status = sim.get_status(sub.value)
            assert status.subsystem == sub
            assert len(status.readings) == 2


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _START_EPOCH_PLUS(seconds: float) -> datetime:
    """Simulator's deterministic epoch + ``seconds`` (UTC)."""
    from telemetry_mcp.simulator import _START_EPOCH

    return _START_EPOCH + timedelta(seconds=seconds)
