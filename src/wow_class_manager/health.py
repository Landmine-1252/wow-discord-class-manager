"""Local readiness/heartbeat probe; never opens a network listener."""

import json
import math
import os
import sys
import time
from pathlib import Path


def write_health(path: Path, state: str) -> None:
    payload = {"pid": os.getpid(), "state": state, "heartbeat": time.time()}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def check_health(path: Path, max_age: float = 45) -> tuple[int, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        heartbeat = float(data["heartbeat"])
        age = time.time() - heartbeat
        pid = data["pid"]
        if (
            not isinstance(pid, int)
            or pid <= 0
            or not math.isfinite(age)
            or not 0 <= age <= max_age
        ):
            return 1, "unresponsive: stale or invalid heartbeat"
        os.kill(pid, 0)
        state = data["state"]
        if state == "connected":
            return 0, "connected"
        if state == "starting":
            return 1, "starting"
        if state == "waiting_for_guild":
            return (
                1,
                (
                    "waiting_for_guild: install the bot in an allowed server; check"
                    " startup logs"
                ),
            )
        if state == "degraded":
            return (
                1,
                (
                    "degraded: startup checks found a configuration or server problem;"
                    " check logs"
                ),
            )
        return 1, "disconnected"
    except (OSError, ValueError, KeyError, TypeError):
        return 1, "unresponsive: no valid live process heartbeat"


def main() -> None:
    code, message = check_health(
        Path(os.environ.get("HEALTH_PATH", "/tmp/wow-class-manager-health.json"))
    )
    print(message)
    sys.exit(code)


if __name__ == "__main__":
    main()
