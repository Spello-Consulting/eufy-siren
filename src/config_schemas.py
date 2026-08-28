"""Configuration schemas for use with the SCConfigManager class."""


class ConfigSchema:
    """Base class for configuration schemas."""

    def __init__(self) -> None:

        self.validation = {
            "General": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "AppName": {"type": "string", "required": False, "nullable": True},
                    "PollingInterval": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 3600},
                },
            },
            "SMS": {
                "type": "dict",
                "required": False,
                "schema": {
                    "EnableSMS": {"type": "boolean", "required": False, "nullable": True},
                    "SendSMSTo": {
                        "type": "list",
                        "required": False,
                        "nullable": True,
                        "schema": {"type": "string"},
                    },
                },
            },
            "ServiceAPI": {
                "type": "dict",
                "required": False,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "HostingIP": {"type": "string", "required": True, "nullable": True},
                    "Port": {"type": "number", "required": True, "nullable": True, "min": 80, "max": 65535},
                    "Endpoints": {
                        "type": "list",
                        "required": True,
                        "nullable": False,
                        "schema": {
                            "type": "dict",
                            "schema": {
                                "Name": {"type": "string", "required": True, "nullable": False},
                                "Path": {"type": "string", "required": True, "nullable": False},
                                "Action": {"type": "string", "required": True, "nullable": False, "allowed": ["Motion", "StartSiren", "StopSiren", "ResetSiren", "Ignore"]},
                            },
                        },
                    },
                },
            },
            "Siren": {
                "type": "dict",
                "required": False,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "Switch": {"type": "string", "required": True, "nullable": True},
                    "SirenDuration": {"type": "number", "required": True, "nullable": True, "min": 1, "max": 600},
                    "MinMotionEvents": {"type": "number", "required": True, "nullable": True, "min": 1, "max": 10},
                    "MinMotionSources": {"type": "number", "required": True, "nullable": True, "min": 1, "max": 10},
                    "MinMotionInterval": {"type": "number", "required": True, "nullable": True, "min": 1, "max": 360},
                    "MaxMotionInterval": {"type": "number", "required": True, "nullable": True, "min": 1, "max": 3600},
                    "PostTriggerSleepTimer": {"type": "number", "required": True, "nullable": True, "min": 30, "max": 86400},
                },
            },
        }
