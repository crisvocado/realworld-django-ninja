"""Configuration for Logcore structured logging integration."""

import logging
import os
import re

_VALID_ENVS = {"prod", "staging", "dev", "test", "local"}
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class LogcoreConfig:
    """Dynamic configuration reading environment variables for Logcore."""

    @property
    def url(self) -> str:
        return os.getenv("LOGCORE_URL", "https://logcore-dev-352942961463.us-east4.run.app").rstrip("/")

    @property
    def api_key(self) -> str:
        return os.getenv("LOGCORE_KEY", "")

    @property
    def enabled(self) -> bool:
        enabled_val = os.getenv("LOGCORE_ENABLED")
        if enabled_val is not None:
            return enabled_val.lower() in ("true", "1", "yes", "on")
        return bool(self.api_key)

    @property
    def service(self) -> str:
        service_val = os.getenv("LOGCORE_SERVICE", "realworld-django-ninja").lower()
        if not _SERVICE_NAME_RE.match(service_val):
            return "realworld-django-ninja"
        return service_val

    @property
    def env(self) -> str:
        env_val = os.getenv("LOGCORE_ENV", "dev").lower()
        if env_val == "production":
            return "prod"
        if env_val not in _VALID_ENVS:
            return "dev"
        return env_val

    @property
    def min_severity_level(self) -> int:
        severity_name = os.getenv("LOGCORE_MIN_SEVERITY", "ERROR").upper()
        level = getattr(logging, severity_name, logging.ERROR)
        return int(level) if isinstance(level, int) else logging.ERROR

    def is_active(self) -> bool:
        return self.enabled and bool(self.url) and bool(self.api_key)


def get_config() -> LogcoreConfig:
    return LogcoreConfig()
