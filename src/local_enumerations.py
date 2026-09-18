"""Local enumerations and constants for the eufy-siren app."""

from enum import StrEnum

CONFIG_FILE = "config.yaml"

# Name of the environment variable that, when set, requires callers to present a
# matching access key (via ``?key=`` query parameter or ``X-Access-Key`` header).
ACCESS_KEY_ENV_VAR = "ACCESS_KEY"

# Query-parameter name and header name used to carry the access key.
ACCESS_KEY_QUERY_PARAM = "key"
ACCESS_KEY_HEADER = "X-Access-Key"

# Name of the file (relative to the project root) that persists the runtime motion
# enable/disable state while ``General.MotionEventsControl`` is ``APIControl``, so an
# ``EnableMotion``/``DisableMotion`` decision survives an app restart or power failure.
SAVED_STATE_FILE = "saved-state.json"

# JSON field within ``saved-state.json`` holding the persisted motion-enabled flag.
SAVED_STATE_KEY = "motion_api_enabled"


class EndpointAction(StrEnum):
    """Action associated with a configured ServiceAPI endpoint."""

    MOTION = "Motion"
    START_SIREN = "StartSiren"
    STOP_SIREN = "StopSiren"
    RESET_SIREN = "ResetSiren"
    ENABLE_MOTION = "EnableMotion"
    DISABLE_MOTION = "DisableMotion"
    IGNORE = "Ignore"


class MotionEventsControl(StrEnum):
    """How motion events are gated (``General.MotionEventsControl``)."""

    DISABLED = "Disabled"  # Motion events are logged but never processed.
    ENABLED = "Enabled"  # Motion events are always processed (the default).
    API_CONTROL = (
        "APIControl"  # Motion is toggled at runtime via the ServiceAPI actions.
    )


class SirenState(StrEnum):
    """State of the siren control state machine."""

    IDLE = "Idle"  # Waiting for a qualifying trigger.
    SOUNDING = "Sounding"  # Siren is on; motion-following countdown is running.
    COOLDOWN = "Cooldown"  # Post-trigger lock-out; motion events are ignored.
