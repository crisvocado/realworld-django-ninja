from __future__ import annotations

import json
import logging
import os
import sys

import django


def main() -> None:
    os.environ.setdefault("DEBUG", "True")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    captured_envelopes: list[dict[str, object]] = []

    from logcore.transport import get_transport

    transport = get_transport()
    transport.set_capture_sink(lambda envelope: captured_envelopes.append(envelope))

    logger = logging.getLogger("django")
    try:
        raise RuntimeError("Logcore probe test error")
    except RuntimeError:
        logger.error("Logcore emission probe verification event", exc_info=True)

    transport.flush()

    if not captured_envelopes:
        sys.stderr.write("Error: probe did not capture any emitted envelope\n")
        sys.exit(1)

    # Print captured HTTP envelope to stdout
    print(json.dumps(captured_envelopes[0]))
    sys.exit(0)


if __name__ == "__main__":
    main()
