"""Runtime settings, read from the environment.

TASK_ORCHARD_DB   Path to the SQLite file.
                  Default: $XDG_DATA_HOME/task-orchard/orchard.db
TASK_ORCHARD_TZ   IANA timezone name for the user, e.g. "Europe/London".
                  Default: $TZ, then the system zone, then UTC.
TASK_ORCHARD_MODEL_BASIC      The app's model for everyday conversation.
TASK_ORCHARD_MODEL_STRATEGIC  The app's model for work that needs more thought.
                  Both default to claude-haiku-5-5 (SPEC 10.2, two tiers).

The timezone matters: date-only deadlines, "today", and recurrence schedules
are all interpreted in it (SPEC section 3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENV_DB = "TASK_ORCHARD_DB"
ENV_TZ = "TASK_ORCHARD_TZ"
ENV_MODEL_BASIC = "TASK_ORCHARD_MODEL_BASIC"
ENV_MODEL_STRATEGIC = "TASK_ORCHARD_MODEL_STRATEGIC"
DEFAULT_MODEL = "claude-haiku-5-5"


# Told to the model, by the app's prompt and the MCP server's instructions alike,
# when no zone could be detected.
TIMEZONE_WARNING = (
    "Note: the user's timezone could not be detected, so UTC is being assumed. "
    'Tell the user once that they should set TASK_ORCHARD_TZ (for example "Europe/London") '
    "in the environment Task Orchard runs in."
)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    timezone: ZoneInfo
    # False when no zone could be detected and UTC was assumed. Surfaced by the
    # server's instructions so the agent can tell the user to set TASK_ORCHARD_TZ.
    timezone_detected: bool = True
    model_basic: str = DEFAULT_MODEL
    model_strategic: str = DEFAULT_MODEL


def default_db_path(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "task-orchard" / "orchard.db"


def _system_zone_name() -> str | None:
    """The system's IANA zone name from the /etc/localtime symlink, if any."""
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker not in target:
        return None
    return target.split(marker, 1)[1]


def detect_timezone(env: Mapping[str, str]) -> tuple[ZoneInfo, bool]:
    explicit = env.get(ENV_TZ)
    if explicit:
        # An explicit setting that is wrong should fail loudly, not fall back.
        return ZoneInfo(explicit), True
    for name in (env.get("TZ"), _system_zone_name()):
        if not name:
            continue
        try:
            return ZoneInfo(name.lstrip(":")), True
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC"), False


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    db = Path(env[ENV_DB]).expanduser() if env.get(ENV_DB) else default_db_path(env)
    tz, detected = detect_timezone(env)
    return Settings(
        db_path=db,
        timezone=tz,
        timezone_detected=detected,
        model_basic=env.get(ENV_MODEL_BASIC) or DEFAULT_MODEL,
        model_strategic=env.get(ENV_MODEL_STRATEGIC) or DEFAULT_MODEL,
    )
