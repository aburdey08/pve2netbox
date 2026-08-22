"""
Cooperative shutdown handling.

Long-running modes must react to SIGTERM/SIGINT quickly (``docker stop`` and
``systemctl restart`` send SIGTERM and SIGKILL after a short grace period), but
must never abort in the middle of writing a single object to NetBox. This module
provides a process-wide stop flag: signal handlers set it, sleeping code waits on
it instead of ``time.sleep``, and sync loops check it on safe boundaries
(between nodes, between VMs).
"""

import os
import signal
import threading
from typing import Optional

from .logger import logger

_stop_event = threading.Event()
_signal_received: Optional[int] = None
_installed = False

_SIGNAL_NAMES = {
    signal.SIGTERM: 'SIGTERM',
    signal.SIGINT: 'SIGINT',
}


def _handle_signal(signum: int, _frame) -> None:
    """Set the stop flag; exit immediately if a second signal arrives."""
    global _signal_received  # pylint: disable=global-statement

    name = _SIGNAL_NAMES.get(signum, str(signum))
    if _stop_event.is_set():
        logger.warning(f'Received {name} again, exiting immediately')
        os._exit(130)  # pylint: disable=protected-access

    _signal_received = signum
    _stop_event.set()
    logger.info(f'Received {name}, finishing current step and shutting down...')


def install_handlers() -> None:
    """Install SIGTERM/SIGINT handlers. Safe to call more than once."""
    global _installed  # pylint: disable=global-statement

    if _installed:
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except ValueError:
            # Not running in the main thread — nothing to install.
            return
    _installed = True


def should_stop() -> bool:
    """True once a shutdown signal has been received."""
    return _stop_event.is_set()


def wait(seconds: float) -> bool:
    """
    Sleep up to ``seconds``, waking early on shutdown.

    Returns True if a shutdown was requested (caller should stop), False if the
    full interval elapsed.
    """
    return _stop_event.wait(seconds)


def signal_name() -> Optional[str]:
    """Name of the signal that triggered shutdown, or None."""
    if _signal_received is None:
        return None
    return _SIGNAL_NAMES.get(_signal_received, str(_signal_received))


def reset() -> None:
    """Clear the stop flag. Intended for tests."""
    global _signal_received  # pylint: disable=global-statement

    _stop_event.clear()
    _signal_received = None
