"""Configuration management and validation for pve2netbox."""

import os
import sys
from typing import Optional
from dataclasses import dataclass


@dataclass
class Config:
    """
    Configuration for pve2netbox.

    Proxmox: pve_api_host, pve_api_user, pve_api_token, pve_api_secret, pve_api_verify_ssl.
    NetBox: nb_api_url, nb_api_token, nb_cluster_id, nb_api_delay_seconds,
    nb_api_retry_total, nb_api_retry_backoff.
    Sync: sync_vms, sync_lxc, sync_interval_seconds, quick_check_interval_seconds.
    Roles: vm_role, lxc_role (optional device role names).
    Feature flags: dry_run, enable_cleanup, enable_metrics, metrics_port.
    """
    pve_api_host: str
    pve_api_user: str
    pve_api_token: str
    pve_api_secret: str
    pve_api_verify_ssl: bool
    nb_api_url: str
    nb_api_token: str
    nb_cluster_id: int
    nb_api_delay_seconds: float
    nb_api_retry_total: int
    nb_api_retry_backoff: float
    sync_vms: bool
    sync_lxc: bool
    sync_interval_seconds: Optional[float]
    quick_check_interval_seconds: Optional[float]
    vm_role: Optional[str]
    lxc_role: Optional[str]
    dry_run: bool
    enable_cleanup: bool
    enable_metrics: bool
    metrics_port: int


def load_config() -> Config:
    """
    Load and validate configuration from environment variables.

    Required: PVE_API_HOST, PVE_API_USER, PVE_API_TOKEN, PVE_API_SECRET,
    NB_API_URL, NB_API_TOKEN. Optional variables use defaults; invalid values
    cause exit(1) with messages on stderr.
    """
    errors = []

    pve_api_host = os.getenv('PVE_API_HOST')
    if not pve_api_host:
        errors.append('PVE_API_HOST is required')
    
    pve_api_user = os.getenv('PVE_API_USER')
    if not pve_api_user:
        errors.append('PVE_API_USER is required')
    
    pve_api_token = os.getenv('PVE_API_TOKEN')
    if not pve_api_token:
        errors.append('PVE_API_TOKEN is required')
    
    pve_api_secret = os.getenv('PVE_API_SECRET')
    if not pve_api_secret:
        errors.append('PVE_API_SECRET is required')

    nb_api_url = os.getenv('NB_API_URL')
    if not nb_api_url:
        errors.append('NB_API_URL is required')
    
    nb_api_token = os.getenv('NB_API_TOKEN')
    if not nb_api_token:
        errors.append('NB_API_TOKEN is required')
    
    if errors:
        print('Configuration errors:', file=sys.stderr)
        for error in errors:
            print(f'  - {error}', file=sys.stderr)
        sys.exit(1)

    try:
        config = Config(
            pve_api_host=pve_api_host,  # type: ignore
            pve_api_user=pve_api_user,  # type: ignore
            pve_api_token=pve_api_token,  # type: ignore
            pve_api_secret=pve_api_secret,  # type: ignore
            pve_api_verify_ssl=os.getenv('PVE_API_VERIFY_SSL', 'false').lower() == 'true',
            nb_api_url=nb_api_url,  # type: ignore
            nb_api_token=nb_api_token,  # type: ignore
            nb_cluster_id=int(os.getenv('NB_CLUSTER_ID', '1')),
            nb_api_delay_seconds=float(os.getenv('NB_API_DELAY_SECONDS', '0.2')),
            nb_api_retry_total=int(os.getenv('NB_API_RETRY_TOTAL', '5')),
            nb_api_retry_backoff=float(os.getenv('NB_API_RETRY_BACKOFF', '1.0')),
            sync_vms=os.getenv('SYNC_VMS', 'true').lower() == 'true',
            sync_lxc=os.getenv('SYNC_LXC', 'true').lower() == 'true',
            sync_interval_seconds=float(os.getenv('SYNC_INTERVAL_SECONDS')) 
                if os.getenv('SYNC_INTERVAL_SECONDS') else None,
            quick_check_interval_seconds=float(os.getenv('QUICK_CHECK_INTERVAL_SECONDS'))
                if os.getenv('QUICK_CHECK_INTERVAL_SECONDS') else None,
            vm_role=os.getenv('VM_ROLE'),
            lxc_role=os.getenv('LXC_ROLE'),
            dry_run=os.getenv('DRY_RUN', 'false').lower() == 'true',
            enable_cleanup=os.getenv('ENABLE_CLEANUP', 'false').lower() == 'true',
            enable_metrics=os.getenv('ENABLE_METRICS', 'false').lower() == 'true',
            metrics_port=int(os.getenv('METRICS_PORT', '9090')),
        )
    except (ValueError, TypeError) as e:
        print(f'Configuration parsing error: {e}', file=sys.stderr)
        sys.exit(1)
    
    return config


ROLE_COLORS = {
    'vm': '2196f3',
    'lxc': '4caf50',
}
"""Default hex color codes for device roles: 'vm' (blue) for QEMU VMs, 'lxc' (green) for LXC."""
