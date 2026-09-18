# Design: MotionEventsControl config option + API motion enable/disable

GitHub issue: [#4 — Enable and disable Eufy siren via API endpoint](https://github.com/Spello-Consulting/eufy-siren/issues/4)

Branch: `issue-4-motion-events-control`

## Summary

Two related changes:

1. **Replace** the boolean `General.DisableMotionEvents` config option with a three-valued
   `General.MotionEventsControl` string option.
2. **Extend** `ServiceAPI.Endpoints` with two new actions — `DisableMotion` and
   `EnableMotion` — that toggle motion processing at runtime.

## Behaviour

### `General.MotionEventsControl`

Allowed values (case-sensitive string, validated by the schema):

| Value        | Effect |
|--------------|--------|
| `Disabled`   | All motion events are logged but ignored (old `DisableMotionEvents: True`). |
| `Enabled`    | All motion events are processed normally (old `DisableMotionEvents: False`). |
| `APIControl` | Motion processing is toggled at runtime by the `DisableMotion` / `EnableMotion` API actions. |

If the key is **null or missing**, the default behaviour is **`Enabled`**.

`Enabled` and `Disabled` are *fixed* modes — config is authoritative.

### `APIControl` mode

- At startup (and whenever a config reload switches the mode **into** `APIControl` from
  another mode), motion processing starts **Disabled**, *unless a saved-state.json file exists*. The caller must send an
  `EnableMotion` request to arm motion.
- A config hot-reload that leaves the mode as `APIControl` **preserves** the current
  runtime enabled/disabled state (an API-set state is not clobbered by an unrelated edit).

### New API actions

| Action          | Effect                                                       |
| --------------- | ------------------------------------------------------------ |
| `DisableMotion` | In `APIControl` mode: disable motion processing (same net effect as `Disabled`). In fixed modes: **logged and ignored**. This state is written to a saved-state.json file. |
| `EnableMotion`  | In `APIControl` mode: enable motion processing (same net effect as `Enabled`). In fixed modes: **logged and ignored**. This state is written to a saved-state.json file. |

In the fixed `Enabled`/`Disabled` modes these two actions are logged at `summary`/`warning`
verbosity and have no effect — config remains authoritative. This keeps a clean separation:
only `APIControl` mode responds to the API toggles.

`StartSiren`, `StopSiren`, `ResetSiren` remain unaffected in every mode (manual control is
always available, exactly as `DisableMotionEvents` behaved before).

### saved-state.json

This file is used to preserve the state of the last DisableMotion / EnableMotion API end point call when General.MotionEventsControl is set to APIControl. This ensures that (for example) that motion events will be responded to if previously APIControl received a EnableMotion action and later the system restarted (eg. due to a power failure.)

If General.MotionEventsControl is changed to Disabled or Enabled (or it has this value when the app restarts or reloads the configuration), then saved-state.json will be deleted.

## Effective-state model

A single runtime helper decides whether an incoming `Motion` event is processed:

```
mode == Disabled    -> ignore motion (fixed)
mode == Enabled     -> process motion (fixed)
mode == APIControl  -> process motion iff runtime flag `_motion_api_enabled` is True
                       (flag restored from saved-state.json at startup, else False;
                        toggled by EnableMotion/DisableMotion, which re-persist it)
```

`DisableMotion`/`EnableMotion` events only mutate `_motion_api_enabled`, and only when the
mode is `APIControl`; otherwise they are logged and dropped.

## Files to change

### `src/local_enumerations.py`
- Add `DISABLE_MOTION = "DisableMotion"` and `ENABLE_MOTION = "EnableMotion"` to
  `EndpointAction`.
- Add a `MotionEventsControl` `StrEnum` with `DISABLED`, `ENABLED`, `API_CONTROL`.
- Add a `SAVED_STATE_FILE = "saved-state.json"` constant (the default filename, resolved
  relative to the project root — see the controller notes below).
- Add a `SAVED_STATE_KEY = "motion_api_enabled"` constant for the JSON field name.

### `src/config_schemas.py`
- Remove `DisableMotionEvents` from the `General` schema.
- Add `MotionEventsControl`: `{"type": "string", "required": False, "nullable": True,
  "allowed": ["Disabled", "Enabled", "APIControl"]}`.
- Add `"DisableMotion"` and `"EnableMotion"` to the `ServiceAPI.Endpoints[].Action`
  `allowed` list.

### `src/siren_controller.py`
- Constructor: add an injectable `state_path: Path | None = None` argument (mirrors the
  injectable `time_fn`, so tests can point it at a `tmp_path`). When `None`, default to
  `Path(SCCommon.get_project_root()) / SAVED_STATE_FILE`. Initialise
  `self._motion_api_enabled = False` and `self._motion_mode = None` **before** the first
  `_load_settings()` call.
- In `_load_settings`: read `General.MotionEventsControl` (default `Enabled`), parse to the
  new enum (unknown/invalid → `Enabled`, with a warning). Replace the
  `self.disable_motion_events` bool with `self._motion_mode` and the runtime
  `self._motion_api_enabled` flag. Handle mode transitions and the saved-state file:
  - **Not `APIControl`** (fixed `Enabled`/`Disabled`): clear the runtime flag and **delete**
    `saved-state.json` (via `_delete_saved_state()` — no-op if absent). Config is
    authoritative, so no stale state must survive.
  - **`APIControl`, entering it** (previous mode is `None` at construction, or was a fixed
    mode before this reload): **restore** the runtime flag from `saved-state.json` via
    `_read_saved_state()` — the saved bool if the file exists and is readable, else `False`
    (Disabled). This is what lets an `EnableMotion` survive a restart/power failure.
  - **`APIControl`, already in it** (mode unchanged across a reload): leave the runtime flag
    untouched (preserve API-set state across unrelated edits); do not re-read the file.
  - Record `self._motion_mode = new_mode` last, so the next reload can detect a transition.
- Add saved-state helpers (all wrapped so a filesystem error is logged as a warning and
  never crashes the controller thread):
  - `_read_saved_state() -> bool`: `JSONEncoder.read_from_file(self._state_path)`; returns
    the saved `SAVED_STATE_KEY` bool, or `False` when the file is missing/unreadable/malformed.
  - `_write_saved_state() -> None`: `JSONEncoder.save_to_file({SAVED_STATE_KEY:
    self._motion_api_enabled, "saved_at": <UTC datetime>}, self._state_path)` (atomic
    `.tmp` write, datetime-preserving — matches the house JSON helper).
  - `_delete_saved_state() -> None`: `self._state_path.unlink(missing_ok=True)`.
- Add `_motion_currently_enabled() -> bool` implementing the effective-state model above.
- In `_handle_event`: route `EndpointAction.ENABLE_MOTION` / `DISABLE_MOTION` to a new
  `_handle_motion_control(enable, event)` method. In `APIControl` mode it sets the flag and
  calls `_write_saved_state()`; in a fixed mode it logs at `warning`/`summary` and ignores
  the request (config authoritative).
- In the `MOTION` branch: gate on `_motion_currently_enabled()` instead of
  `self.disable_motion_events`.
- Startup log line: report the configured mode (and, for `APIControl`, whether motion is
  currently enabled or disabled, reflecting any restored saved state).

### `src/main.py`
- No functional change required: the controller resolves its own default `state_path`. (The
  project root is already established via `SCCommon.get_project_root()` /
  `SC_FOUNDATION_PROJECT_ROOT`, which `--homedir` sets.)

### Config files
- `configs/development.yaml`, `configs/production.yaml`: replace
  `General.DisableMotionEvents: False` with `General.MotionEventsControl: Enabled`.
- Add example `DisableMotion` / `EnableMotion` endpoints to the dev config so the feature is
  discoverable: `/motion/disable` → `DisableMotion`, `/motion/enable` → `EnableMotion`.

### `.gitignore`
- Add `saved-state.json` (runtime state, must not be committed), under the `# App` section
  alongside `logs/`.

### Documentation (`README.md`)
- Replace the `DisableMotionEvents` prose and config sample with `MotionEventsControl`,
  documenting the three modes and the default.
- Add `DisableMotion` / `EnableMotion` rows to the **Endpoint actions** table and note that
  they only take effect in `APIControl` mode.
- Document `saved-state.json`: what it stores, that it only exists in `APIControl` mode,
  that it is restored on restart and deleted when the mode is `Enabled`/`Disabled`, and that
  it lives at the project root and is gitignored.
- Update the hot-reload note (which currently references flipping `DisableMotionEvents`).

## Tests

### `tests/test_config.py`
- Add: `MotionEventsControl: APIControl` (and `Enabled`/`Disabled`) validate.
- Add: an invalid `MotionEventsControl` value is rejected.
- Add: `DisableMotion`/`EnableMotion` endpoint actions validate.

### `tests/test_siren_controller.py`
- Update `config_data` / `make_controller` helpers: replace the `disable_motion_events:
  bool` parameter with `motion_events_control: str = "Enabled"`. `make_controller` (and
  `controller_with_config`) gain a `state_path: Path` so each test gets an isolated
  `saved-state.json` under `tmp_path` (no shared/project-root file).
- Rework the existing `DisableMotionEvents` tests to the `Disabled` mode.
- New tests:
  - `Disabled` mode: motion ignored; StartSiren/StopSiren/ResetSiren still work.
  - `Enabled` mode (default): two-source motion triggers as today.
  - `APIControl` mode: motion ignored at startup; after `EnableMotion` it triggers; after
    `DisableMotion` it is ignored again.
  - Fixed modes: `EnableMotion`/`DisableMotion` are logged and ignored (no state change).
  - Hot-reload: switching a fixed mode ↔ `APIControl` resets the runtime flag correctly;
    an unrelated reload while in `APIControl` preserves the API-set flag.
  - **saved-state persistence**:
    - `EnableMotion` in `APIControl` writes `saved-state.json` with the enabled flag;
      `DisableMotion` rewrites it to disabled.
    - A fresh controller in `APIControl` with a pre-existing `saved-state.json` (enabled)
      **restores** motion as enabled at startup (simulates a restart after a power failure).
    - Starting/reloading into a fixed `Enabled`/`Disabled` mode **deletes** an existing
      `saved-state.json`.
    - Fixed-mode `EnableMotion`/`DisableMotion` requests do **not** write the file.
    - A missing/corrupt `saved-state.json` is treated as disabled (no crash, warning logged).

### `tests/test_service_api.py`
- Add `DisableMotion`/`EnableMotion` to the routing tests (paths map to the new actions).

## Backwards compatibility

`General.DisableMotionEvents` is **removed**, not aliased. Any config still setting it will
now fail schema validation (unknown key) — this is an intentional breaking change per the
issue ("replace"). The migration is a one-line edit in each config file, covered above for
the shipped configs.

## Out of scope

- No change to the trigger logic, siren state machine, alerting, or access-key handling.
- `DisableMotion`/`EnableMotion` do not touch the siren directly; they only gate whether
  `Motion` events are counted.
