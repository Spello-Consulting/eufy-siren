"""The master controller: turns ServiceAPI motion events into siren actions.

`SirenController` runs its :meth:`run` method as the ``controller`` thread. Each tick it
reloads the config if the file changed on disk, drains the
:class:`~event_inbox.ServiceEventInbox`, feeds motion events through a
:class:`~motion_tracker.MotionTracker`, advances the siren state machine, drives the smart
switch via the :class:`~sc_smart_device.SmartDeviceWorker`, and pings the heartbeat monitor.

The state machine is ``IDLE → SOUNDING → COOLDOWN → IDLE``:

* **IDLE** — motion events feed the tracker; a satisfied trigger starts the siren.
* **SOUNDING** — the siren is on; any motion event resets the ``SirenDuration`` countdown
  (motion-following). The countdown elapsing, or a ``StopSiren`` request, stops the siren.
* **COOLDOWN** — a ``PostTriggerSleepTimer`` lock-out during which motion is ignored;
  a ``StartSiren`` request clears it immediately.

When the siren starts and again when it stops, an alert is sent via email (if
``Email.EnableEmail``) and/or SMS (if ``SMS.EnableSMS``), using ``SCLogger.send_email`` and
``SCLogger.send_sms``. A flag ensures exactly one started alert per activation.

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

        # Configuration-derived settings, loaded here and again on every hot-reload.
        self._load_settings()

        # Timestamp of the config file when last read, for change detection (config hot-reload).
        self.config_last_check = config.get_config_file_last_modified()

        # State machine
        self.state: SirenState = SirenState.IDLE
        self._last_trigger_ts: float | None = None
        self._cooldown_until: float | None = None
        self._commanded_on = False
        # Whether a "siren started" alert is outstanding (awaiting a matching "stopped"
        # alert). Guards against sending duplicate started alerts while the siren sounds.
        self._start_alert_sent = False

    def _load_settings(self) -> None:
        """Read all configuration-derived settings into instance attributes.

        Called at construction and again whenever the config file changes on disk (see
        :meth:`_reload_config_if_changed`), so edits take effect without a restart. Only
        settings are (re)read here; the state machine, cooldown timers and any outstanding
        alert are runtime state and are left untouched.
        """
        config = self.config
        self.enabled = bool(config.get("Siren", "Enable", default=True))
        # When true, motion events are logged but ignored — they cannot trigger the siren.
        # Manual StartSiren/StopSiren/ResetSiren actions remain fully enabled.
        self.disable_motion_events = bool(
            config.get("General", "DisableMotionEvents", default=False)
        )
        self.switch = str(config.get("Siren", "Switch", default="") or "")
        self.siren_duration = _as_float(config.get("Siren", "SirenDuration", default=30), 30.0)
        self.post_trigger_sleep = _as_float(
            config.get("Siren", "PostTriggerSleepTimer", default=60), 60.0
        )
        self.poll_interval = _as_float(config.get("General", "PollingInterval", default=10), 10.0)

        # Alert notifications on siren start/stop (email and/or SMS).
        self.email_alerts_enabled = bool(config.get("Email", "EnableEmail", default=False))
        self.sms_alerts_enabled = bool(config.get("SMS", "EnableSMS", default=False))
        self._sms_recipients = list(config.get("SMS", "SendSMSTo", default=[]) or [])

        self.tracker = MotionTracker(
            min_events=int(_as_float(config.get("Siren", "MinMotionEvents", default=1), 1)),
            min_sources=int(_as_float(config.get("Siren", "MinMotionSources", default=1), 1)),
            min_interval=_as_float(config.get("Siren", "MinMotionInterval", default=10), 10.0),
            max_interval=_as_float(config.get("Siren", "MaxMotionInterval", default=60), 60.0),
        )

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

        if not self._switch_is_valid():
            msg = (
                f"Siren.Switch '{self.switch}' does not match any output under "
                "SCSmartDevices.Devices[].Outputs[]."
            )
            self.logger.log_fatal_error(msg, exit_app=False)
            return False

        self.logger.log_message(f"Siren switch validated: output '{self.switch}'.", "detailed")
        return True

    def _switch_is_valid(self) -> bool:
        """Return whether ``self.switch`` names a real output on a configured device."""
        view = self.smart_device_worker.get_latest_status()
        return bool(view.validate_output_id(self.switch))

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

        if self.disable_motion_events:
            self.logger.log_message(
                "General.DisableMotionEvents is true — motion events will be logged but ignored; "
                "manual StartSiren/StopSiren/ResetSiren remain active.",
                "summary",
            )

        while not stop_event.is_set():
            self.wake_event.clear()
            now = self._time_fn()

            self._reload_config_if_changed()

            for event in self.inbox.drain():
                self._handle_event(event, now)

            self._evaluate_timers(now)
            self.logger.ping_heartbeat()
            self.logger.trim_logfile()

            self.wake_event.wait(timeout=self.poll_interval)

        self._shutdown()

    # ── Config hot-reload ────────────────────────────────────────────────────

    def _reload_config_if_changed(self) -> None:
        """Reload settings from disk if the config file has changed since the last check.

        ``SCConfigManager.check_for_config_changes`` reloads (and re-validates) the file
        in place when its modification time advances; we then re-read our cached settings.
        A reload never disturbs the running state machine, but if the newly loaded
        ``Siren.Switch`` no longer names a real output the previous, validated switch is
        kept so the siren remains controllable.
        """
        timestamp = self.config.check_for_config_changes(self.config_last_check)
        if timestamp is None:
            return
        self.config_last_check = timestamp
        self.logger.log_message("Configuration file changed on disk — reloading settings.", "summary")

        previous_switch = self.switch
        self._load_settings()
        if self.switch != previous_switch and not self._switch_is_valid():
            self.logger.log_message(
                f"Reloaded Siren.Switch '{self.switch}' is not a valid output; "
                f"keeping previous switch '{previous_switch}'.",
                "warning",
            )
            self.switch = previous_switch

    def _shutdown(self) -> None:
        """Turn the siren off on shutdown."""
        self.logger.log_message("Siren controller stopping — turning siren off.", "detailed")
        self._command_switch(on=False)
        # If the siren was sounding, send the matching stopped alert so an activation is
        # never left un-closed.
        self._notify_siren_stopped("controller shutdown")

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
            self._reset_siren(now, reason="ResetSiren endpoint")
        elif event.action == EndpointAction.MOTION:
            if self.disable_motion_events:
                self.logger.log_message(
                    f"Motion from '{event.endpoint_name}' ignored — "
                    "General.DisableMotionEvents is true.",
                    "debug",
                )
                return
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
        self._notify_siren_started(reason)

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
        self._notify_siren_stopped(reason)

    def _reset_siren(self, now: float, reason: str) -> None:
        """Clear the Siren cooldown state if it is active or idle, returning to IDLE."""
        if self.state == SirenState.SOUNDING:
            self._stop_siren(now, reason)
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

    # ── Alert notifications ──────────────────────────────────────────────────

    def _notify_siren_started(self, reason: str) -> None:
        """Send a "siren started" alert, at most once per activation.

        Args:
            reason: Human-readable reason the siren started, included in the alert.
        """
        if self._start_alert_sent:
            return
        self._start_alert_sent = True
        self._send_alert(
            subject="eufy-siren: SIREN ACTIVATED",
            body=f"The eufy-siren has been activated and the siren is now sounding ({reason}).",
        )

    def _notify_siren_stopped(self, reason: str) -> None:
        """Send a "siren stopped" alert, only if a start alert is outstanding.

        Args:
            reason: Human-readable reason the siren stopped, included in the alert.
        """
        if not self._start_alert_sent:
            return
        self._start_alert_sent = False
        self._send_alert(
            subject="eufy-siren: siren stopped",
            body=f"The eufy-siren has stopped sounding ({reason}).",
        )

    def _send_alert(self, subject: str, body: str) -> None:
        """Dispatch an alert via email and/or SMS per configuration.

        A failure on one channel is logged and never propagates — an alerting problem
        must not take down the controller thread.

        Args:
            subject: Email subject (SMS uses the body only).
            body: Message body for both channels.
        """
        if self.email_alerts_enabled:
            try:
                self.logger.send_email(subject, body)
            except Exception as exc:  # noqa: BLE001 - a mail failure must not break the controller
                self.logger.log_message(f"Failed to send email alert: {exc}", "error")
        if self.sms_alerts_enabled:
            try:
                self.logger.send_sms(body, self._sms_recipients or None)
            except Exception as exc:  # noqa: BLE001 - an SMS failure must not break the controller
                self.logger.log_message(f"Failed to send SMS alert: {exc}", "error")


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
