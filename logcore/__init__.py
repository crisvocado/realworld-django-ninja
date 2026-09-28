"""Logcore structured logging integration."""

from logcore.client import LogcoreClient, get_client
from logcore.config import LogcoreConfig, get_config
from logcore.handler import LogcoreHandler
from logcore.middleware import LogcoreTraceMiddleware

__all__ = [
    "LogcoreClient",
    "LogcoreConfig",
    "LogcoreHandler",
    "LogcoreTraceMiddleware",
    "get_client",
    "get_config",
]
