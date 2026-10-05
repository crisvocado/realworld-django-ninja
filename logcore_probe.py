"""Repository-backed emission proof for the configured logging handler."""

import json
import logging
import os
import urllib.request

import django

os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("DATABASE_URL", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("LOGCORE_ENABLED", "true")
os.environ.setdefault("LOGCORE_URL", "http://logcore.invalid")
os.environ.setdefault("LOGCORE_KEY", "probe-key")

captured: list[bytes] = []


class Capture:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b"{}"


def capture(request, *args, **kwargs):
    captured.append(request.data)
    return Capture()


urllib.request.urlopen = capture  # type: ignore[invalid-assignment]
django.setup()
logging.getLogger("logcore.probe").error("probe error")
for handler in logging.getLogger().handlers:
    if hasattr(handler, "flush"):
        handler.flush()
if not captured:
    raise RuntimeError("logcore probe captured no request")
print(json.dumps(json.loads(captured[0]), sort_keys=True))
