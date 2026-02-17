"""
Entry point for ``python -m pve2netbox``.

Supports three modes:
- **Simple mode**: only SYNC_INTERVAL_SECONDS set — full sync in a loop at that interval.
- **Combined mode**: QUICK_CHECK_INTERVAL_SECONDS set — quick checks at that interval,
  full sync at SYNC_INTERVAL_SECONDS (default 3600s); reuses API connections.
- **Single run**: no intervals — one full sync then exit.
"""
import os
import sys
import time
import urllib3
import pynetbox
from proxmoxer import ProxmoxAPI

from pve2netbox import main, quick_check_changes, sync_specific_vms, _make_netbox_session
from pve2netbox.config import load_config
from pve2netbox.logger import logger, log_section, log_subsection
from pve2netbox.metrics import start_metrics_server, metrics
from pve2netbox.api.proxmox import quick_check_changes as quick_check_changes_new

if __name__ == '__main__':
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    config = load_config()

    if config.enable_metrics:
        start_metrics_server(config.metrics_port)
        logger.info(f'Metrics server started on http://0.0.0.0:{config.metrics_port}/metrics')

    quick_check_interval = config.quick_check_interval_seconds
    full_sync_interval = config.sync_interval_seconds

    if full_sync_interval and not quick_check_interval:
        log_section(f'Running in simple mode: full sync every {full_sync_interval}s')
        while True:
            try:
                sync_start = metrics.record_full_sync_start()
                main()
            except Exception as e:
                logger.error(f'Error during sync: {e}', exc_info=True)
                metrics.record_error()
            logger.info(f'Next full sync in {full_sync_interval}s...')
            time.sleep(full_sync_interval)

    elif quick_check_interval:
        full_sync_interval = full_sync_interval if full_sync_interval else 3600
        log_section('Running in combined mode')
        logger.info(f'  - Quick check every {quick_check_interval}s')
        logger.info(f'  - Full sync every {full_sync_interval}s')

        pve_api = ProxmoxAPI(
            host=config.pve_api_host,
            user=config.pve_api_user,
            token_name=config.pve_api_token,
            token_value=config.pve_api_secret,
            verify_ssl=config.pve_api_verify_ssl,
        )
        
        nb_api = pynetbox.api(
            url=config.nb_api_url,
            token=config.nb_api_token,
        )
        nb_api.http_session = _make_netbox_session()
        last_quick_state = {}
        last_full_sync = 0

        log_section('Initial full sync')
        initial_full_sync_ok = False
        try:
            sync_start = metrics.record_full_sync_start()
            main()
            initial_full_sync_ok = True
        except Exception as e:
            logger.error(f'Error during initial sync: {e}', exc_info=True)
            metrics.record_error()
        last_full_sync = time.time() if initial_full_sync_ok else 0
        log_subsection('Initializing quick check state')
        try:
            _, last_quick_state = quick_check_changes_new(pve_api, {}, config)
        except Exception:
            _, last_quick_state = quick_check_changes(pve_api, {})
        logger.info(f'Tracking {len(last_quick_state)} VMs for changes')
        if not initial_full_sync_ok:
            logger.warning(
                'Initial full sync failed; scheduled full sync retry will run on next cycle.'
            )

        while True:
            time.sleep(quick_check_interval)
            current_time = time.time()

            if current_time - last_full_sync >= full_sync_interval:
                log_section('Running scheduled full sync')
                try:
                    sync_start = metrics.record_full_sync_start()
                    main()
                except Exception as e:
                    logger.error(f'Error during full sync: {e}', exc_info=True)
                    metrics.record_error()
                last_full_sync = current_time
                try:
                    _, last_quick_state = quick_check_changes_new(pve_api, {}, config)
                except Exception:
                    _, last_quick_state = quick_check_changes(pve_api, {})
                logger.info(f'Full sync completed. Tracking {len(last_quick_state)} VMs.')
                continue

            log_subsection(f'Quick check ({int(current_time - last_full_sync)}s since last full sync)')
            try:
                try:
                    changed_vmids, last_quick_state = quick_check_changes_new(pve_api, last_quick_state, config)
                except Exception:
                    changed_vmids, last_quick_state = quick_check_changes(pve_api, last_quick_state)
                
                metrics.record_quick_check(len(changed_vmids))
                
                if changed_vmids:
                    logger.info(f'Changes detected in {len(changed_vmids)} VM(s): {changed_vmids}')
                    sync_specific_vms(pve_api, nb_api, changed_vmids)
                else:
                    logger.info('No changes detected.')
            except Exception as e:
                logger.error(f'Error during quick check: {e}', exc_info=True)
                logger.info('Will retry on next check cycle.')
                metrics.record_error()

    else:
        log_section('Running single sync (no intervals configured)')
        try:
            sync_start = metrics.record_full_sync_start()
            main()
        except Exception as e:
            logger.error(f'Error during sync: {e}', exc_info=True)
            metrics.record_error()
            sys.exit(1)
