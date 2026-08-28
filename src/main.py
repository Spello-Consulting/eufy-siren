"""Main module for the Eufy Security Siren Integration app."""

import argparse
import os
import platform
import signal
import sys
from pathlib import Path
from threading import Event

from dotenv import load_dotenv
from mergedeep import merge
from sc_foundation import (
    RestartPolicy,
    SCCommon,
    SCConfigManager,
    SCLogger,
    ThreadManager,
)
from sc_smart_device import SCSmartDevice, SmartDeviceWorker, smart_devices_validator

from config_schemas import ConfigSchema
from event_inbox import ServiceEventInbox
from local_enumerations import CONFIG_FILE
from service_api import serve_api_blocking
from siren_controller import SirenController


def parse_command_line_args() -> dict[str, str | None]:
    """Parse and validate command line arguments.

    Returns:
        dict: Dictionary containing parsed arguments with keys:
            - 'config_file': Path to configuration file (always present)
            - 'homedir': Project home directory (may be None)
    """
    parser = argparse.ArgumentParser(
        description="eufy-siren - Eufy Security Siren Integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py
  python main.py --config /path/to/config.yaml
  python main.py --homedir /opt/eufy-siren --config config.yaml
        """
    )

    parser.add_argument(
        "--homedir",
        type=str,
        metavar="PATH",
        help="Specify the project home directory",
    )

    parser.add_argument(
        "--config",
        type=str,
        metavar="FILE",
        help=f"Path to configuration file (default: {CONFIG_FILE})",
    )

    args = parser.parse_args()

    if args.homedir:
        homedir = Path(args.homedir)
        if not homedir.exists():
            print(f"ERROR: Specified homedir does not exist: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        if not homedir.is_dir():
            print(f"ERROR: Specified homedir is not a directory: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        base_dir = homedir.resolve()
        os.environ["SC_FOUNDATION_PROJECT_ROOT"] = str(base_dir)
    else:
        base_dir = Path(SCCommon.get_project_root())

    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = base_dir / config_path
        config_file = str(config_path.resolve())
        if not Path(config_file).exists():
            print(f"ERROR: Configuration file does not exist: {config_file}", file=sys.stderr)
            sys.exit(1)
        if not Path(config_file).is_file():
            print(f"ERROR: Configuration path is not a file: {config_file}", file=sys.stderr)
            sys.exit(1)
    else:
        config_file = CONFIG_FILE

    return {
        "config_file": config_file,
        "homedir": str(base_dir) if args.homedir else None,
    }


def report_fatal_heartbeat(logger: SCLogger) -> None:
    """Report a fatal error to the heartbeat monitor and log it.

    Args:
        logger: The logger instance used to report the heartbeat failure.
    """
    logger.ping_heartbeat(is_fail=True)


def initialize_config_and_logging(
    cmd_args: dict[str, str | None],
) -> tuple[SCConfigManager, SCLogger]:
    """Initialize the configuration manager.

    If either fails, the program will exit with an error message.

    Args:
        cmd_args: Command line arguments containing the config file path.

    Returns:
        tuple[SCConfigManager, SCLogger]: Initialized configuration manager and logger instances.
    """
    schemas = ConfigSchema()

    # Merge the local schema (General/ServiceAPI/Siren) with the foundation schema
    # (Files/Email/HeartbeatMonitor) and the smart-device schema (SCSmartDevices) so the
    # whole config file validates in one pass. Merge into a fresh dict so no source is mutated.
    merged_schema = merge({}, schemas.validation, smart_devices_validator)
    assert isinstance(merged_schema, dict), "Merged schema should be type dict"

    try:
        config_file = cmd_args["config_file"]
        assert isinstance(config_file, str)
        config = SCConfigManager(
            config_file=config_file,
            validation_schema=merged_schema
        )
    except RuntimeError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        heartbeat_config = config.get("HeartbeatMonitor")
        if not isinstance(heartbeat_config, dict) or not heartbeat_config.get("Enable", False):
            heartbeat_config = None
        logger = SCLogger(config.get_logger_settings(), heartbeat_config=heartbeat_config)
    except RuntimeError as e:
        print(f"Logger initialisation error: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        logger.log_message("", "summary")
        logger.log_message("", "summary")
        logger.log_message("Eufy Security Siren Integration application starting.", "summary")
        logger.log_message(f"Configuration file: {cmd_args['config_file']}", "debug")
        if cmd_args["homedir"]:
            logger.log_message(f"Home directory: {cmd_args['homedir']}", "debug")

        email_settings = config.get_email_settings()
        if email_settings is not None:
            logger.register_email_settings(email_settings)

        if logger.get_fatal_error():
            logger.log_message("Application has recovered after a prior fatal failure.", "summary")
            logger.clear_fatal_error()
            logger.send_email("eufy-siren recovery", "eufy-siren run was successful after a prior failure.")

    return config, logger


def main() -> None:
    """Main entry point."""
    load_dotenv()  # Load .env file if present (no-op if absent)
    print(f"Starting eufy-siren on {platform.system()}")

    wake_event = Event()   # Wakes the controller loop from sleep (e.g. on webhook)
    stop_event = Event()   # Signals all threads to stop

    cmd_args = parse_command_line_args()

    # Install SIGINT handler early
    def handle_sigint(_sig: int, _frame: object) -> None:
        stop_event.set()
        wake_event.set()
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    config, logger = initialize_config_and_logging(cmd_args)

    # Initialise the smart-device control stack.
    smart_device_settings = config.get("SCSmartDevices")
    if smart_device_settings is None:
        logger.log_fatal_error("No SCSmartDevices settings found in the configuration file.")
        return

    try:
        smart_device = SCSmartDevice(logger, smart_device_settings, wake_event)
    except RuntimeError as e:
        logger.log_fatal_error(f"SCSmartDevice initialisation error: {e}")
        return
    logger.log_message(
        f"SCSmartDevice initialised with {len(smart_device.devices)} device(s).", "summary"
    )

    # Build the worker objects: the smart-device worker, the event inbox shared with the
    # ServiceAPI, and the controller that ties motion events to siren actions.
    inbox = ServiceEventInbox(wake_event)
    try:
        smart_device_worker = SmartDeviceWorker(smart_device, logger, wake_event)
        controller = SirenController(config, logger, smart_device_worker, inbox, wake_event)
    except (RuntimeError, TypeError) as e:
        logger.log_fatal_error(f"Fatal error at startup: {e}")
        return

    # Wire up and start the managed threads.
    thread_manager = ThreadManager(
        logger,
        global_stop=stop_event,
        before_exit=lambda: report_fatal_heartbeat(logger),
    )
    thread_manager.add(
        name="smart device",
        target=smart_device_worker.run,
        restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=2.0),
        stop_event=stop_event,
    )
    thread_manager.add(
        name="controller",
        target=controller.run,
        kwargs={"stop_event": stop_event},
        restart=RestartPolicy(mode="never"),
    )
    thread_manager.add(
        name="service api",
        target=serve_api_blocking,
        args=(inbox, config, logger, stop_event),
        restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=2.0),
    )

    thread_manager.start_all()
    logger.log_message("eufy-siren application started.", "summary")
    try:
        while not stop_event.is_set():
            if thread_manager.any_crashed():
                logger.log_fatal_error(
                    "A managed thread crashed. Initiating shutdown.", report_stack=False
                )
                stop_event.set()
                wake_event.set()
                break
            stop_event.wait(timeout=1.0)
    finally:
        thread_manager.stop_all()
        thread_manager.join_all(timeout_per_thread=10.0)
        logger.log_message("eufy-siren application stopped.", "summary")


if __name__ == "__main__":
    main()
