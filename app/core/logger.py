"""Central logging configuration for MailPulse.

A single place that gives every module a consistent, timestamped logger so the
Gmail push / sync pipeline can be traced end-to-end (watch setup, webhook
triggered, historyId changes, emails fetched, fallback sync, etc.).
"""

import logging
import os
import sys

_LOG_LEVEL = os.getenv("MAILPULSE_LOG_LEVEL", "INFO").upper()

_root_logger = logging.getLogger("mailpulse")
_root_logger.setLevel(_LOG_LEVEL)

if not _root_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _handler.setFormatter(_formatter)
    _root_logger.addHandler(_handler)
    _root_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the `mailpulse` namespace."""
    return _root_logger.getChild(name)
