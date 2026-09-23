"""PROTOTYPE alert rule on SIMULATED telemetry.

This is an illustrative demo rule. It is not validated machine safety logic and
must not be used to protect people on a real machine.
"""

from __future__ import annotations

from .api.schemas import TelemetryReadings
from .store import AlertTemplate

SEATBELT_RULE = AlertTemplate(
    rule_id="prototype.seatbelt_unfastened.v1",
    alert_type="seatbelt_unfastened",
    severity="warning",
    message="Seatbelt unfastened while the engine is running (simulated).",
    start_speech="Warning: the engine is running and your seatbelt is not fastened. Please fasten your seatbelt.",
    clear_speech="Thanks, your seatbelt is fastened. The seatbelt warning is cleared.",
)


def seatbelt_condition(readings: TelemetryReadings, *, requires_engine_on: bool) -> bool:
    """True while the (prototype) seatbelt condition holds for this sample."""
    if readings.seatbelt_fastened:
        return False
    return readings.engine_on or not requires_engine_on
