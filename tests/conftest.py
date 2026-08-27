# eufy-siren pytest initializer.
import subprocess  # noqa: S404
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
TEMPLATE = ROOT / ".env.test.template"
ENV_FILE = ROOT / ".env.test"


def pytest_configure(config):  # noqa: ARG001
    if not TEMPLATE.exists():
        return
    subprocess.run(  # noqa: S603
        ["op", "inject", "-i", str(TEMPLATE), "-o", str(ENV_FILE), "-f"],  # noqa: S607
        check=True,
    )
    load_dotenv(ENV_FILE, override=True)


def pytest_unconfigure(config):  # noqa: ARG001
    if ENV_FILE.exists():
        ENV_FILE.unlink()
