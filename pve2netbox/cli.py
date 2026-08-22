"""
Command-line entry point for pve2netbox.

Holds the mode dispatcher shared by the ``pve2netbox`` console script and
``python -m pve2netbox``:

- **Single run** — no intervals configured: one full sync, then exit.
- **Simple mode** — ``SYNC_INTERVAL_SECONDS`` only: full sync in a loop.
- **Combined mode** — ``QUICK_CHECK_INTERVAL_SECONDS`` set: quick change checks
  at that interval, full sync every ``SYNC_INTERVAL_SECONDS`` (default 3600).

Both entry points go through :func:`run`, so every mode behaves the same however
the program was started.
"""

import argparse
import os
import sys
import time
from typing import List, Optional

import pynetbox
import urllib3

from . import main as run_sync
from . import shutdown, sync_specific_vms
from .api.netbox import make_netbox_session, resolve_cluster
from .api.proxmox import create_proxmox_api, quick_check_changes
from .config import Config, describe_config, load_config, load_env_file, set_config
from .logger import log_section, log_subsection, logger, set_log_level
from .metrics import default_readiness, metrics, start_http_server
from .version import get_version

DEFAULT_FULL_SYNC_INTERVAL_SECONDS = 3600.0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog='pve2netbox',
        description='Synchronize Proxmox VE inventory to NetBox.',
        epilog='Configuration is read from environment variables; see README.md.',
    )
    parser.add_argument(
        '--env-file',
        metavar='PATH',
        help='Load environment variables from PATH. Defaults to $PVE2NETBOX_ENV_FILE, '
             'then ./.env if it exists. Variables already set in the environment win.',
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'pve2netbox {get_version()}',
    )
    return parser


def _resolve_env_file(explicit: Optional[str]) -> Optional[str]:
    """
    Pick the env file to load: explicit argument, then ``PVE2NETBOX_ENV_FILE``,
    then ``./.env`` if present. A path given explicitly must exist.
    """
    for path in (explicit, os.getenv('PVE2NETBOX_ENV_FILE')):
        if not path:
            continue
        # An explicitly requested file must exist: silently falling back to the
        # environment would start the process with the wrong configuration.
        if not os.path.isfile(path):
            print(f'Env file not found: {path}', file=sys.stderr)
            sys.exit(1)
        return path

    default_path = os.path.join(os.getcwd(), '.env')
    return default_path if os.path.isfile(default_path) else None


def _log_startup(config: Config) -> None:
    """Log the effective configuration (no secrets) and any safety warnings."""
    logger.info(f'pve2netbox {get_version()}')
    for label, value in describe_config(config):
        logger.info(f'  {label}: {value}')

    if not config.pve_api_verify_ssl:
        logger.warning(
            'PVE_API_VERIFY_SSL is disabled: the Proxmox TLS certificate is not verified. '
            'This is the current default and will change to enabled in 2.0.0 — '
            'set PVE_API_VERIFY_SSL explicitly to keep today\'s behaviour.'
        )


def _readiness_max_age(config: Config) -> Optional[float]:
    """
    How stale the last successful sync may get before ``/readyz`` fails.

    Two sync intervals, so a single failed cycle does not flip the container to
    unhealthy. Single-run mode has no interval and only requires one success.
    """
    if config.quick_check_interval_seconds:
        interval = config.sync_interval_seconds or DEFAULT_FULL_SYNC_INTERVAL_SECONDS
        return interval * 2
    if config.sync_interval_seconds:
        return config.sync_interval_seconds * 2
    return None


def _start_http_server(config: Config) -> None:
    """Start the metrics/health HTTP server when either endpoint is enabled."""
    if not config.enable_metrics and not config.enable_health_endpoint:
        return

    try:
        start_http_server(
            port=config.metrics_port,
            enable_metrics=config.enable_metrics,
            enable_health=config.enable_health_endpoint,
            readiness=default_readiness(_readiness_max_age(config)),
        )
    except OSError as e:
        logger.error(
            f'Cannot listen on port {config.metrics_port} for metrics/health endpoints: {e}. '
            f'Set METRICS_PORT to a free port, or disable both ENABLE_METRICS and '
            f'ENABLE_HEALTH_ENDPOINT.'
        )
        sys.exit(1)
    endpoints: List[str] = []
    if config.enable_metrics:
        endpoints.append('/metrics')
    if config.enable_health_endpoint:
        endpoints.extend(('/healthz', '/readyz'))
    logger.info(
        f'HTTP server listening on http://0.0.0.0:{config.metrics_port} '
        f'({", ".join(endpoints)})'
    )


def _run_full_sync(config: Config, pve_api, nb_api, label: str) -> bool:
    """Run one full sync, converting failures into a logged error. True on success."""
    try:
        run_sync(config, pve_api, nb_api)
        return True
    except Exception as e:  # pylint: disable=broad-except
        logger.error(f'Error during {label}: {e}', exc_info=True)
        metrics.record_error()
        return False


def _run_single(config: Config, pve_api, nb_api) -> int:
    """Single-run mode: one full sync, then exit."""
    log_section('Running single sync (no intervals configured)')
    return 0 if _run_full_sync(config, pve_api, nb_api, 'sync') else 1


def _run_simple(config: Config, pve_api, nb_api) -> int:
    """Simple mode: full sync on a fixed interval."""
    interval = config.sync_interval_seconds
    log_section(f'Running in simple mode: full sync every {interval}s')

    while True:
        _run_full_sync(config, pve_api, nb_api, 'sync')
        if shutdown.should_stop():
            break
        logger.info(f'Next full sync in {interval}s...')
        if shutdown.wait(interval):
            break

    return _shutdown_exit_code()


def _run_combined(config: Config, pve_api, nb_api) -> int:
    """Combined mode: frequent quick checks plus a periodic full sync."""
    quick_check_interval = config.quick_check_interval_seconds
    full_sync_interval = config.sync_interval_seconds or DEFAULT_FULL_SYNC_INTERVAL_SECONDS

    log_section('Running in combined mode')
    logger.info(f'  - Quick check every {quick_check_interval}s')
    logger.info(f'  - Full sync every {full_sync_interval}s')

    log_section('Initial full sync')
    last_full_sync = time.time() if _run_full_sync(
        config, pve_api, nb_api, 'initial sync') else 0.0
    if not last_full_sync:
        logger.warning(
            'Initial full sync failed; scheduled full sync retry will run on next cycle.'
        )

    last_quick_state: dict = {}
    if not shutdown.should_stop():
        log_subsection('Initializing quick check state')
        try:
            _, last_quick_state = quick_check_changes(pve_api, {}, config)
            logger.info(f'Tracking {len(last_quick_state)} VMs for changes')
        except Exception as e:  # pylint: disable=broad-except
            logger.error(f'Failed to initialize quick check state: {e}', exc_info=True)
            metrics.record_error()

    while not shutdown.wait(quick_check_interval):
        current_time = time.time()

        if current_time - last_full_sync >= full_sync_interval:
            log_section('Running scheduled full sync')
            _run_full_sync(config, pve_api, nb_api, 'full sync')
            last_full_sync = current_time
            if shutdown.should_stop():
                break
            try:
                _, last_quick_state = quick_check_changes(pve_api, {}, config)
                logger.info(f'Full sync completed. Tracking {len(last_quick_state)} VMs.')
            except Exception as e:  # pylint: disable=broad-except
                logger.error(f'Failed to refresh quick check state: {e}', exc_info=True)
                metrics.record_error()
            continue

        log_subsection(
            f'Quick check ({int(current_time - last_full_sync)}s since last full sync)'
        )
        try:
            changed_vmids, last_quick_state = quick_check_changes(
                pve_api, last_quick_state, config)
            metrics.record_quick_check(len(changed_vmids))

            if changed_vmids:
                logger.info(f'Changes detected in {len(changed_vmids)} VM(s): {changed_vmids}')
                sync_specific_vms(pve_api, nb_api, changed_vmids)
            else:
                logger.info('No changes detected.')
        except Exception as e:  # pylint: disable=broad-except
            logger.error(f'Error during quick check: {e}', exc_info=True)
            logger.info('Will retry on next check cycle.')
            metrics.record_error()

    return _shutdown_exit_code()


def _resolve_cluster_with_retry(nb_api, config: Config, retry_interval: Optional[float]) -> bool:
    """
    Resolve and validate the NetBox cluster, retrying while NetBox is unreachable.

    A NetBox restart must not take the daemon down, so in the looping modes this
    keeps retrying until it succeeds or a shutdown is requested. Single-run mode
    (``retry_interval`` is None) fails fast instead.

    Configuration mistakes — a cluster ID that does not exist — still exit
    immediately from :func:`resolve_cluster`; only transport failures are retried.
    """
    while True:
        try:
            resolve_cluster(nb_api, config)
            return True
        except Exception as e:  # pylint: disable=broad-except
            metrics.record_error()
            if retry_interval is None:
                logger.error(f'Cannot reach NetBox: {e}', exc_info=True)
                return False
            logger.error(f'Cannot reach NetBox: {e}. Retrying in {retry_interval}s...')
            if shutdown.wait(retry_interval):
                return False


def _shutdown_exit_code() -> int:
    """Log the reason for leaving a loop and return the process exit code."""
    name = shutdown.signal_name()
    if name:
        log_section(f'Stopped on {name}')
    return 0


def run(argv: Optional[List[str]] = None) -> int:
    """
    Program entry point: configure the process, then dispatch to a sync mode.

    Returns the process exit code.
    """
    args = build_parser().parse_args(argv)

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    env_file = _resolve_env_file(args.env_file)
    loaded_vars = load_env_file(env_file) if env_file else 0

    # The logger is configured at import time, before the env file was read.
    set_log_level()
    if env_file:
        logger.info(f'Loaded {loaded_vars} variable(s) from {env_file}')

    config = load_config()
    set_config(config)
    shutdown.install_handlers()
    _log_startup(config)
    _start_http_server(config)

    pve_api = create_proxmox_api(config)
    nb_api = pynetbox.api(url=config.nb_api_url, token=config.nb_api_token)
    nb_api.http_session = make_netbox_session(config)

    retry_interval = (config.quick_check_interval_seconds
                      or config.sync_interval_seconds)
    if not _resolve_cluster_with_retry(nb_api, config, retry_interval):
        return _shutdown_exit_code() if shutdown.should_stop() else 1

    if config.quick_check_interval_seconds:
        return _run_combined(config, pve_api, nb_api)
    if config.sync_interval_seconds:
        return _run_simple(config, pve_api, nb_api)
    return _run_single(config, pve_api, nb_api)

