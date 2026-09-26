"""Repository-backed probe for Logcore structured logging integration."""

import json
import logging
import os
import sys

# Ensure Django environment defaults
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("DATABASE_URL", "file:memdb_probe?mode=memory&cache=shared")


def main() -> None:
    """Run logcore probe emitting one ERROR record through the logging system."""
    import django

    django.setup()

    from helpers.logcore import get_transport

    transport = get_transport()
    captured_entries: list[dict] = []

    original_enqueue = transport.enqueue

    def intercept_enqueue(entry: dict) -> None:
        captured_entries.append(entry)
        original_enqueue(entry)

    transport.enqueue = intercept_enqueue  # type: ignore[method-assign]

    logger = logging.getLogger("probe")
    try:
        raise RuntimeError("logcore probe error: structured logging verified")
    except RuntimeError:
        logger.error("logcore probe error: structured logging verified", exc_info=True)

    if captured_entries:
        sys.stdout.write(json.dumps(captured_entries[0]) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
