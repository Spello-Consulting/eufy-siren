I've read the brief and cross-checked it against the existing config, schema, and `main.py` shell. Overall it's a well-scoped, sensible project — the control flow is clear, the constraints (Apple Home Shortcuts can only do `Get contents of URL`) are realistic, and reusing `ThreadManager` + `sc-smart-device` with the PowerController project as a template is a solid foundation. Before any coding, here's what I'd want nailed down.

## Design questions worth resolving first

**1. Siren duration model — fixed vs. motion-following (the big one).**
The brief contradicts itself on how the siren turns off:
- Step 7: "turn off the switch a set period of time **after the motion events have stopped**" → motion-following with a trailing timeout.
- Config `SirenDuration: 30` with comment "How long to sound the siren" → a **fixed** duration.

These are different behaviours. If someone keeps triggering motion, does the siren (a) sound for exactly 30s and stop, or (b) stay on and keep resetting a trailing timer while motion continues? I'd recommend **(b) motion-following** — it matches the intent of a security siren — with `SirenDuration` reinterpreted (or renamed) as the trailing "quiet period" after the last motion event. Needs your call.

**2. `MinMotionInterval` as a *minimum* separation fights `MinMotionSources`.**
Requiring events be "separated by **at least** `MinMotionInterval` seconds" reads as a debounce so one camera's rapid-fire burst (Eufy cameras emit motion in bursts) can't self-satisfy `MinMotionEvents`. That's reasonable. But combined with `MinMotionSources: 2`, it means **two different cameras firing near-simultaneously — exactly the intruder-crossing-two-FOVs case — would be rejected** because they're <10s apart. I'd suggest making `MinMotionInterval` a **per-source debounce** (each camera can only contribute one event per interval) rather than a global gap between any two events. That resolves the tension. Worth an explicit decision.

**3. Window semantics.** "Within the `MinMotionInterval`/`MaxMotionInterval` window" — I'd define it concretely as: an event counts if it falls within `MaxMotionInterval` of the *previous qualifying* event (a sliding window that resets if the gap is exceeded). Confirm that's the intent.

**4. `PostTriggerSleepTimer` — cooldown or trailing timer?** The config comment ("before it can be triggered again" = a cooldown/lockout) and brief line 36 ("post trigger sleep interval") seem to describe the same thing, but it also overlaps conceptually with #1's trailing timeout. Clarify whether it's a hard lockout where motion is ignored entirely, and whether manual `StartSiren` bypasses it (I'd say yes — manual override should ignore cooldown).

**5. No API authentication.** The API binds `0.0.0.0:8085` and `/siren/start` is an unauthenticated GET — anyone on the LAN can sound (or, via `/siren/stop`, silence) the siren. Given Shortcuts can append a query/path token, do you want a shared-secret path segment or header? For a home LAN it may be acceptable, but it's a deliberate decision, not a default.

## Bugs/inconsistencies already in the repo (flagging, not fixing)

- **Schema typo breaks endpoint validation:** [config_schemas.py:26](src/config_schemas.py:26) declares `"Enpoints"` (missing *d*) but the config uses `Endpoints`. As-is, the endpoint list is never validated.
- **Heartbeat never enables:** [main.py:127](src/main.py:127) checks `heartbeat_config.get("Enabled", False)`, but the config key is `Enable`. Heartbeat monitoring will silently stay off.
- **Brief typo:** line 36 says StopSiren "will **start** the siren immediately" — should be **stop**.
- **Schema is partial:** `config_schemas.py` validates only `General`/`ServiceAPI`/`Siren` — `SCSmartDevices`, `Files`, `Email`, and `HeartbeatMonitor` have no schema. I assume the plan is to merge the `sc-smart-device` / `sc-foundation` library schemas in; worth confirming.
- **`Siren.Switch` → output cross-check:** FR line 34 requires `Switch` to reference a valid `SCSmartDevices.Devices[].Outputs[].Name`. Cerberus can't express that cross-reference, so it needs an explicit runtime validation step at startup (fail fast with a clear message).

## Minor notes

- Thread hand-off: the API thread will need to push motion events to the controller's state via a thread-safe structure (queue or locked list) plus the existing `wake_event`. Worth stating the shared-state ownership explicitly in the plan.
- `CONFIG_FILE = "config.yaml"` in `main.py` but the configs are `development.yaml`/`production.yaml` — presumably driven by `--config`, just noting there's no bare `config.yaml`.

If you're happy with my read, my suggested next step is that I turn this into a proper implementation plan in `docs/` (per your workflow) — but I'll hold for your answers on the five design questions above first, since #1 and #2 materially change the controller logic. Want me to draft the plan with my recommended defaults baked in, or would you rather settle those points first?