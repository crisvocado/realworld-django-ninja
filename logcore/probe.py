"""Safe repository-backed probe entry point to emit an error record."""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any


def _bootstrap_env() -> None:
    workspace_root = Path(__file__).resolve().parent.parent
    if str(workspace_root) not in sys.path:
        sys.path.insert(0, str(workspace_root))
    apps_dir = workspace_root / "apps"
    if apps_dir.is_dir() and str(apps_dir) not in sys.path:
        sys.path.insert(0, str(apps_dir))

    for sp in (workspace_root / ".venv" / "lib").glob("python*/site-packages"):
        sp_str = str(sp)
        if sp_str not in sys.path:
            sys.path.insert(0, sp_str)


def main() -> None:
    _bootstrap_env()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DEBUG", "True")
    os.environ.setdefault("DATABASE_URL", "file:memdb1?mode=memory&cache=shared")

    try:
        import django

        django.setup()
    except Exception as e:
        sys.stderr.write(f"[logcore-probe] Failed to setup Django: {e}\n")
        sys.exit(1)

    captured_entries: list[dict[str, Any]] = []
    try:
        from logcore.client import get_client

        client = get_client()
        client._custom_sender = lambda batch: captured_entries.extend(batch)
    except Exception:
        pass

    from logcore.trace import generate_trace_context, set_trace_context

    _ = set_trace_context(generate_trace_context())

    logger = logging.getLogger("django")
    try:
        raise RuntimeError("Logcore probe test error")
    except Exception:
        logger.error("Logcore test error", exc_info=True)

    # Flush all handlers
    for handler in logging.getLogger().handlers + logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()

    try:
        from logcore.client import get_client

        get_client().flush()
    except Exception:
        pass

    for entry in captured_entries:
        sys.stdout.write(json.dumps(entry) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
