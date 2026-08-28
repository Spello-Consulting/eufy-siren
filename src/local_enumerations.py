"""Local enumerations and constants for the eufy-siren app."""

from enum import StrEnum

CONFIG_FILE = "config.yaml"

# Name of the environment variable that, when set, requires callers to present a
# matching access key (via ``?key=`` query parameter or ``X-Access-Key`` header).
ACCESS_KEY_ENV_VAR = "ACCESS_KEY"

# Query-parameter name and header name used to carry the access key.
ACCESS_KEY_QUERY_PARAM = "key"
ACCESS_KEY_HEADER = "X-Access-Key"


class EndpointAction(StrEnum):
    """Action associated with a configured ServiceAPI endpoint."""

    MOTION = "Motion"
    START_SIREN = "StartSiren"
    STOP_SIREN = "StopSiren"
    IGNORE = "Ignore"


class SirenState(StrEnum):
    """State of the siren control state machine."""

    IDLE = "Idle"          # Waiting for a qualifying trigger.
    SOUNDING = "Sounding"  # Siren is on; motion-following countdown is running.
    COOLDOWN = "Cooldown"  # Post-trigger lock-out; motion events are ignored.
