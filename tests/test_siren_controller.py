"""Tests for the siren state machine in SirenController."""
# ruff: noqa: DOC201  (return sections are noise in small test helpers)

from __future__ import annotations

import threading
from typing import Any

import pytest

from event_inbox import ServiceEvent, ServiceEventInbox
from local_enumerations import EndpointAction, SirenState
from siren_controller import SirenController

SWITCH = "Siren O1"


# ── Test doubles ─────────────────────────────────────────────────────────────


class FakeConfig:
    """Minimal stand-in for SCConfigManager backed by a nested dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node


class FakeLogger:
    """Captures log and fatal-error calls."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fatal_errors: list[str] = []

    def log_message(self, message: str, verbosity: str = "summary") -> None:
        self.messages.append((verbosity, message))

    def log_fatal_error(self, message: str, **_kwargs: Any) -> None:
        self.fatal_errors.append(message)

    def ping_heartbeat(self, is_fail: bool | None = None) -> bool:  # noqa: ARG002, PLR6301
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


def make_controller(
    *,
    enable: bool = True,
    switch: str = SWITCH,
    min_events: int = 2,
    min_sources: int = 2,
) -> tuple[SirenController, FakeWorker, FakeLogger, Clock]:
    """Build a controller wired to test doubles."""
    config = FakeConfig(
        {
            "General": {"PollingInterval": 10},
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
    )
    logger = FakeLogger()
    worker = FakeWorker(FakeView({SWITCH}))
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
