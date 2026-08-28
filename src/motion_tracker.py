"""Pure motion-window logic for deciding when the siren should trigger.

This module has no dependencies on threads, HTTP, the config manager or the system
clock (timestamps are always passed in), so it can be unit-tested in isolation.

The rules implement the ``Siren`` trigger requirements from the design brief:

* **Per-source debounce** (``MinMotionInterval``): a given source can contribute at
  most one qualifying event per ``MinMotionInterval`` seconds — this stops a single
  camera's rapid burst of motion events from satisfying the trigger on its own.
* **Sliding window** (``MaxMotionInterval``): a qualifying event extends the current
  window only if it arrives within ``MaxMotionInterval`` seconds of the previous
  qualifying event; otherwise the window resets and a fresh one starts with this event.
* **Trigger**: fire once the current window holds at least ``MinMotionEvents``
  qualifying events spanning at least ``MinMotionSources`` unique sources.
"""

from __future__ import annotations

from dataclasses import dataclass

# Upper bound on retained qualifying events, to cap memory if a misconfigured window
# (e.g. MinMotionSources set higher than the number of cameras) can never trigger.
# Far above any realistic MinMotionEvents value, so it never affects the decision.
_MAX_RETAINED_EVENTS = 256


@dataclass(frozen=True)
class _QualifyingEvent:
    """A motion event accepted into the current window."""

    source: str
    ts: float


class MotionTracker:
    """Tracks qualifying motion events and evaluates the siren trigger condition.

    Args:
        min_events: Minimum number of qualifying events required in the window.
        min_sources: Minimum number of unique sources required among those events.
        min_interval: Per-source debounce, in seconds.
        max_interval: Maximum gap, in seconds, between consecutive qualifying events
            before the window resets.
    """

    def __init__(
        self,
        min_events: int,
        min_sources: int,
        min_interval: float,
        max_interval: float,
    ) -> None:
        self._min_events = max(1, int(min_events))
        self._min_sources = max(1, int(min_sources))
        self._min_interval = float(min_interval)
        self._max_interval = float(max_interval)
        self._events: list[_QualifyingEvent] = []
        self._last_accepted_ts: dict[str, float] = {}

    def record(self, source: str, now: float) -> bool:
        """Record a motion event and report whether the trigger condition is now met.

        Args:
            source: Identifier of the motion source (the endpoint name).
            now: Monotonic timestamp of the event, in seconds.

        Returns:
            ``True`` if, after recording this event, the window satisfies the trigger
            condition; ``False`` otherwise (including when the event is debounced).
        """
        # Per-source debounce: ignore events too close to this source's last accepted one.
        last_ts = self._last_accepted_ts.get(source)
        if last_ts is not None and (now - last_ts) < self._min_interval:
            return self._is_triggered()

        # Sliding window: if the gap since the last qualifying event exceeds the
        # maximum, the chain is broken — start a fresh window.
        if self._events and (now - self._events[-1].ts) > self._max_interval:
            self._reset_window()

        self._events.append(_QualifyingEvent(source=source, ts=now))
        self._last_accepted_ts[source] = now
        if len(self._events) > _MAX_RETAINED_EVENTS:
            dropped = self._events.pop(0)
            # Only forget the source's debounce stamp if it has no more retained events.
            if all(e.source != dropped.source for e in self._events):
                self._last_accepted_ts.pop(dropped.source, None)

        return self._is_triggered()

    def reset(self) -> None:
        """Clear all tracked events and debounce state (a fresh start)."""
        self._reset_window()

    @property
    def event_count(self) -> int:
        """Number of qualifying events currently in the window."""
        return len(self._events)

    @property
    def unique_source_count(self) -> int:
        """Number of unique sources among the qualifying events in the window."""
        return len({e.source for e in self._events})

    def _reset_window(self) -> None:
        """Reset the window and per-source debounce state."""
        self._events = []
        self._last_accepted_ts = {}

    def _is_triggered(self) -> bool:
        """Return whether the current window satisfies the trigger condition."""
        if len(self._events) < self._min_events:
            return False
        unique_sources = {e.source for e in self._events}
        return len(unique_sources) >= self._min_sources
