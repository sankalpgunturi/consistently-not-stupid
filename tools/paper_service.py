"""Install and inspect the macOS paper runner. Run with the project's Python."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.consistently-not-stupid.paper"
DOMAIN = f"gui/{os.getuid()}"
TARGET = f"{DOMAIN}/{LABEL}"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "status", "restart", "stop"])
    args = parser.parse_args()
    if args.command == "install":
        python = ROOT / ".venv" / "bin" / "python"
        if not python.is_file():
            raise SystemExit("Install the project into .venv first.")
        logs = ROOT / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "Label": LABEL,
            "ProgramArguments": ["/usr/bin/caffeinate", "-i", str(python), "-m", "cst", "serve"],
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "StandardOutPath": str(logs / "runner.log"),
            "StandardErrorPath": str(logs / "runner.log"),
        }
        if subprocess.run(["launchctl", "print", TARGET], capture_output=True).returncode == 0:
            raise SystemExit("Already installed and loaded. Use restart to load code changes.")
        PLIST.write_bytes(plistlib.dumps(config))
        subprocess.run(["launchctl", "bootstrap", DOMAIN, str(PLIST)], check=True)
        print("Paper runner installed. Dashboard: http://127.0.0.1:8000")
    elif args.command == "status":
        subprocess.run(["launchctl", "print", TARGET], check=True)
    elif args.command == "restart":
        subprocess.run(["launchctl", "kickstart", "-k", TARGET], check=True)
    else:
        subprocess.run(["launchctl", "bootout", TARGET], check=True)
        print("Paper runner stopped. Use install to start it again.")


if __name__ == "__main__":
    main()
