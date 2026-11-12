"""The host's paths and tunables, read once from its environment."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# The tracked Stop-hook registration the park must end within.
REGISTRATION_SECONDS = 28800
DEFAULT_PARK_SECONDS = 27000

_POSITIVE = re.compile(r"[1-9][0-9]*")


def numeric_or(value: str | None, default: int) -> int:
    """A positive integer without a leading zero, or `default`."""
    return int(value) if value and _POSITIVE.fullmatch(value) else default


@dataclass(frozen=True)
class Settings:
    engine_grace: int
    park_seconds: int
    park_limit: int
    turn_timeout: int
    rotate_turns: int
    ready_timeout: int
    poll: int
    warm_compact_tick: int


def settings_from(env: Mapping[str, str]) -> Settings:
    park_seconds = numeric_or(env.get("FM_SUPERVISION_HOST_PARK_SECONDS"), DEFAULT_PARK_SECONDS)
    if park_seconds >= REGISTRATION_SECONDS:
        park_seconds = DEFAULT_PARK_SECONDS
    park_limit = numeric_or(env.get("FM_SUPERVISION_HOST_PARK_LIMIT"), park_seconds)
    if not park_seconds <= park_limit < REGISTRATION_SECONDS:
        park_limit = park_seconds
    return Settings(
        engine_grace=numeric_or(env.get("FM_SUPERVISION_ENGINE_GRACE"), 30),
        park_seconds=park_seconds,
        park_limit=park_limit,
        turn_timeout=numeric_or(env.get("FM_SUPERVISION_HOST_TURN_TIMEOUT"), 1200),
        rotate_turns=numeric_or(env.get("FM_SUPERVISION_HOST_ROTATE_TURNS"), 20),
        ready_timeout=numeric_or(env.get("FM_SUPERVISION_HOST_READY_TIMEOUT"), 25),
        poll=numeric_or(env.get("FM_SUPERVISION_HOST_POLL"), 1),
        warm_compact_tick=numeric_or(env.get("FM_WARM_COMPACT_TICK"), 60),
    )


@dataclass(frozen=True)
class Paths:
    script_dir: str
    root: str
    home: str
    state: str
    config: str

    def at(self, name: str) -> str:
        return os.path.join(self.state, name)

    def script(self, name: str) -> str:
        return os.path.join(self.script_dir, name)

    def configured(self, name: str) -> bool:
        """True when config/<name> exists, the way a home opts in."""
        return os.path.exists(os.path.join(self.config, name))

    @property
    def host_record(self) -> str:
        return self.at(".supervision-host")

    @property
    def engine_record(self) -> str:
        return self.at(".supervision-host-engine")

    @property
    def turn_file(self) -> str:
        return self.at(".supervision-host-turn")

    @property
    def receipts(self) -> str:
        return self.at(".supervision-host-receipts")

    @property
    def prompt_file(self) -> str:
        return self.at(".supervision-host-prompt")

    @property
    def wake_file(self) -> str:
        return self.at(".supervision-host-wake")

    @property
    def host_log(self) -> str:
        return self.at(".supervision-host.log")

    @property
    def engine_pid_file(self) -> str:
        return self.at(".supervision-host.engine-pid")

    @property
    def health_file(self) -> str:
        return self.at(".supervision-host-health")

    @property
    def mirror_feed(self) -> str:
        return self.at(".supervision-host-mirror")

    @property
    def left_record(self) -> str:
        return self.at(".supervision-host-left")

    @property
    def handoff_owner(self) -> str:
        return self.at(".supervision-handoff-owner")

    @property
    def handoff_pending(self) -> str:
        return self.at(".supervision-handoff-pending")

    @property
    def marker(self) -> str:
        return self.at(".watcher-down")

    @property
    def session_lock(self) -> str:
        return self.at(".lock")

    @property
    def away_daemon_flag(self) -> str:
        return self.at(".afk")


def paths_from(env: Mapping[str, str], script_dir: str) -> Paths:
    root = env.get("FM_ROOT_OVERRIDE") or os.path.dirname(script_dir)
    home = env.get("FM_HOME") or env.get("FM_ROOT_OVERRIDE") or root
    return Paths(
        script_dir=script_dir,
        root=root,
        home=home,
        state=env.get("FM_STATE_OVERRIDE") or os.path.join(home, "state"),
        config=env.get("FM_CONFIG_OVERRIDE") or os.path.join(home, "config"),
    )
