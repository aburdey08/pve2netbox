"""Configuration management and validation for pve2netbox."""

import ipaddress
import os
import re
import sys
from typing import Any, List, Optional, Tuple, Union
from dataclasses import dataclass, field

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

NODE_MISSING_POLICIES = ('skip', 'fail')
"""Allowed values of ``NODE_MISSING_POLICY``: skip the node, or abort the run."""

_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass
class Config:
    """
    Configuration for pve2netbox.

    Proxmox: pve_api_host, pve_api_user, pve_api_token, pve_api_secret, pve_api_verify_ssl.
    NetBox: nb_api_url, nb_api_token, nb_cluster_id, nb_cluster_name, nb_api_delay_seconds,
    nb_api_retry_total, nb_api_retry_backoff.
    Sync: sync_vms, sync_lxc, sync_tags, sync_interval_seconds, quick_check_interval_seconds.
    Roles: vm_role, lxc_role (optional device role names).
    Feature flags: dry_run, enable_cleanup, enable_metrics, metrics_port,
    enable_health_endpoint, node_missing_policy.
    """
    pve_api_host: str
    pve_api_user: str
    pve_api_token: str
    pve_api_secret: str
    pve_api_verify_ssl: bool
    nb_api_url: str
    nb_api_token: str
    nb_cluster_id: Optional[int]
    nb_api_delay_seconds: float
    nb_api_retry_total: int
    nb_api_retry_backoff: float
    sync_vms: bool
    sync_lxc: bool
    sync_tags: bool
    sync_interval_seconds: Optional[float]
    quick_check_interval_seconds: Optional[float]
    vm_role: Optional[str]
    lxc_role: Optional[str]
    dry_run: bool
    enable_cleanup: bool
    enable_metrics: bool
    metrics_port: int
    ignore_status_when_locked: bool
    primary_subnets: Tuple[IPNetwork, ...] = field(default_factory=tuple)
    nb_cluster_name: Optional[str] = None
    node_missing_policy: str = 'skip'
    enable_health_endpoint: bool = True


def load_env_file(path: str, override: bool = False) -> int:
    """
    Load ``KEY=VALUE`` pairs from an env file into ``os.environ``.

    Deliberately minimal (no dependency on python-dotenv): blank lines and
    ``#`` comments are skipped, a leading ``export`` is stripped, and matching
    surrounding quotes are removed. For unquoted values a trailing ``#`` comment
    is cut off.

    Variables already present in the environment win unless ``override`` is set —
    values injected by Docker or systemd must not be shadowed by a stale file.

    Args:
        path: Path to the env file.
        override: Replace variables that are already set.

    Returns:
        Number of variables applied to the environment.
    """
    applied = 0
    with open(path, 'r', encoding='utf-8') as env_file:
        for lineno, raw_line in enumerate(env_file, start=1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[len('export '):].lstrip()
            if '=' not in line:
                print(f'Warning: {path}:{lineno}: ignoring line without "="', file=sys.stderr)
                continue

            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()

            if not _ENV_KEY_RE.match(key):
                print(f'Warning: {path}:{lineno}: ignoring invalid variable name "{key}"',
                      file=sys.stderr)
                continue

            value = _strip_env_value(value)

            if not override and key in os.environ:
                continue

            os.environ[key] = value
            applied += 1

    return applied


def _strip_env_value(value: str) -> str:
    """
    Unquote an env-file value and drop a trailing comment.

    A quoted value ends at its closing quote, so ``"secret"  # note`` yields
    ``secret``. An unquoted value is cut at the first ``  #``/`` #`` sequence,
    which keeps values that legitimately contain ``#`` (such as a password
    without surrounding whitespace) intact.
    """
    if value[:1] in ('"', "'"):
        quote = value[0]
        closing = value.find(quote, 1)
        if closing != -1:
            return value[1:closing]
        return value[1:]

    if ' #' in value:
        return value.split(' #', 1)[0].rstrip()
    return value


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var; anything other than 'true'/'false' is an error."""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in ('true', '1', 'yes', 'on'):
        return True
    if normalized in ('false', '0', 'no', 'off'):
        return False
    raise ValueError(f'{name} must be true or false, got "{raw}"')


def load_config() -> Config:
    """
    Load and validate configuration from environment variables.

    Required: PVE_API_HOST, PVE_API_USER, PVE_API_TOKEN, PVE_API_SECRET,
    NB_API_URL, NB_API_TOKEN, and one of NB_CLUSTER_ID / NB_CLUSTER_NAME.
    Optional variables use defaults; invalid values cause exit(1) with messages
    on stderr.
    """
    errors: List[str] = []

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

    raw_cluster_id = os.getenv('NB_CLUSTER_ID')
    nb_cluster_name = os.getenv('NB_CLUSTER_NAME') or None
    nb_cluster_id: Optional[int] = None
    if raw_cluster_id:
        try:
            nb_cluster_id = int(raw_cluster_id)
        except ValueError:
            errors.append(f'NB_CLUSTER_ID must be an integer, got "{raw_cluster_id}"')
    elif not nb_cluster_name:
        errors.append(
            'NB_CLUSTER_ID or NB_CLUSTER_NAME is required '
            '(previously an unset NB_CLUSTER_ID silently defaulted to cluster 1)'
        )

    node_missing_policy = os.getenv('NODE_MISSING_POLICY', 'skip').strip().lower()
    if node_missing_policy not in NODE_MISSING_POLICIES:
        errors.append(
            f'NODE_MISSING_POLICY must be one of {", ".join(NODE_MISSING_POLICIES)}, '
            f'got "{node_missing_policy}"'
        )

    if errors:
        print('Configuration errors:', file=sys.stderr)
        for error in errors:
            print(f'  - {error}', file=sys.stderr)
        sys.exit(1)

    primary_subnets = _parse_primary_subnets(os.getenv('PRIMARY_SUBNETS'))

    try:
        config = Config(
            pve_api_host=pve_api_host,  # type: ignore
            pve_api_user=pve_api_user,  # type: ignore
            pve_api_token=pve_api_token,  # type: ignore
            pve_api_secret=pve_api_secret,  # type: ignore
            pve_api_verify_ssl=_env_flag('PVE_API_VERIFY_SSL', False),
            nb_api_url=nb_api_url,  # type: ignore
            nb_api_token=nb_api_token,  # type: ignore
            nb_cluster_id=nb_cluster_id,
            nb_api_delay_seconds=float(os.getenv('NB_API_DELAY_SECONDS', '0.2')),
            nb_api_retry_total=int(os.getenv('NB_API_RETRY_TOTAL', '5')),
            nb_api_retry_backoff=float(os.getenv('NB_API_RETRY_BACKOFF', '1.0')),
            sync_vms=_env_flag('SYNC_VMS', True),
            sync_lxc=_env_flag('SYNC_LXC', True),
            sync_tags=_env_flag('SYNC_TAGS', True),
            sync_interval_seconds=float(os.getenv('SYNC_INTERVAL_SECONDS'))
                if os.getenv('SYNC_INTERVAL_SECONDS') else None,
            quick_check_interval_seconds=float(os.getenv('QUICK_CHECK_INTERVAL_SECONDS'))
                if os.getenv('QUICK_CHECK_INTERVAL_SECONDS') else None,
            vm_role=os.getenv('VM_ROLE'),
            lxc_role=os.getenv('LXC_ROLE'),
            dry_run=_env_flag('DRY_RUN', False),
            enable_cleanup=_env_flag('ENABLE_CLEANUP', False),
            enable_metrics=_env_flag('ENABLE_METRICS', False),
            metrics_port=int(os.getenv('METRICS_PORT', '9090')),
            ignore_status_when_locked=_env_flag('IGNORE_STATUS_WHEN_LOCKED', True),
            primary_subnets=primary_subnets,
            nb_cluster_name=nb_cluster_name,
            node_missing_policy=node_missing_policy,
            enable_health_endpoint=_env_flag('ENABLE_HEALTH_ENDPOINT', True),
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


TRANSIENT_PVE_LOCKS = frozenset({'backup', 'snapshot', 'migrate', 'clone', 'rollback'})
"""PVE ``lock`` values that can cause transient ``status`` flips (e.g. backup briefly
starts a helper QEMU for a stopped VM). While locked, ``status`` must not overwrite
the value stored in NetBox to avoid changelog noise."""


PROXMOX_CLUSTER_TYPE = 'Proxmox VE'
"""NetBox cluster type created when a cluster has to be provisioned by name."""


def _parse_primary_subnets(raw: Optional[str]) -> Tuple[IPNetwork, ...]:
    """
    Parse ``PRIMARY_SUBNETS`` env value into an ordered tuple of IP networks.

    Accepts a comma- and/or whitespace-separated list (e.g.
    ``"192.168.88.0/24, 2001:db8::/64"``). Each token is parsed with
    ``ipaddress.ip_network(strict=False)``; invalid tokens print a warning to
    stderr and are skipped. Order is preserved — it defines the priority used
    when picking primary IPv4/IPv6 for a VM (first matching subnet wins).
    """
    if not raw:
        return ()

    subnets = []
    for token in raw.replace(',', ' ').split():
        token = token.strip()
        if not token:
            continue
        try:
            subnets.append(ipaddress.ip_network(token, strict=False))
        except ValueError as exc:
            print(
                f'Warning: ignoring invalid subnet in PRIMARY_SUBNETS: '
                f'"{token}" ({exc})',
                file=sys.stderr,
            )
    return tuple(subnets)


_config: Optional[Config] = None
"""Process-wide configuration, set once at startup by the CLI."""


def set_config(config: Config) -> None:
    """Store the process-wide configuration."""
    global _config  # pylint: disable=global-statement
    _config = config


def get_config() -> Config:
    """
    Return the process-wide configuration, loading it from the environment on
    first use. Lets library entry points work without going through the CLI.
    """
    global _config  # pylint: disable=global-statement
    if _config is None:
        _config = load_config()
    return _config


def current_config() -> Optional[Config]:
    """Return the configuration if it has been loaded, without loading it."""
    return _config


def describe_config(config: Config) -> List[Tuple[str, Any]]:
    """Return a list of (label, value) pairs for startup logging. No secrets."""
    return [
        ('Proxmox host', config.pve_api_host),
        ('Proxmox user', config.pve_api_user),
        ('Proxmox TLS verify', config.pve_api_verify_ssl),
        ('NetBox URL', config.nb_api_url),
        ('NetBox cluster', config.nb_cluster_name or config.nb_cluster_id),
        ('Sync VMs / LXC / tags', f'{config.sync_vms} / {config.sync_lxc} / {config.sync_tags}'),
        ('Dry run', config.dry_run),
        ('Cleanup', config.enable_cleanup),
        ('Node missing policy', config.node_missing_policy),
    ]
