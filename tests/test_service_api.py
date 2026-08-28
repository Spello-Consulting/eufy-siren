"""Tests for the ServiceAPI HTTP server and its helpers."""
# ruff: noqa: DOC201, DOC402  (return/yield sections are noise in small test helpers)
# ruff: noqa: PLC2701  (tests intentionally exercise private module helpers)

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from email.message import Message
from typing import TYPE_CHECKING, Any

import pytest

from event_inbox import ServiceEventInbox
from local_enumerations import EndpointAction
from service_api import (
    _access_key_ok,
    _normalise_path,
    build_routes,
    serve_api_blocking,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


# ── Test doubles ─────────────────────────────────────────────────────────────


class FakeConfig:
    """Minimal SCConfigManager stand-in backed by a nested dict."""

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
    """Collects log messages for assertions."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def log_message(self, message: str, verbosity: str = "summary") -> None:
        self.messages.append((verbosity, message))


ENDPOINTS = [
    {"Name": "Camera 1", "Path": "/motion/camera1", "Action": "Motion"},
    {"Name": "Camera 4", "Path": "/motion/camera4", "Action": "Ignore"},
    {"Name": "Start Siren", "Path": "/siren/start", "Action": "StartSiren"},
    {"Name": "Stop Siren", "Path": "/siren/stop", "Action": "StopSiren"},
]


# ── Unit tests: pure helpers ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("/motion/camera1", "/motion/camera1"), ("/motion/camera1/", "/motion/camera1"), ("/", "/")],
)
def test_normalise_path(raw: str, expected: str) -> None:
    """Trailing slashes are stripped, root stays '/'."""
    assert _normalise_path(raw) == expected


def test_build_routes_maps_paths_to_actions() -> None:
    """Routes map normalised paths to (name, action)."""
    routes = build_routes(FakeConfig({"ServiceAPI": {"Endpoints": ENDPOINTS}}), FakeLogger())  # type: ignore[arg-type]
    assert routes["/motion/camera1"] == ("Camera 1", EndpointAction.MOTION)
    assert routes["/siren/start"][1] == EndpointAction.START_SIREN


def test_build_routes_rejects_unknown_action() -> None:
    """An unrecognised action raises ValueError."""
    bad = [{"Name": "X", "Path": "/x", "Action": "Explode"}]
    with pytest.raises(ValueError, match="unknown action"):
        build_routes(FakeConfig({"ServiceAPI": {"Endpoints": bad}}), FakeLogger())  # type: ignore[arg-type]


def test_build_routes_skips_duplicate_path() -> None:
    """A duplicate path keeps the first definition and logs a warning."""
    dupes = [
        {"Name": "A", "Path": "/motion/x", "Action": "Motion"},
        {"Name": "B", "Path": "/motion/x", "Action": "Ignore"},
    ]
    logger = FakeLogger()
    routes = build_routes(FakeConfig({"ServiceAPI": {"Endpoints": dupes}}), logger)  # type: ignore[arg-type]
    assert routes["/motion/x"] == ("A", EndpointAction.MOTION)
    assert any("Duplicate" in msg for _v, msg in logger.messages)


def _headers(pairs: dict[str, str]) -> Message:
    """Build an email.message.Message carrying the given headers."""
    msg = Message()
    for key, value in pairs.items():
        msg[key] = value
    return msg


def test_access_key_open_when_unset() -> None:
    """No configured key means open access."""
    assert _access_key_ok(None, "", _headers({})) is True


def test_access_key_via_query() -> None:
    """A matching key in the query string is accepted."""
    assert _access_key_ok("secret", "key=secret", _headers({})) is True
    assert _access_key_ok("secret", "key=wrong", _headers({})) is False


def test_access_key_via_header() -> None:
    """A matching key in the header is accepted; missing is rejected."""
    assert _access_key_ok("secret", "", _headers({"X-Access-Key": "secret"})) is True
    assert _access_key_ok("secret", "", _headers({})) is False


# ── Integration tests: live server ───────────────────────────────────────────


def _free_port() -> int:
    """Return an unused localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RunningServer:
    """Manages a ServiceAPI server running in a background thread for tests."""

    def __init__(self, inbox: ServiceEventInbox, port: int) -> None:
        self.inbox = inbox
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path: str, headers: dict[str, str] | None = None) -> int:
        """Issue a GET request and return the HTTP status code."""
        req = urllib.request.Request(f"{self.base}{path}", headers=headers or {})  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)


@pytest.fixture
def running_server() -> Iterator[RunningServer]:
    """Start serve_api_blocking on an ephemeral port and tear it down after the test."""
    port = _free_port()
    wake_event = threading.Event()
    inbox = ServiceEventInbox(wake_event)
    stop_event = threading.Event()
    config = FakeConfig(
        {"ServiceAPI": {"Enable": True, "HostingIP": "127.0.0.1", "Port": port, "Endpoints": ENDPOINTS}}
    )
    logger = FakeLogger()

    thread = threading.Thread(
        target=serve_api_blocking,
        args=(inbox, config, logger, stop_event),
        daemon=True,
    )
    thread.start()

    server = RunningServer(inbox, port)
    _wait_until_ready(server)
    try:
        yield server
    finally:
        stop_event.set()
        thread.join(timeout=5)


def _wait_until_ready(server: RunningServer, timeout: float = 5.0) -> None:
    """Poll the server until it accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail("ServiceAPI server did not start in time")


def test_motion_endpoint_pushes_event(running_server: RunningServer) -> None:
    """A recognised motion path returns 200 and pushes an event to the inbox."""
    assert running_server.get("/motion/camera1") == 200
    events = running_server.inbox.drain()
    assert len(events) == 1
    assert events[0].action == EndpointAction.MOTION
    assert events[0].endpoint_name == "Camera 1"


def test_start_and_stop_endpoints(running_server: RunningServer) -> None:
    """Start/stop paths push the matching actions."""
    assert running_server.get("/siren/start") == 200
    assert running_server.get("/siren/stop") == 200
    actions = [e.action for e in running_server.inbox.drain()]
    assert actions == [EndpointAction.START_SIREN, EndpointAction.STOP_SIREN]


def test_unknown_path_returns_404(running_server: RunningServer) -> None:
    """An unconfigured path returns 404 and pushes nothing."""
    assert running_server.get("/nope") == 404
    assert running_server.inbox.drain() == []


def test_trailing_slash_is_tolerated(running_server: RunningServer) -> None:
    """A trailing slash still matches the configured endpoint."""
    assert running_server.get("/motion/camera1/") == 200
    assert len(running_server.inbox.drain()) == 1


@pytest.fixture
def keyed_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunningServer]:
    """A running server that requires ACCESS_KEY=secret."""
    monkeypatch.setenv("ACCESS_KEY", "secret")
    port = _free_port()
    wake_event = threading.Event()
    inbox = ServiceEventInbox(wake_event)
    stop_event = threading.Event()
    config = FakeConfig(
        {"ServiceAPI": {"Enable": True, "HostingIP": "127.0.0.1", "Port": port, "Endpoints": ENDPOINTS}}
    )
    logger = FakeLogger()
    thread = threading.Thread(
        target=serve_api_blocking, args=(inbox, config, logger, stop_event), daemon=True
    )
    thread.start()
    server = RunningServer(inbox, port)
    _wait_until_ready(server)
    try:
        yield server
    finally:
        stop_event.set()
        thread.join(timeout=5)


def test_access_key_required_when_set(keyed_server: RunningServer) -> None:
    """Without a key: 403. With the right key in query or header: 200."""
    assert keyed_server.get("/motion/camera1") == 403
    assert keyed_server.inbox.drain() == []

    assert keyed_server.get("/motion/camera1?key=secret") == 200
    assert len(keyed_server.inbox.drain()) == 1

    assert keyed_server.get("/motion/camera1", headers={"X-Access-Key": "secret"}) == 200
    assert len(keyed_server.inbox.drain()) == 1

    assert keyed_server.get("/motion/camera1?key=wrong") == 403
