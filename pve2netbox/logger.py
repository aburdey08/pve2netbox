"""Logging configuration for pve2netbox."""

import logging
import sys
import os
from typing import Optional


def setup_logger(name: str = 'pve2netbox', level: Optional[str] = None) -> logging.Logger:
    """
    Setup and configure logger with consistent formatting.

    Clears existing handlers to avoid duplicates, adds a single console handler
    to stdout with format ``[%(asctime)s] %(levelname)s: %(message)s``
    (e.g. ``[2025-02-13 10:30:45] INFO: Message``).

    Args:
        name: Logger name.
        level: Log level (DEBUG, INFO, WARNING, ERROR). Defaults to LOG_LEVEL env var or INFO.

    Returns:
        Configured logger instance.
    """
    if level is None:
        level = os.getenv('LOG_LEVEL', 'INFO').upper()

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level, logging.INFO))
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


logger = setup_logger()
"""Global logger instance for the application."""


def log_section(title: str) -> None:
    """Log a section header for better readability."""
    logger.info('=' * 60)
    logger.info(title)
    logger.info('=' * 60)


def log_subsection(title: str) -> None:
    """Log a subsection header."""
    logger.info('-' * 60)
    logger.info(title)
    logger.info('-' * 60)
