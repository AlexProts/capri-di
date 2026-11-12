from dataclasses import dataclass
import os
from pathlib import Path


def flag(name):
    value = os.environ.get(name, "false").lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass
class Config:
    state: Path
    owner: int
    telegram_token: str
    openrouter_key: str
    model: str
    schedules: bool
    proactive_tests: bool
    time_enabled: bool
    digest_hours: tuple
    time_interval: int
    test_hour: int
    bot_username: str = ""

    @classmethod
    def load(cls):
        hours = tuple(int(v) for v in os.environ.get("DIGEST_HOURS", "9,20").split(","))
        interval = int(os.environ.get("TIME_INTERVAL_HOURS", "4"))
        test_hour = int(os.environ.get("TEST_SCAN_HOUR", "10"))
        if not hours or any(h not in range(24) for h in hours) or interval not in range(1, 25) or test_hour not in range(24):
            raise ValueError("Invalid schedule")
        return cls(Path(os.environ.get("STATE_DIR", "state/shared")).resolve(),
                   int(os.environ.get("TELEGRAM_OWNER_ID", "0")),
                   os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                   os.environ.get("OPENROUTER_API_KEY", ""),
                   os.environ.get("OPENROUTER_MODEL", ""),
                   flag("SCHEDULES_ENABLED"), flag("PROACTIVE_TESTS_ENABLED"),
                   flag("TIME_ENABLED"), hours, interval, test_hour,
                   os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@"))
