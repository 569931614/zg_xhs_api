from __future__ import annotations

import logging
import os
import sys


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    root_logger.setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
    logging.getLogger("app").setLevel(level)
