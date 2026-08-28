# eufy-siren

Sound a **loud, wired siren** when your Eufy security cameras detect motion — and only
when the conditions you configure are met.

The built-in [Eufy Security siren](https://www.eufy.com/au/products/eufy-security-siren-105-db-wireless-alarm)
is too quiet, and the Eufy ecosystem has very limited options for driving an external
alarm. This app bridges that gap: it exposes a tiny HTTP API that Apple Home automations
call when a Eufy camera sees motion, applies your trigger rules, and switches a smart
relay (Shelly or Tasmota) that powers a real wired siren.

## How it works

```
Eufy camera → Apple Home (HomeKit) → Home automation → Shortcut ("Get contents of URL")
      → http://<host>:8085/motion/cameraN → eufy-siren → smart switch → 12V PSU → siren
```

1. Eufy cameras are exposed to Apple Home via
   [HomeKit on eufySecurity devices](https://service.eufy.com/article-description/HomeKit-on-eufySecurity-Devices).
2. For each camera, an Apple Home automation runs a Shortcut with a **Get contents of URL**
   action pointing at one of this app's endpoints (e.g. `http://192.168.86.99:8085/motion/camera1`).
3. When a camera detects motion, its endpoint is called.
4. When the configured conditions are met (for example, motion from **two different cameras**
   within a time window), the app turns the smart switch **on**.
5. The switch powers a 12 V PSU, which drives the wired siren.
6. The siren is **motion-following**: it stays on while motion continues and turns off a set
   time after the last motion event (or when a stop endpoint is called).

### Trigger logic

The siren starts when enough qualifying motion events arrive:

- **`MinMotionEvents`** — how many motion events are required.
- **`MinMotionSources`** — how many *distinct* endpoints (cameras) must contribute.
- **`MinMotionInterval`** — a *per-source debounce*: a single camera can contribute at most
  one event per this many seconds, so one camera's rapid burst can't trigger on its own.
- **`MaxMotionInterval`** — events must arrive within this many seconds of the previous
  qualifying event; a longer gap resets the window.

Once sounding, any further motion event resets the **`SirenDuration`** countdown. After the
siren stops (either the countdown elapses or a stop endpoint is called), a
**`PostTriggerSleepTimer`** cooldown begins during which motion is ignored — a `StartSiren`
request clears the cooldown and sounds the siren immediately.

### Architecture

A multi-threaded app orchestrated by `sc_foundation.ThreadManager`:

| Thread | Responsibility |
|--------|----------------|
| **controller** | Applies the trigger logic and siren state machine; drives the switch; pings the heartbeat. |
| **smart device** | `sc_smart_device.SmartDeviceWorker` — performs the actual switch changes. |
| **service api** | Stdlib HTTP server answering the Apple Home GET requests. |

The ServiceAPI hands events to the controller through a thread-safe inbox; switch state is
read back via the worker's thread-safe `SmartDeviceView` snapshot.

## Hardware

```
[Smart switch: Shelly 2PM G3]  →  [12V PSU]  →  [Wired siren]
```

Wire the siren so it sounds when the configured switch **output is energised**. The switch
is controlled over your LAN by the `sc-smart-device` library; no cloud account is needed.

## Prerequisites

- Python 3.13+ — `brew install python@3.13`
- [uv](https://docs.astral.sh/uv/) — `brew install uv`
- A Shelly or Tasmota smart switch on the same LAN
- Eufy cameras added to Apple Home (see [Apple Home setup](#apple-home-setup))

`scripts/launch.sh` runs `uv sync` to install dependencies automatically.

> **macOS note:** to let the app reach the local network, allow your terminal/IDE under
> *System Settings → Privacy and Security → Local Network*.

## Configuration

Config lives in [configs/development.yaml](configs/development.yaml) and
[configs/production.yaml](configs/production.yaml). Secrets are **not** stored in YAML —
they come from the environment (see [Environment files](#environment-files)).

Key sections:

```yaml
SCSmartDevices:                 # The smart switch(es) — validated by sc-smart-device
  Devices:
    - Name: Spello Siren
      Model: Shelly2PMG3
      Hostname: 192.168.86.36
      Simulate: False           # set True to simulate the switch (used by the test suite)
      Outputs:
        - Name: "Spello Siren O1"
        - Name: "Spello Siren O2"

ServiceAPI:
  Enable: True
  HostingIP: 0.0.0.0            # bind address (0.0.0.0 = all interfaces)
  Port: 8085
  Endpoints:
    - Name: "Camera 1"
      Path: "/motion/camera1"
      Action: "Motion"          # Motion | StartSiren | StopSiren | Ignore
    - Name: "Start Siren"
      Path: "/siren/start"
      Action: "StartSiren"
    - Name: "Stop Siren"
      Path: "/siren/stop"
      Action: "StopSiren"

Siren:
  Enable: True
  Switch: "Spello Siren O1"      # MUST match an output name under SCSmartDevices.Devices[].Outputs[]
  SirenDuration: 30             # seconds to keep sounding after the last triggering event
  MinMotionEvents: 2            # min qualifying events to trigger
  MinMotionSources: 2           # min distinct cameras among those events
  MinMotionInterval: 10         # per-source debounce (seconds)
  MaxMotionInterval: 60         # max gap between qualifying events (seconds)
  PostTriggerSleepTimer: 60     # cooldown after the siren stops (seconds)
```

`Siren.Switch` is validated at startup against the configured outputs; a mismatch is a
fatal error. The `Files`, `Email` and `HeartbeatMonitor` sections are handled by
`sc-foundation-services` (logging, email alerts, and an uptime heartbeat ping).

### Endpoint actions

| Action | Effect |
|--------|--------|
| `Motion` | A motion event; counts toward the trigger conditions. |
| `StartSiren` | Sound the siren immediately, ignoring motion conditions (and clearing any cooldown). |
| `StopSiren` | Stop the siren immediately and begin the cooldown. |
| `Ignore` | Logged but never counted (useful to wire up a camera without arming it). |

Every request — recognised or not — is logged. Unknown paths return `404`.

### Access key (optional)

If an `ACCESS_KEY` environment variable is set, callers must present it, either as a query
parameter or a header; otherwise the endpoints are open on the LAN:

```
http://192.168.86.99:8085/motion/camera1?key=YOUR_KEY
# or
curl -H "X-Access-Key: YOUR_KEY" http://192.168.86.99:8085/motion/camera1
```

The key is supplied from 1Password via the `.env` templates (`ACCESS_KEY=op://...`) and is
never written to disk. The request **path** is logged, but the query string is not, so the
key does not land in the logfile.

## Environment files

The app loads a `.env` file (via `python-dotenv`). Two templates are provided
(`.env.dev.template`, `.env.prod.template`) using 1Password `op://` references, plus
`tests/.env.test.template` for the test suite. Select one per deployment:

```bash
ln -s .env.dev.template .env.target
```

`scripts/launch.sh` injects these via `op run`, so secrets live only in memory.

## Running the app

```bash
./scripts/launch.sh
```

This selects the config file (via `APP_CONFIG`), runs `uv sync`, and launches the app. To
run directly against a specific config:

```bash
uv run python src/main.py --config configs/development.yaml
```

## Apple Home setup

### 1. Expose Eufy cameras to Apple Home

Follow Eufy's guide:
[HomeKit on eufySecurity devices](https://service.eufy.com/article-description/HomeKit-on-eufySecurity-Devices).
In the Eufy Security app, add each camera to HomeKit; it then appears in the Apple **Home**
app with a **Motion** sensor.

### 2. Create a per-camera automation that calls this app

For **each** camera you want to arm, in the Apple **Home** app:

1. **Automation → +  → Add Automation → “An Accessory Detects Motion”**.
2. Choose the camera's motion sensor and the times/conditions you want.
3. Scroll to the bottom and choose **Convert to Shortcut** (this unlocks custom actions).
4. Remove any default actions and add a **Get Contents of URL** action.
5. Set the URL to this app's endpoint for that camera, for example:
   `http://192.168.86.99:8085/motion/camera1`
   (add `?key=YOUR_KEY` if you configured an access key).
6. Leave the method as **GET** and save.

Repeat for each camera, pointing at its own `/motion/cameraN` endpoint (matching the
`ServiceAPI.Endpoints` paths in your config). You can also create automations/Shortcuts for
the `/siren/start` and `/siren/stop` endpoints (e.g. from a Home button or Siri phrase).

> **Tip:** the device running eufy-siren should have a static/reserved IP so the URLs in
> your automations stay valid.

## Development

```bash
uv sync --extra all           # install dev dependencies
uv run ruff format src tests  # format
uv run ruff check src tests   # lint
uv run mypy --strict src      # type-check
uv run pytest                 # run the test suite
```

The test suite simulates the smart switch (`SCSmartDevices.Devices[].Simulate = True`), so
no hardware is required.

## Running via systemd

To run automatically at boot (e.g. on a Raspberry Pi):

```bash
sudo cp deploy/eufy-siren.service /etc/systemd/system/eufy-siren.service
sudo nano /etc/systemd/system/eufy-siren.service   # adjust paths/user
sudo systemctl daemon-reload
sudo systemctl enable eufy-siren
sudo systemctl start eufy-siren
journalctl -u eufy-siren -f                         # view logs
```
