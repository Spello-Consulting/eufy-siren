"""Tests for the siren state machine in SirenController."""
# ruff: noqa: DOC201  (return sections are noise in small test helpers)

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest

from event_inbox import ServiceEvent, ServiceEventInbox
from local_enumerations import EndpointAction, SirenState
from siren_controller import SirenController

SWITCH = "Siren O1"


# ── Test doubles ─────────────────────────────────────────────────────────────


class FakeConfig:
    """Minimal stand-in for SCConfigManager backed by a nested dict.

    Supports the config hot-reload API: :meth:`simulate_change` swaps in new data and
    advances the modification time, so :meth:`check_for_config_changes` reports a change.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self._mtime = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def get_config_file_last_modified(self) -> dt.datetime:
        return self._mtime

    def check_for_config_changes(self, last_check: dt.datetime | None) -> dt.datetime | None:
        """Report the new mtime when the (simulated) file has changed since ``last_check``."""
        if last_check is None or self._mtime > last_check:
            return self._mtime
        return None

    def simulate_change(self, data: dict[str, Any]) -> None:
        """Replace the backing data and advance the modification time (a file edit)."""
        self._data = data
        self._mtime += dt.timedelta(seconds=1)


class FakeLogger:
    """Captures log, fatal-error, and alert (email/SMS) calls."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fatal_errors: list[str] = []
        self.emails: list[tuple[str, str]] = []
        self.sms: list[tuple[str, list[str] | None]] = []

    def log_message(self, message: str, verbosity: str = "summary") -> None:
        self.messages.append((verbosity, message))

    def log_fatal_error(self, message: str, **_kwargs: Any) -> None:
        self.fatal_errors.append(message)

    def ping_heartbeat(self, is_fail: bool | None = None) -> bool:  # noqa: ARG002, PLR6301
        return True

    def send_email(self, subject: str, body: str, test_mode: bool = False) -> bool:  # noqa: ARG002
        self.emails.append((subject, body))
        return True

    def send_sms(self, body: str, to_numbers: list[str] | None = None) -> bool:
        self.sms.append((body, to_numbers))
        return True


class FakeView:
    """Stand-in for SmartDeviceView; knows which output names are valid."""

    def __init__(self, valid_outputs: set[str]) -> None:
        self._valid = valid_outputs

    def validate_output_id(self, output_id: str | int) -> bool:
        return output_id in self._valid


class FakeWorker:
    """Captures submitted device sequence requests."""

    def __init__(self, view: FakeView) -> None:
        self._view = view
        self.submitted: list[Any] = []

    def submit(self, req: Any) -> str:
        self.submitted.append(req)
        return str(req.id)

    def get_latest_status(self) -> FakeView:
        return self._view

    # Convenience for assertions -------------------------------------------------
    def last_switch_state(self) -> bool | None:
        """Return the ``state`` of the most recent CHANGE_OUTPUT request, if any."""
        if not self.submitted:
            return None
        return bool(self.submitted[-1].steps[0].params["state"])


# ── Fixtures ─────────────────────────────────────────────────────────────────


class Clock:
    """A mutable monotonic clock for deterministic time control."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


SMS_RECIPIENTS = ["+15550001111"]


def config_data(
    *,
    enable: bool = True,
    switch: str = SWITCH,
    min_events: int = 2,
    min_sources: int = 2,
    email: bool = False,
    sms: bool = False,
    disable_motion_events: bool = False,
) -> dict[str, Any]:
    """Build a config dict for the test doubles (also used to model an on-disk edit)."""
    return {
        "General": {
            "PollingInterval": 10,
            "DisableMotionEvents": disable_motion_events,
        },
        "Email": {"EnableEmail": email},
        "SMS": {"EnableSMS": sms, "SendSMSTo": SMS_RECIPIENTS},
        "Siren": {
            "Enable": enable,
            "Switch": switch,
            "SirenDuration": 30,
            "MinMotionEvents": min_events,
            "MinMotionSources": min_sources,
            "MinMotionInterval": 10,
            "MaxMotionInterval": 60,
            "PostTriggerSleepTimer": 60,
        },
    }


def make_controller(
    *,
    enable: bool = True,
    switch: str = SWITCH,
    min_events: int = 2,
    min_sources: int = 2,
    email: bool = False,
    sms: bool = False,
    disable_motion_events: bool = False,
) -> tuple[SirenController, FakeWorker, FakeLogger, Clock]:
    """Build a controller wired to test doubles."""
    config = FakeConfig(
        config_data(
            enable=enable,
            switch=switch,
            min_events=min_events,
            min_sources=min_sources,
            email=email,
            sms=sms,
            disable_motion_events=disable_motion_events,
        )
    )
    logger = FakeLogger()
    worker = FakeWorker(FakeView({SWITCH}))
    clock = Clock()
    wake_event = threading.Event()
    inbox = ServiceEventInbox(wake_event)
    controller = SirenController(config, logger, worker, inbox, wake_event, time_fn=clock)  # type: ignore[arg-type]
    return controller, worker, logger, clock


def controller_with_config(
    config: FakeConfig, valid_outputs: set[str] | None = None
) -> tuple[SirenController, FakeWorker, FakeLogger, Clock]:
    """Build a controller around a caller-owned FakeConfig (for hot-reload tests)."""
    logger = FakeLogger()
    worker = FakeWorker(FakeView(valid_outputs if valid_outputs is not None else {SWITCH}))
    clock = Clock()
    wake_event = threading.Event()
    inbox = ServiceEventInbox(wake_event)
    controller = SirenController(config, logger, worker, inbox, wake_event, time_fn=clock)  # type: ignore[arg-type]
    return controller, worker, logger, clock


def motion(source: str) -> ServiceEvent:
    """Build a Motion event from the given source endpoint."""
    return ServiceEvent(action=EndpointAction.MOTION, endpoint_name=source, path=f"/motion/{source}")


# ── Runtime validation ───────────────────────────────────────────────────────


def test_validate_runtime_accepts_known_switch() -> None:
    """Validation passes when Siren.Switch names a real output."""
    controller, _worker, _logger, _clock = make_controller()
    assert controller.validate_runtime() is True


def test_validate_runtime_rejects_unknown_switch() -> None:
    """Validation fails (and logs fatal) when Siren.Switch is not a real output."""
    controller, _worker, logger, _clock = make_controller(switch="Nonexistent")
    assert controller.validate_runtime() is False
    assert logger.fatal_errors


# ── Motion triggering ────────────────────────────────────────────────────────


def test_motion_from_two_sources_starts_siren() -> None:
    """Two qualifying sources move the siren from IDLE to SOUNDING and switch on."""
    controller, worker, _logger, clock = make_controller()

    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE

    clock.advance(2)
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING
    assert worker.last_switch_state() is True


def test_single_source_does_not_trigger_multi_source_config() -> None:
    """Repeats from one camera do not satisfy a 2-source configuration."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    clock.advance(12)
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is None


# ── Motion-following countdown ───────────────────────────────────────────────


def test_motion_while_sounding_resets_countdown() -> None:
    """Motion during SOUNDING resets the SirenDuration countdown."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING

    # 25s in (< 30s duration): a new motion event resets the timer.
    clock.advance(25)
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING

    # 25s more (50s since first trigger, but only 25s since reset): still sounding.
    clock.advance(25)
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING

    # 30s after the reset: duration elapses.
    clock.advance(5)
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.COOLDOWN
    assert worker.last_switch_state() is False


def test_duration_elapses_stops_siren_and_enters_cooldown() -> None:
    """Without further motion the siren stops after SirenDuration and cools down."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001

    clock.advance(30)
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.COOLDOWN
    assert worker.last_switch_state() is False


# ── Cooldown lock-out ────────────────────────────────────────────────────────


def test_motion_ignored_during_cooldown() -> None:
    """Motion events are ignored while in COOLDOWN."""
    controller, _worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    clock.advance(30)
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.COOLDOWN

    # Motion during cooldown must not restart the siren.
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    assert controller.state == SirenState.COOLDOWN


def test_cooldown_ends_after_post_trigger_sleep() -> None:
    """The controller returns to IDLE once the cooldown elapses."""
    controller, _worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    clock.advance(30)
    controller._evaluate_timers(clock())  # noqa: SLF001  -> COOLDOWN, cooldown 60s

    clock.advance(60)
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE


# ── Manual overrides ─────────────────────────────────────────────────────────


def test_start_siren_endpoint_bypasses_conditions() -> None:
    """StartSiren sounds the siren immediately regardless of motion conditions."""
    controller, worker, _logger, clock = make_controller()
    event = ServiceEvent(
        action=EndpointAction.START_SIREN, endpoint_name="Start", path="/siren/start"
    )
    controller._handle_event(event, clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING
    assert worker.last_switch_state() is True


def test_stop_siren_endpoint_stops_and_starts_cooldown() -> None:
    """StopSiren stops the siren and begins the cooldown."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING

    event = ServiceEvent(action=EndpointAction.STOP_SIREN, endpoint_name="Stop", path="/siren/stop")
    controller._handle_event(event, clock())  # noqa: SLF001
    assert controller.state == SirenState.COOLDOWN
    assert worker.last_switch_state() is False


def test_start_siren_clears_cooldown() -> None:
    """StartSiren during cooldown clears the lock-out and sounds the siren."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    clock.advance(30)
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.COOLDOWN

    event = ServiceEvent(
        action=EndpointAction.START_SIREN, endpoint_name="Start", path="/siren/start"
    )
    controller._handle_event(event, clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING
    assert worker.last_switch_state() is True


def test_disabled_siren_does_not_sound() -> None:
    """With Siren.Enable false, StartSiren does not command the switch on."""
    controller, worker, logger, clock = make_controller(enable=False)
    event = ServiceEvent(
        action=EndpointAction.START_SIREN, endpoint_name="Start", path="/siren/start"
    )
    controller._handle_event(event, clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is None
    assert any("suppressed" in msg for _v, msg in logger.messages)


@pytest.mark.parametrize("action", [EndpointAction.IGNORE])
def test_ignore_action_has_no_effect(action: EndpointAction) -> None:
    """An Ignore endpoint never advances the state machine."""
    controller, worker, _logger, clock = make_controller()
    event = ServiceEvent(action=action, endpoint_name="Camera 4", path="/motion/camera4")
    controller._handle_event(event, clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is None


# ── DisableMotionEvents ──────────────────────────────────────────────────────


def test_disable_motion_events_ignores_motion() -> None:
    """With General.DisableMotionEvents true, motion never triggers the siren."""
    controller, worker, logger, clock = make_controller(disable_motion_events=True)

    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    clock.advance(2)
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001

    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is None
    # The ignored events are still logged.
    assert any("DisableMotionEvents" in msg for _v, msg in logger.messages)


def test_disable_motion_events_still_allows_start_siren() -> None:
    """StartSiren works even when motion events are disabled."""
    controller, worker, _logger, clock = make_controller(disable_motion_events=True)
    event = ServiceEvent(
        action=EndpointAction.START_SIREN, endpoint_name="Start", path="/siren/start"
    )
    controller._handle_event(event, clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING
    assert worker.last_switch_state() is True


def test_disable_motion_events_still_allows_stop_and_reset() -> None:
    """StopSiren and ResetSiren remain functional when motion events are disabled."""
    controller, worker, _logger, clock = make_controller(disable_motion_events=True)

    start = ServiceEvent(
        action=EndpointAction.START_SIREN, endpoint_name="Start", path="/siren/start"
    )
    controller._handle_event(start, clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING

    controller._handle_event(reset_event(), clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is False


# ── ResetSiren ───────────────────────────────────────────────────────────────


def reset_event() -> ServiceEvent:
    """Build a ResetSiren command event."""
    return ServiceEvent(action=EndpointAction.RESET_SIREN, endpoint_name="Reset", path="/siren/reset")


def test_reset_siren_from_sounding_stops_and_returns_to_idle() -> None:
    """ResetSiren while sounding stops the siren and returns straight to IDLE."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING

    controller._handle_event(reset_event(), clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is False


def test_reset_siren_from_cooldown_returns_to_idle() -> None:
    """ResetSiren during cooldown clears the lock-out and returns to IDLE."""
    controller, _worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    clock.advance(30)
    controller._evaluate_timers(clock())  # noqa: SLF001
    assert controller.state == SirenState.COOLDOWN

    controller._handle_event(reset_event(), clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE


def test_reset_siren_when_idle_is_a_noop() -> None:
    """ResetSiren from IDLE changes nothing and commands no switch action."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(reset_event(), clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is None


def test_reset_siren_re_arms_for_next_trigger() -> None:
    """After a reset, a fresh set of motion events can trigger the siren again."""
    controller, worker, _logger, clock = make_controller()
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    controller._handle_event(reset_event(), clock())  # noqa: SLF001
    assert controller.state == SirenState.IDLE

    clock.advance(1)
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING
    assert worker.last_switch_state() is True


# ── Alert notifications ──────────────────────────────────────────────────────


def _trigger(controller: SirenController, clock: Clock) -> None:
    """Drive two motion events so the siren starts."""
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    controller._handle_event(motion("Camera 2"), clock())  # noqa: SLF001


def test_alerts_sent_on_start_and_stop() -> None:
    """With email and SMS enabled, start and stop each send one alert on both channels."""
    controller, _worker, logger, clock = make_controller(email=True, sms=True)
    _trigger(controller, clock)
    assert len(logger.emails) == 1
    assert len(logger.sms) == 1
    assert logger.sms[0][1] == SMS_RECIPIENTS  # configured recipients are passed through

    event = ServiceEvent(action=EndpointAction.STOP_SIREN, endpoint_name="Stop", path="/siren/stop")
    controller._handle_event(event, clock())  # noqa: SLF001
    assert len(logger.emails) == 2
    assert len(logger.sms) == 2


def test_started_alert_sent_only_once_while_sounding() -> None:
    """Motion-following and a repeated StartSiren do not resend the started alert."""
    controller, _worker, logger, clock = make_controller(email=True, sms=True)
    _trigger(controller, clock)
    assert len(logger.emails) == 1

    clock.advance(5)
    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001  motion-following
    start = ServiceEvent(action=EndpointAction.START_SIREN, endpoint_name="Start", path="/siren/start")
    controller._handle_event(start, clock())  # noqa: SLF001  re-invoked while sounding
    assert len(logger.emails) == 1
    assert len(logger.sms) == 1


def test_no_alerts_when_disabled() -> None:
    """No alerts are sent when both channels are disabled."""
    controller, _worker, logger, clock = make_controller(email=False, sms=False)
    _trigger(controller, clock)
    assert logger.emails == []
    assert logger.sms == []


def test_email_only_when_sms_disabled() -> None:
    """Only the email channel fires when SMS is disabled."""
    controller, _worker, logger, clock = make_controller(email=True, sms=False)
    _trigger(controller, clock)
    assert len(logger.emails) == 1
    assert logger.sms == []


def test_reset_from_sounding_sends_stopped_alert() -> None:
    """A reset that stops a sounding siren also sends the stopped alert."""
    controller, _worker, logger, clock = make_controller(email=True, sms=False)
    _trigger(controller, clock)
    assert len(logger.emails) == 1  # started

    controller._handle_event(reset_event(), clock())  # noqa: SLF001
    assert len(logger.emails) == 2  # started + stopped


def test_stopped_alert_not_sent_without_start() -> None:
    """StopSiren from IDLE (no active siren) sends no stopped alert."""
    controller, _worker, logger, clock = make_controller(email=True, sms=True)
    event = ServiceEvent(action=EndpointAction.STOP_SIREN, endpoint_name="Stop", path="/siren/stop")
    controller._handle_event(event, clock())  # noqa: SLF001
    assert logger.emails == []
    assert logger.sms == []


# ── Config hot-reload ────────────────────────────────────────────────────────


def test_reload_is_noop_when_file_unchanged() -> None:
    """Without a file change, a reload check does nothing and logs no reload."""
    config = FakeConfig(config_data(disable_motion_events=False))
    controller, _worker, logger, _clock = controller_with_config(config)

    controller._reload_config_if_changed()  # noqa: SLF001
    assert not any("reloading" in msg.lower() for _v, msg in logger.messages)
    assert controller.disable_motion_events is False


def test_reload_applies_changed_settings() -> None:
    """Editing the config file on disk takes effect on the next tick, without a restart."""
    config = FakeConfig(config_data(disable_motion_events=False))
    controller, worker, logger, clock = controller_with_config(config)

    # Before the change, two-source motion triggers the siren.
    _trigger(controller, clock)
    assert controller.state == SirenState.SOUNDING
    controller._handle_event(reset_event(), clock())  # noqa: SLF001  back to IDLE

    # Edit the file to disable motion events, then reload.
    config.simulate_change(config_data(disable_motion_events=True))
    controller._reload_config_if_changed()  # noqa: SLF001
    assert controller.disable_motion_events is True
    assert any("reloading" in msg.lower() for _v, msg in logger.messages)

    # Motion is now ignored.
    clock.advance(20)
    _trigger(controller, clock)
    assert controller.state == SirenState.IDLE
    assert worker.last_switch_state() is False  # last command was the reset's switch-off


def test_reload_updates_motion_tracker_thresholds() -> None:
    """A reload rebuilds the MotionTracker so new trigger thresholds apply immediately."""
    config = FakeConfig(config_data(min_events=2, min_sources=2))
    controller, _worker, _logger, clock = controller_with_config(config)

    # Loosen the trigger to a single source, then reload.
    config.simulate_change(config_data(min_events=1, min_sources=1))
    controller._reload_config_if_changed()  # noqa: SLF001

    controller._handle_event(motion("Camera 1"), clock())  # noqa: SLF001
    assert controller.state == SirenState.SOUNDING


def test_reload_keeps_previous_switch_when_new_one_is_invalid() -> None:
    """A reload naming an unknown Siren.Switch keeps the previous validated switch."""
    config = FakeConfig(config_data(switch=SWITCH))
    controller, _worker, logger, _clock = controller_with_config(config)

    config.simulate_change(config_data(switch="Nonexistent"))
    controller._reload_config_if_changed()  # noqa: SLF001

    assert controller.switch == SWITCH
    assert any("keeping previous switch" in msg for _v, msg in logger.messages)


def test_reload_adopts_new_valid_switch() -> None:
    """A reload naming a different, valid output adopts it."""
    other = "Siren O2"
    config = FakeConfig(config_data(switch=SWITCH))
    controller, _worker, _logger, _clock = controller_with_config(
        config, valid_outputs={SWITCH, other}
    )

    config.simulate_change(config_data(switch=other))
    controller._reload_config_if_changed()  # noqa: SLF001

    assert controller.switch == other
