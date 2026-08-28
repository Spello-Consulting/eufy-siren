"""Thread-safe hand-off of ServiceAPI events to the controller thread.

The ServiceAPI thread appends :class:`ServiceEvent` records via :meth:`ServiceEventInbox.push`
and sets the shared ``wake_event``; the controller thread drains them each tick with
:meth:`ServiceEventInbox.drain`.  This follows the same locked-snapshot pattern used by
``sc_smart_device.SmartDeviceView`` for passing state between threads.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from local_enumerations import EndpointAction


@dataclass(frozen=True)
class ServiceEvent:
    """A single request received by the ServiceAPI, handed to the controller.

    Attributes:
        action: The action configured for the matched endpoint.
        endpoint_name: The human-readable name of the matched endpoint.
        path: The request URL path (without query string).
        ts: Monotonic timestamp (seconds) captured when the event was received.
    """

    action: EndpointAction
    endpoint_name: str
    path: str
    ts: float = field(default_factory=time.monotonic)


class ServiceEventInbox:
    """A thread-safe FIFO buffer of :class:`ServiceEvent` records.

    The API thread produces events; the controller thread consumes them. A shared
    ``threading.Event`` is set on every push so the controller can wake immediately
    rather than waiting for its next poll interval.

    Args:
        wake_event: Event set whenever a new event is pushed, to wake the controller.
    """

    def __init__(self, wake_event: threading.Event) -> None:
        self._wake_event = wake_event
        self._lock = threading.Lock()
        self._events: list[ServiceEvent] = []

    def push(self, event: ServiceEvent) -> None:
        """Append an event and wake the controller.

        Args:
            event: The event to enqueue.
        """
        with self._lock:
            self._events.append(event)
        self._wake_event.set()

    def drain(self) -> list[ServiceEvent]:
        """Atomically remove and return all queued events in arrival order.

        Returns:
            The events queued since the last drain (possibly empty).
        """
        with self._lock:
            events = self._events
            self._events = []
        return events
