"""Centralized logging setup for ONASSIS.

Call :func:`setup_logging` once at startup (the entry point does this),
then everywhere else just use::

    from onassis.logger import get_logger
    log = get_logger(__name__)

Logs go to both the console and a rotating file so a long-running
scheduler doesn't fill the disk.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from onassis.config import Config

_CONFIGURED = False
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(config: Config) -> None:
    """Configure the root logger from :class:`Config`.

    Idempotent: calling it more than once is a no-op so we never attach
    duplicate handlers.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_path: Path = config.log_dir / config.log_file

    level = getattr(logging, config.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _CONFIGURED = True
    get_logger(__name__).debug("Logging initialized at level %s -> %s", config.log_level, log_path)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
