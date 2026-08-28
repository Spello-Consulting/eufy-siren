"""The ServiceAPI: a small HTTP server answering Apple Home GET requests.

Apple Home automations invoke a Shortcut whose "Get contents of URL" action calls one of the
endpoints configured under ``ServiceAPI.Endpoints``. This module maps those paths to actions,
enforces an optional access key, logs every request, and hands each recognised request to the
controller via a :class:`~event_inbox.ServiceEventInbox`.

It uses the standard-library :class:`http.server.ThreadingHTTPServer` — the API is a handful of
trivial GET endpoints with no UI, so no web framework is needed. Run
:func:`serve_api_blocking` as the ``service api`` thread target.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import socketserver
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from event_inbox import ServiceEvent
from local_enumerations import (
    ACCESS_KEY_ENV_VAR,
    ACCESS_KEY_HEADER,
    ACCESS_KEY_QUERY_PARAM,
    EndpointAction,
)

if TYPE_CHECKING:
    from email.message import Message

    from sc_foundation import SCConfigManager, SCLogger

    from event_inbox import ServiceEventInbox


def _normalise_path(path: str) -> str:
    """Normalise a URL path for routing (strip a trailing slash, keep root as '/').

    Args:
        path: The raw URL path.

    Returns:
        The normalised path.
    """
    stripped = path.rstrip("/")
    return stripped or "/"


@dataclass(frozen=True)
class _ApiContext:
    """Per-server context shared with the request handler.

    Attributes:
        routes: Mapping of normalised path → (endpoint name, action).
        inbox: Inbox to push recognised events onto.
        logger: Logger for request logging.
        access_key: Required access key, or ``None`` for open access.
    """

    routes: dict[str, tuple[str, EndpointAction]]
    inbox: ServiceEventInbox
    logger: SCLogger
    access_key: str | None


class _ApiServer(ThreadingHTTPServer):
    """Threading HTTP server carrying an :class:`_ApiContext`."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        context: _ApiContext,
    ) -> None:
        self.context = context
        super().__init__(server_address, handler_class)

    def server_bind(self) -> None:
        """Bind the socket without the reverse-DNS lookup that HTTPServer performs.

        ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)``, a reverse-DNS lookup that
        can block for seconds (or hang) on hosts without working reverse DNS — undesirable
        for a service that should start promptly on a LAN. We don't need the FQDN, so we bind
        via the plain TCP server and record the host as the server name directly.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _ServiceApiHandler(BaseHTTPRequestHandler):
    """Handles GET requests, routing them to configured endpoint actions."""

    server: _ApiServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802  (name mandated by BaseHTTPRequestHandler)
        """Route a GET request to its configured action, or 403/404."""
        ctx = self.server.context
        client = self.client_address[0] if self.client_address else "?"
        try:
            parsed = urlsplit(self.path)
            path = _normalise_path(parsed.path)

            if not _access_key_ok(ctx.access_key, parsed.query, self.headers):
                ctx.logger.log_message(
                    f"API GET {path} from {client}: rejected — missing/invalid access key.",
                    "warning",
                )
                self._respond(HTTPStatus.FORBIDDEN, "Forbidden")
                return

            route = ctx.routes.get(path)
            if route is None:
                ctx.logger.log_message(
                    f"API GET {path} from {client}: 404 — no matching endpoint.", "warning"
                )
                self._respond(HTTPStatus.NOT_FOUND, "Not Found")
                return

            endpoint_name, action = route
            ctx.logger.log_message(
                f"API GET {path} from {client}: matched '{endpoint_name}' (action={action}).",
                "summary",
            )
            ctx.inbox.push(ServiceEvent(action=action, endpoint_name=endpoint_name, path=path))
            self._respond(HTTPStatus.OK, "OK")
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before we responded; nothing useful to do.
            ctx.logger.log_message(f"API GET from {client}: client disconnected.", "debug")
        except Exception as exc:  # noqa: BLE001  (never let one bad request kill the thread)
            ctx.logger.log_message(f"API GET from {client}: handler error: {exc}", "error")
            with contextlib.suppress(Exception):
                self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal Server Error")

    def _respond(self, status: HTTPStatus, message: str) -> None:
        """Send a minimal plain-text response.

        Args:
            status: HTTP status code to send.
            message: Short plain-text body.
        """
        body = f"{message}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the handler's default stderr logging (we log via SCLogger)."""


def _access_key_ok(expected: str | None, query: str, headers: Message) -> bool:
    """Check the request's access key against the expected value.

    Args:
        expected: The required key, or ``None`` for open access.
        query: The raw URL query string.
        headers: The request headers.

    Returns:
        ``True`` if access is allowed.
    """
    if expected is None:
        return True
    provided = parse_qs(query).get(ACCESS_KEY_QUERY_PARAM, [None])[0]
    if provided is None:
        provided = headers.get(ACCESS_KEY_HEADER)
    if provided is None:
        return False
    return hmac.compare_digest(provided, expected)


def _load_access_key(logger: SCLogger) -> str | None:
    """Load the access key from the environment.

    Args:
        logger: Logger for a one-line status message.

    Returns:
        The access key if set and non-empty, else ``None`` (open access).
    """
    raw = os.environ.get(ACCESS_KEY_ENV_VAR, "").strip()
    if raw:
        logger.log_message("ServiceAPI access key is set — requests must present it.", "summary")
        return raw
    logger.log_message(
        f"ServiceAPI access key not set ({ACCESS_KEY_ENV_VAR}) — endpoints are open.", "summary"
    )
    return None


def build_routes(
    config: SCConfigManager, logger: SCLogger
) -> dict[str, tuple[str, EndpointAction]]:
    """Build the path → (name, action) routing table from config.

    Args:
        config: The configuration manager.
        logger: Logger for warnings about malformed or duplicate endpoints.

    Returns:
        Mapping of normalised path to (endpoint name, action). Duplicate paths keep the
        first definition; later duplicates are logged and skipped.

    Raises:
        ValueError: If an endpoint has an unrecognised action.
    """
    endpoints = config.get("ServiceAPI", "Endpoints", default=[]) or []
    routes: dict[str, tuple[str, EndpointAction]] = {}
    for entry in endpoints:
        name = str(entry.get("Name", "")).strip()
        raw_path = str(entry.get("Path", "")).strip()
        raw_action = str(entry.get("Action", "")).strip()
        if not raw_path or not name:
            logger.log_message(f"Skipping endpoint with missing Name/Path: {entry!r}.", "warning")
            continue
        try:
            action = EndpointAction(raw_action)
        except ValueError as exc:
            msg = f"Endpoint '{name}' has unknown action '{raw_action}'."
            raise ValueError(msg) from exc
        path = _normalise_path(raw_path)
        if path in routes:
            logger.log_message(
                f"Duplicate endpoint path '{path}' ({name}) — keeping first definition.", "warning"
            )
            continue
        routes[path] = (name, action)
    return routes


def serve_api_blocking(
    inbox: ServiceEventInbox,
    config: SCConfigManager,
    logger: SCLogger,
    stop_event: threading.Event,
) -> None:
    """Run the ServiceAPI HTTP server until ``stop_event`` is set.

    Args:
        inbox: Inbox to push recognised events onto.
        config: The configuration manager.
        logger: The logger.
        stop_event: Event that, when set, cooperatively shuts the server down.
    """
    if not config.get("ServiceAPI", "Enable", default=True):
        logger.log_message("ServiceAPI is disabled — not starting the HTTP server.", "summary")
        return

    host_raw = config.get("ServiceAPI", "HostingIP", default="0.0.0.0")  # noqa: S104
    host = host_raw if isinstance(host_raw, str) and host_raw else "0.0.0.0"  # noqa: S104
    port = int(config.get("ServiceAPI", "Port", default=8085) or 8085)

    routes = build_routes(config, logger)
    if not routes:
        logger.log_message("ServiceAPI has no valid endpoints configured.", "warning")

    context = _ApiContext(routes, inbox, logger, _load_access_key(logger))
    server = _ApiServer((host, port), _ServiceApiHandler, context)

    def _watch_for_stop() -> None:
        stop_event.wait()
        server.shutdown()

    watcher = threading.Thread(target=_watch_for_stop, name="service-api-stop", daemon=True)
    watcher.start()

    logger.log_message(
        f"ServiceAPI listening on http://{host}:{port} with {len(routes)} endpoint(s).", "summary"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        logger.log_message("ServiceAPI shutdown complete.", "detailed")
