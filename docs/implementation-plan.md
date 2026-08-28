# Eufy Siren — Implementation Plan

Derived from `docs/design-brief.md`. This is the build plan for review; code follows the
same conventions as `~/dev/PowerController` and reuses the `sc-foundation-services` and
`sc-smart-device` libraries.

## 1. Architecture overview

Three worker threads orchestrated by `sc_foundation.ThreadManager` (mirrors PowerController's
`main.py`), sharing two `threading.Event`s (`wake_event`, `stop_event`):

| Thread | Target | Restart policy | Role |
|--------|--------|----------------|------|
| `smart device` | `SmartDeviceWorker.run` (library) | `on_crash`, 3 | Executes output changes on the Shelly/Tasmota switch via a request queue. |
| `controller` | `SirenController.run` | `never` | Master orchestrator: drains service events, runs the motion→siren state machine, drives the switch, pings the heartbeat. |
| `service api` | `serve_api_blocking` | `on_crash`, 3 | Stdlib HTTP server answering Apple Home GET requests; pushes events to the controller. |

### Thread hand-off (per brief §Technical Requirements)
- `ServiceEventInbox` — a **locked-list** structure (list + `threading.Lock`), same pattern as
  `SmartDeviceView`'s thread-safe snapshot. The API thread `push()`es a `ServiceEvent` and sets
  `wake_event`; the controller `drain()`s the full list each tick and clears it.
- Switch state flows the other way via `worker.get_latest_status()` → `SmartDeviceView` (already
  thread-safe), used for runtime validation and to read/confirm output state.

### Why no web framework
The API is six trivial GET endpoints on the LAN with no UI, websockets, or templates (brief §43:
"no requirement for a web UI"). PowerController pulls in FastAPI/uvicorn only for its web UI —
neither is installed here. We use the stdlib `http.server.ThreadingHTTPServer`: zero new
dependencies, trivial to run in a managed thread, easy to test. **Decision point — flag if you'd
prefer FastAPI for cross-project consistency.**

## 2. Modules (`src/`, flat layout, `pythonpath=["src"]`)

- `main.py` *(extend existing shell)* — arg parsing (done), schema merge, config+logger init,
  `SCSmartDevice` + `SmartDeviceWorker` + `SirenController` + service API construction, ThreadManager
  wiring, supervisory loop, graceful shutdown (turn siren **off** on exit).
- `config_schemas.py` *(refine existing)* — local Cerberus schema for `General`, `ServiceAPI`,
  `Siren`. Merged at top level in `main.py` with `sc_foundation.yaml_config_validation`
  (Files/Email/HeartbeatMonitor) and `sc_smart_device.smart_devices_validator` (SCSmartDevices) via
  dict-unpack (`{**a, **b, **c}` — no key overlap, so `mergedeep` isn't needed).
- `local_enumerations.py` *(new)* — `CONFIG_FILE`, `EndpointAction` (`Motion`/`StartSiren`/
  `StopSiren`/`Ignore`), `SirenState` (`IDLE`/`SOUNDING`/`COOLDOWN`).
- `event_inbox.py` *(new)* — `ServiceEvent` dataclass (`action`, `endpoint_name`, `path`, `ts`) +
  `ServiceEventInbox` (locked list; `push`, `drain`).
- `motion_tracker.py` *(new)* — **pure, dependency-free** motion-window logic (unit-testable with no
  threads/HTTP/clock). Tracks qualifying events and evaluates the trigger condition.
- `service_api.py` *(new)* — `ThreadingHTTPServer` + handler; endpoint routing from config;
  access-key check; request logging; `serve_api_blocking(inbox, config, logger, stop_event)` with
  cooperative shutdown.
- `siren_controller.py` *(new)* — `SirenController` state machine and tick loop.

## 3. Motion-trigger logic (`motion_tracker.py`)

Implements brief FR bullets. Config: `MinMotionEvents`, `MinMotionSources`, `MinMotionInterval`
(per-source debounce), `MaxMotionInterval` (sliding-window gap).

- Each incoming `Motion` event carries its source (endpoint name) and timestamp.
- **Per-source debounce:** an event is ignored if that same source produced a qualifying event
  less than `MinMotionInterval` seconds ago.
- **Sliding window:** a qualifying event extends the current window if it is within
  `MaxMotionInterval` of the previous qualifying event; otherwise the window resets and starts fresh.
- **Trigger** when the current window holds ≥ `MinMotionEvents` qualifying events **and** those
  events span ≥ `MinMotionSources` unique sources.
- Pure functions over an in-memory list of `(source, ts)`; time passed in as an argument (no direct
  clock reads) so tests are deterministic.

## 4. Siren state machine (`siren_controller.py`)

States: `IDLE → SOUNDING → COOLDOWN → IDLE`. Timing driven by the tick loop
(`General.PollingInterval`, default 10 s) plus immediate `wake_event` wakes on new events. Clock is
injected (`time_fn`, default `time.monotonic`) for deterministic tests.

- **IDLE** — feed `Motion` events to `MotionTracker`. On `should_trigger()` → start siren, set
  `last_trigger_ts = now`, → **SOUNDING**.
- **SOUNDING** — siren on. Any `Motion` event resets `last_trigger_ts = now` (motion-following; no
  need to re-satisfy the full trigger). When `now − last_trigger_ts ≥ SirenDuration` → stop siren →
  **COOLDOWN**. `StopSiren` → stop siren immediately → **COOLDOWN**.
- **COOLDOWN** (`PostTriggerSleepTimer`) — `Motion` events ignored (still logged). When elapsed →
  **IDLE** (tracker reset). `StartSiren` clears cooldown → start siren → **SOUNDING**.
- **`StartSiren`** (any state) — start siren immediately, bypassing motion conditions → **SOUNDING**.
- **Start/stop siren** = submit a `DeviceSequenceRequest([DeviceStep(StepKind.CHANGE_OUTPUT,
  {"output_identity": Siren.Switch, "state": True/False}, retries=…)])` to the worker.
- Every tick calls `logger.ping_heartbeat()` (SCLogger throttles to `HeartbeatMonitor.Frequency`).

## 5. Service API (`service_api.py`)

- Routes built from `ServiceAPI.Endpoints` (`Path → (Name, Action)`).
- Recognized path → push `ServiceEvent`, set `wake_event`, respond `200` (fast ack; siren work
  happens in the controller thread). `Ignore` action is pushed+logged but never counts.
- Unknown path → `404`; still logged.
- **Access key** (brief §48) — if `ACCESS_KEY` env var is set, request must supply it via `?key=…`
  **or** an `X-Access-Key` header; mismatch/missing → `403`. If unset → open. Follows
  PowerController's `_validate_access_key`.
- **Logging** (brief §36) — every request logged with method, matched endpoint/action (or
  "unknown"), and client IP. The URL **path** is logged (not the raw query string), so the access
  key never lands in the logfile.
- Cooperative shutdown: background thread waits on `stop_event` then calls `server.shutdown()`.

## 6. Runtime validation (not Cerberus — brief §37)

At controller startup, after the first `SmartDeviceView` snapshot is available:
- `Siren.Switch` must resolve via `view.validate_output_id(...)`; else `log_fatal_error`.
- `ServiceAPI.Endpoints` paths unique and non-empty; `Siren.Switch` present.
- `MinMotionSources ≤ MinMotionEvents` sanity check (a warning, not fatal).

## 7. Tests (`tests/`, pytest)

- `test_motion_tracker.py` — single/multi-source, per-source debounce, min/max-interval windowing,
  `MinMotionEvents`/`MinMotionSources` combinations, window reset.
- `test_siren_controller.py` — full state machine against a stub worker (captures submitted
  requests) and an injected clock: motion trigger, motion-following reset, duration expiry,
  StartSiren/StopSiren overrides, cooldown lockout + StartSiren clearing it. `Simulate=True` devices.
- `test_service_api.py` — real server on an ephemeral port: endpoint routing, event pushed to inbox,
  access-key enforcement (query + header + reject), unknown path 404, all-requests-logged.
- `test_config.py` — merged-schema validation (valid config passes; bad enum/range fails).
- `test_main.py` — existing arg-parsing tests retained.

## 8. README (brief §51)

Context; hardware wiring (switch → 12 V PSU → siren); install via `uv`; running (`scripts/launch.sh`,
systemd unit in `deploy/`); full config reference; `ACCESS_KEY` via 1Password/op; and setup guides
for **Eufy Security → Apple Home (HomeKit)** and the **per-camera Apple Home automation → Shortcut
"Get contents of URL"** pointing at each `/motion/cameraN` endpoint.

## 9. Out of scope / assumptions

- No web UI, no persistent state file (siren state is in-memory; a restart begins IDLE).
- Secrets (`ACCESS_KEY`, SMTP, heartbeat URL) come from the environment via `op run` in
  `scripts/launch.sh`; not written to disk.
- One switch/output drives the siren; multi-output sequences are not required.
