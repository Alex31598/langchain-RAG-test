"""Injectable fault scenarios with realistic log signatures.

Each scenario is a named, reproducible fault injection that perturbs sensor
readings via the simulator's internal ``_inject_bias`` hook — **not** the
public ``execute_command`` path. Scenarios bypass the safety interlocks by
design, but only inject sensor drift (never destructive commands). The agent
(M2) must diagnose the resulting anomalous log signatures.

Scenario descriptions are plain observation text — clearly synthetic, no shell
or control content, no executable instructions. They are how
adversarial-looking text could enter the log stream (PLAN §4.3); the agent
prompt fences all log lines as ``<observation>`` blocks (M2.4).

See ``tasks/PLAN.md`` §4.1, §4.4 and ``tasks/M1-telemetry-mcp/03-scenarios.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from telemetry_mcp.models import Subsystem

if TYPE_CHECKING:
    from telemetry_mcp.simulator import AcceleratorSimulator

__all__ = ["Scenario", "SCENARIOS", "inject"]


def _validate_description(text: str) -> str:
    """Ensure a scenario description is plain printable text.

    Rejects control characters (including ``\\r``, ``\\n``, ``\\t``) to prevent
    log-injection, and shell metacharacters (``;|&$<>`\n``) to keep the
    description inert even if it is ever surfaced to a shell context. The
    descriptions are hardcoded here (not user input), so this is a defense-
    in-depth check.
    """
    forbidden = set("\x00\r\n\t;|&$<>`\"'\\!")
    for ch in text:
        if ord(ch) < 32 or ord(ch) == 127 or ch in forbidden:
            raise ValueError(f"unsafe character in scenario description: {ch!r}")
    return text


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named, reproducible fault injection.

    ``apply`` is called on each tick whose step count falls in
    ``[at_step, at_step + duration)``. The first argument is the simulator
    (so the scenario can call ``sim._inject_bias``); the second is the
    step offset (0-based) since activation.
    """

    name: str
    subsystem: Subsystem
    duration: int
    description: str
    apply: Callable[[AcceleratorSimulator, int], None]


# --------------------------------------------------------------------------- #
# Scenario apply functions
# --------------------------------------------------------------------------- #
# Each function is called once per active tick with (simulator, step_offset).
# Bias deltas are cumulative (additive) across ticks — this produces drifts,
# spikes, and cascades. Determinism is preserved: no RNG is used here.


def _apply_magnet_quench_drift(sim: AcceleratorSimulator, step_offset: int) -> None:
    """Gradual beamline-magnet field drift + current rise (resistive quench)."""
    sim._inject_bias(Subsystem.beamline_magnets, "field_strength", 0.04)
    if step_offset >= 5:
        sim._inject_bias(Subsystem.beamline_magnets, "current", 3.0)


def _apply_rf_cavity_overheating(sim: AcceleratorSimulator, step_offset: int) -> None:
    """RF amplitude drift (thermal) + delayed phase drift (thermal expansion)."""
    sim._inject_bias(Subsystem.rf_cavities, "rf_amplitude", 0.08)
    if step_offset >= 8:
        sim._inject_bias(Subsystem.rf_cavities, "rf_phase", 1.0)


def _apply_cryo_pressure_spike(sim: AcceleratorSimulator, step_offset: int) -> None:
    """Sharp cryo temperature spike + delayed helium flow surge (boil-off)."""
    if step_offset == 0:
        sim._inject_bias(Subsystem.cryogenics, "temperature", 1.5)
    if step_offset == 1:
        sim._inject_bias(Subsystem.cryogenics, "he_flow_rate", 5.0)


def _apply_vacuum_leak_cascade(sim: AcceleratorSimulator, step_offset: int) -> None:
    """Cascade: vacuum leak -> pump overload -> cryo warming."""
    if step_offset < 5:
        sim._inject_bias(Subsystem.vacuum, "pressure", 2.0e-8)
    if 5 <= step_offset < 10:
        sim._inject_bias(Subsystem.vacuum, "pump_speed", -2.0)
    if step_offset >= 10:
        sim._inject_bias(Subsystem.cryogenics, "temperature", 0.15)


def _apply_psu_ripple(sim: AcceleratorSimulator, step_offset: int) -> None:
    """Square-wave AC ripple on power-supply current and voltage.

    Uses ``_set_bias`` (absolute) so the bias oscillates around zero rather
    than accumulating — two of every four ticks sit above the anomaly
    threshold, two sit below it, producing intermittent anomalies.
    """
    phase = step_offset % 4
    if phase < 2:
        sim._set_bias(Subsystem.power_supply, "current", 35.0)
        sim._set_bias(Subsystem.power_supply, "voltage", 2.5)
    else:
        sim._set_bias(Subsystem.power_supply, "current", 15.0)
        sim._set_bias(Subsystem.power_supply, "voltage", 1.0)


# --------------------------------------------------------------------------- #
# Named scenario registry (single source of truth for available faults)
# --------------------------------------------------------------------------- #

SCENARIOS: dict[str, Scenario] = {
    "magnet_quench_drift": Scenario(
        name="magnet_quench_drift",
        subsystem=Subsystem.beamline_magnets,
        duration=15,
        description=_validate_description(
            "[OBSERVATION] Beamline magnet field strength drifting above "
            "setpoint at approximately 0.04 T per step. Suspected resistive "
            "quench propagating along the magnet string. Magnet current "
            "beginning to rise."
        ),
        apply=_apply_magnet_quench_drift,
    ),
    "rf_cavity_overheating": Scenario(
        name="rf_cavity_overheating",
        subsystem=Subsystem.rf_cavities,
        duration=20,
        description=_validate_description(
            "[OBSERVATION] RF cavity amplitude drifting upward. Cavity "
            "temperature rising above nominal operating point. Phase "
            "instability developing after initial thermal drift."
        ),
        apply=_apply_rf_cavity_overheating,
    ),
    "cryo_pressure_spike": Scenario(
        name="cryo_pressure_spike",
        subsystem=Subsystem.cryogenics,
        duration=5,
        description=_validate_description(
            "[OBSERVATION] Cryogenic helium pressure spike detected. Rapid "
            "boil-off causing temperature excursion above nominal. Helium "
            "flow rate surging."
        ),
        apply=_apply_cryo_pressure_spike,
    ),
    "vacuum_leak_cascade": Scenario(
        name="vacuum_leak_cascade",
        subsystem=Subsystem.vacuum,
        duration=15,
        description=_validate_description(
            "[OBSERVATION] Vacuum pressure rising gradually. Pump speed "
            "decreasing as pump overloaded. Cryogenic temperature beginning "
            "to rise. Suspected cascade failure from vacuum sector leak."
        ),
        apply=_apply_vacuum_leak_cascade,
    ),
    "psu_ripple": Scenario(
        name="psu_ripple",
        subsystem=Subsystem.power_supply,
        duration=12,
        description=_validate_description(
            "[OBSERVATION] Power supply current and voltage exhibiting AC "
            "ripple above nominal noise floor. Intermittent anomalies on "
            "current and voltage channels."
        ),
        apply=_apply_psu_ripple,
    ),
}


def inject(simulator: AcceleratorSimulator, name: str, at_step: int = 0) -> Scenario:
    """Register a named fault scenario on ``simulator``, starting at ``at_step``.

    Raises :class:`ValueError` if ``name`` is not a known scenario. The
    scenario fires on each tick whose step count falls in
    ``[at_step, at_step + scenario.duration)``.
    """
    if name not in SCENARIOS:
        known = ", ".join(sorted(SCENARIOS))
        raise ValueError(f"unknown scenario: {name!r} (known: {known})")
    scenario = SCENARIOS[name]
    simulator.register_scenario(at_step, scenario)
    return scenario
