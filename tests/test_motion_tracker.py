"""Tests for the pure motion-window trigger logic."""

from motion_tracker import MotionTracker


def test_single_event_triggers_when_thresholds_are_one() -> None:
    """One event satisfies a 1-event / 1-source configuration."""
    tracker = MotionTracker(min_events=1, min_sources=1, min_interval=10, max_interval=60)
    assert tracker.record("Camera 1", now=0.0) is True


def test_two_events_one_source_triggers_after_debounce_gap() -> None:
    """Two spaced events from one source trigger a 2-event / 1-source config."""
    tracker = MotionTracker(min_events=2, min_sources=1, min_interval=10, max_interval=60)
    assert tracker.record("Camera 1", now=0.0) is False
    assert tracker.record("Camera 1", now=12.0) is True


def test_per_source_debounce_ignores_rapid_repeats() -> None:
    """A second event from the same source within MinMotionInterval is ignored."""
    tracker = MotionTracker(min_events=2, min_sources=1, min_interval=10, max_interval=60)
    assert tracker.record("Camera 1", now=0.0) is False
    # Within the 10s debounce window — must not count.
    assert tracker.record("Camera 1", now=5.0) is False
    assert tracker.event_count == 1
    # After the debounce window — counts and triggers.
    assert tracker.record("Camera 1", now=11.0) is True


def test_multi_source_requires_distinct_sources() -> None:
    """A 2-source config is not satisfied by repeats from a single source."""
    tracker = MotionTracker(min_events=2, min_sources=2, min_interval=10, max_interval=60)
    assert tracker.record("Camera 1", now=0.0) is False
    assert tracker.record("Camera 1", now=12.0) is False  # 2 events, still 1 source
    assert tracker.unique_source_count == 1
    assert tracker.record("Camera 2", now=20.0) is True   # now 2 sources


def test_near_simultaneous_two_cameras_trigger() -> None:
    """Two different cameras firing close together satisfy the multi-source trigger.

    The per-source debounce must not block distinct sources — this is the intruder
    crossing two fields of view at nearly the same instant.
    """
    tracker = MotionTracker(min_events=2, min_sources=2, min_interval=10, max_interval=60)
    assert tracker.record("Camera 1", now=0.0) is False
    assert tracker.record("Camera 2", now=1.0) is True


def test_window_resets_when_max_interval_exceeded() -> None:
    """A gap larger than MaxMotionInterval starts a fresh window."""
    tracker = MotionTracker(min_events=2, min_sources=1, min_interval=10, max_interval=60)
    assert tracker.record("Camera 1", now=0.0) is False
    # 100s later — beyond the 60s window; the earlier event is dropped.
    assert tracker.record("Camera 1", now=100.0) is False
    assert tracker.event_count == 1


def test_reset_clears_state() -> None:
    """reset() empties the window and debounce state."""
    tracker = MotionTracker(min_events=2, min_sources=2, min_interval=10, max_interval=60)
    tracker.record("Camera 1", now=0.0)
    tracker.record("Camera 2", now=1.0)
    tracker.reset()
    assert tracker.event_count == 0
    assert tracker.unique_source_count == 0
    # A source that was previously debounced can immediately contribute again.
    assert tracker.record("Camera 1", now=2.0) is False
    assert tracker.event_count == 1
