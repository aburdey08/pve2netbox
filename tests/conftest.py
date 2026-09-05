"""Shared fixtures. Keeps the tests free of a live Proxmox or NetBox."""

import pytest

from pve2netbox.config import Config, set_config


def make_config(**overrides) -> Config:
    """A Config with every required field filled in, so tests set only what they mean."""
    defaults = dict(
        pve_api_host='pve.example.com',
        pve_api_user='sync@pve',
        pve_api_token='token',
        pve_api_secret='secret',
        pve_api_verify_ssl=False,
        nb_api_url='https://netbox.example.com',
        nb_api_token='nbtoken',
        nb_cluster_id=1,
        nb_api_delay_seconds=0.0,
        nb_api_retry_total=0,
        nb_api_retry_backoff=0.0,
        sync_vms=True,
        sync_lxc=True,
        sync_tags=True,
        sync_interval_seconds=None,
        quick_check_interval_seconds=None,
        vm_role=None,
        lxc_role=None,
        dry_run=False,
        enable_cleanup=False,
        enable_metrics=False,
        metrics_port=9100,
        ignore_status_when_locked=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def config():
    """Install a process-wide config and tear it down again."""
    def _install(**overrides):
        cfg = make_config(**overrides)
        set_config(cfg)
        return cfg
    yield _install
    set_config(None)
