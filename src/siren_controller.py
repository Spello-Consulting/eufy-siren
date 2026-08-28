"""The master controller: turns ServiceAPI motion events into siren actions.

`SirenController` runs its :meth:`run` method as the ``controller`` thread. Each tick it
drains the :class:`~event_inbox.ServiceEventInbox`, feeds motion events through a
:class:`~motion_tracker.MotionTracker`, advances the siren state machine, drives the smart
switch via the :class:`~sc_smart_device.SmartDeviceWorker`, and pings the heartbeat monitor.

The state machine is ``IDLE → SOUNDING → COOLDOWN → IDLE``:

* **IDLE** — motion events feed the tracker; a satisfied trigger starts the siren.
* **SOUNDING** — the siren is on; any motion event resets the ``SirenDuration`` countdown
  (motion-following). The countdown elapsing, or a ``StopSiren`` request, stops the siren.
* **COOLDOWN** — a ``PostTriggerSleepTimer`` lock-out during which motion is ignored;
  a ``StartSiren`` request clears it immediately.

The clock is injected (``time_fn``) so the timing behaviour can be unit-tested deterministically.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from sc_smart_device import DeviceSequenceRequest, DeviceStep, StepKind

from local_enumerations import EndpointAction, SirenState
from motion_tracker import MotionTracker

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event

    from sc_foundation import SCConfigManager, SCLogger
    from sc_smart_device import SmartDeviceWorker

    from event_inbox import ServiceEvent, ServiceEventInbox


# Retry policy for the switch change-output command.
_SWITCH_RETRIES = 2
_SWITCH_RETRY_BACKOFF_S = 1.0


class SirenController:
    """Orchestrates motion events into siren on/off actions.

    Args:
        config: The configuration manager.
        logger: The logger.
        smart_device_worker: Worker used to change the smart-switch output.
        inbox: Thread-safe inbox of ServiceAPI events.
        wake_event: Event the controller clears each tick and waits on between ticks.
        time_fn: Monotonic clock function, injectable for testing.
    """

    def __init__(
        self,
        config: SCConfigManager,
        logger: SCLogger,
        smart_device_worker: SmartDeviceWorker,
        inbox: ServiceEventInbox,
        wake_event: Event,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.logger = logger
        self.smart_device_worker = smart_device_worker
        self.inbox = inbox
        self.wake_event = wake_event
        self._time_fn = time_fn

        self.enabled = bool(config.get("Siren", "Enable", default=True))
        self.switch = str(config.get("Siren", "Switch", default="") or "")
        self.siren_duration = _as_float(config.get("Siren", "SirenDuration", default=30), 30.0)
        self.post_trigger_sleep = _as_float(
            config.get("Siren", "PostTriggerSleepTimer", default=60), 60.0
        )
        self.poll_interval = _as_float(config.get("General", "PollingInterval", default=10), 10.0)

        self.tracker = MotionTracker(
            min_events=int(_as_float(config.get("Siren", "MinMotionEvents", default=1), 1)),
            min_sources=int(_as_float(config.get("Siren", "MinMotionSources", default=1), 1)),
            min_interval=_as_float(config.get("Siren", "MinMotionInterval", default=10), 10.0),
            max_interval=_as_float(config.get("Siren", "MaxMotionInterval", default=60), 60.0),
        )

        # State machine
        self.state: SirenState = SirenState.IDLE
        self._last_trigger_ts: float | None = None
        self._cooldown_until: float | None = None
        self._commanded_on = False

    # ── Startup validation ───────────────────────────────────────────────────

    def validate_runtime(self) -> bool:
        """Validate configuration that Cerberus cannot check (brief §37).

        Ensures ``Siren.Switch`` names a real output on a configured smart device.

        Returns:
            ``True`` if the configuration is usable; ``False`` (after logging a fatal
            error) otherwise.
        """
        if not self.switch:
            self.logger.log_fatal_error(
                "Siren.Switch is not configured — cannot control the siren.", exit_app=False
            )
            return False

        view = self.smart_device_worker.get_latest_status()
        if not view.validate_output_id(self.switch):
            msg = (
                f"Siren.Switch '{self.switch}' does not match any output under "
                "SCSmartDevices.Devices[].Outputs[]."
            )
            self.logger.log_fatal_error(msg, exit_app=False)
            return False

        self.logger.log_message(f"Siren switch validated: output '{self.switch}'.", "detailed")
        return True

    # ── Thread entry point ───────────────────────────────────────────────────

    def run(self, stop_event: Event) -> None:
        """Run the controller loop until ``stop_event`` is set.

        Args:
            stop_event: Event signalling the controller to stop.
        """
        self.logger.log_message("Siren controller starting main control loop.", "detailed")

        if not self.validate_runtime():
            stop_event.set()
            self.wake_event.set()
            return

        # Safety: ensure the siren is off at startup regardless of prior state.
        self._command_switch(on=False)

        if not self.enabled:
            self.logger.log_message(
                "Siren.Enable is false — running but the siren will not be sounded.", "summary"
            )

        while not stop_event.is_set():
            self.wake_event.clear()
            now = self._time_fn()

            for event in self.inbox.drain():
                self._handle_event(event, now)

            self._evaluate_timers(now)
            self.logger.ping_heartbeat()
            self.logger.trim_logfile()

            self.wake_event.wait(timeout=self.poll_interval)

        self._shutdown()

    def _shutdown(self) -> None:
        """Turn the siren off on shutdown."""
        self.logger.log_message("Siren controller stopping — turning siren off.", "detailed")
        self._command_switch(on=False)

    # ── Event handling ───────────────────────────────────────────────────────

    def _handle_event(self, event: ServiceEvent, now: float) -> None:
        """Dispatch a single ServiceAPI event to the state machine.

        Args:
            event: The event to handle.
            now: Current monotonic time.
        """
        if event.action == EndpointAction.START_SIREN:
            self.logger.log_message(
                f"StartSiren requested via '{event.endpoint_name}'.", "summary"
            )
            self._start_siren(now, reason="StartSiren endpoint")
        elif event.action == EndpointAction.STOP_SIREN:
            self.logger.log_message(
                f"StopSiren requested via '{event.endpoint_name}'.", "summary"
            )
            self._stop_siren(now, reason="StopSiren endpoint")
        elif event.action == EndpointAction.RESET_SIREN:
            self.logger.log_message(
                f"ResetSiren requested via '{event.endpoint_name}'.", "summary"
            )
            self._reset_siren(reason="ResetSiren endpoint")
        elif event.action == EndpointAction.MOTION:
            self._handle_motion(event, now)
        else:  # EndpointAction.IGNORE
            self.logger.log_message(
                f"Ignoring event from '{event.endpoint_name}' (action=Ignore).", "debug"
            )

    def _handle_motion(self, event: ServiceEvent, now: float) -> None:
        """Handle a ``Motion`` event according to the current state.

        Args:
            event: The motion event (its ``endpoint_name`` is the tracker source).
            now: Current monotonic time.
        """
        if self.state == SirenState.COOLDOWN:
            self.logger.log_message(
                f"Motion from '{event.endpoint_name}' ignored — siren in cooldown.", "debug"
            )
            return

        if self.state == SirenState.SOUNDING:
            self._last_trigger_ts = now
            self.logger.log_message(
                f"Motion from '{event.endpoint_name}' — siren countdown reset.", "debug"
            )
            return

        # IDLE — feed the tracker and start the siren if the trigger condition is met.
        triggered = self.tracker.record(event.endpoint_name, now)
        self.logger.log_message(
            f"Motion from '{event.endpoint_name}' recorded "
            f"({self.tracker.event_count} event(s), {self.tracker.unique_source_count} source(s)).",
            "debug",
        )
        if triggered:
            self._start_siren(now, reason="motion trigger condition met")
            self.tracker.reset()

    # ── State transitions ────────────────────────────────────────────────────

    def _start_siren(self, now: float, reason: str) -> None:
        """Start the siren and enter the SOUNDING state.

        Args:
            now: Current monotonic time.
            reason: Human-readable reason, for logging.
        """
        if not self.enabled:
            self.logger.log_message(
                f"Siren start suppressed ({reason}) — Siren.Enable is false.", "warning"
            )
            return

        self._command_switch(on=True)
        self.state = SirenState.SOUNDING
        self._last_trigger_ts = now
        self._cooldown_until = None
        self.logger.log_message(
            f"Siren STARTED ({reason}); will stop {self.siren_duration:g}s after last motion.",
            "summary",
        )

    def _stop_siren(self, now: float, reason: str) -> None:
        """Stop the siren and enter the COOLDOWN state.

        Args:
            now: Current monotonic time.
            reason: Human-readable reason, for logging.
        """
        self._command_switch(on=False)
        self.state = SirenState.COOLDOWN
        self._last_trigger_ts = None
        self._cooldown_until = now + self.post_trigger_sleep
        self.tracker.reset()
        self.logger.log_message(
            f"Siren STOPPED ({reason}); cooldown for {self.post_trigger_sleep:g}s.", "summary"
        )

    def _reset_siren(self, reason: str) -> None:
        """Clear the Siren cooldown state if it is active or idle, returning to IDLE."""
        if self.state == SirenState.SOUNDING:
            self._command_switch(on=False)
            self.logger.log_message(f"Siren stopped ({reason}).", "summary")
        if self.state in {SirenState.COOLDOWN, SirenState.SOUNDING}:
            self.state = SirenState.IDLE
            self._cooldown_until = None
            self.tracker.reset()
            self.logger.log_message(f"Siren reset ({reason}) — cooldown cleared.", "summary")

    def _evaluate_timers(self, now: float) -> None:
        """Advance time-based state transitions (duration expiry, cooldown end).

        Args:
            now: Current monotonic time.
        """
        if (
            self.state == SirenState.SOUNDING
            and self._last_trigger_ts is not None
            and (now - self._last_trigger_ts) >= self.siren_duration
        ):
            self._stop_siren(now, reason="siren duration elapsed")
            return

        if (
            self.state == SirenState.COOLDOWN
            and self._cooldown_until is not None
            and now >= self._cooldown_until
        ):
            self.state = SirenState.IDLE
            self._cooldown_until = None
            self.tracker.reset()
            self.logger.log_message("Cooldown ended — ready to trigger again.", "detailed")

    # ── Smart-switch command ─────────────────────────────────────────────────

    def _command_switch(self, on: bool) -> None:
        """Submit a change-output request to the smart-device worker.

        Args:
            on: ``True`` to energise the switch (siren on), ``False`` to de-energise.
        """
        req = DeviceSequenceRequest(
            steps=[
                DeviceStep(
                    StepKind.CHANGE_OUTPUT,
                    {"output_identity": self.switch, "state": on},
                    retries=_SWITCH_RETRIES,
                    retry_backoff_s=_SWITCH_RETRY_BACKOFF_S,
                )
            ],
            label=f"siren-{'on' if on else 'off'}",
        )
        self.smart_device_worker.submit(req)
        self._commanded_on = on


def _as_float(value: object, default: float) -> float:
    """Coerce a config value to float, falling back to ``default``.

    Args:
        value: The raw config value.
        default: Value to use when ``value`` is missing or not numeric.

    Returns:
        The coerced float.
    """
    if isinstance(value, (int, float)):
        return float(value)
    return default
