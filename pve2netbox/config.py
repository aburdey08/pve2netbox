"""Configuration management and validation for pve2netbox."""

import ipaddress
import os
import re
import sys
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union
from dataclasses import dataclass, field

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

NODE_MISSING_POLICIES = ('skip', 'fail')
"""Allowed values of ``NODE_MISSING_POLICY``: skip the node, or abort the run."""

LXC_IP_SOURCES = ('auto', 'config', 'runtime', 'none')
"""Allowed values of ``LXC_IP_SOURCE``.

``runtime`` reads the addresses a running container actually has
(``/nodes/{node}/lxc/{vmid}/interfaces``, works with DHCP), ``config`` reads the
static ``ip=``/``ip6=`` values from the container config, ``auto`` prefers
runtime and falls back to config, and ``none`` restores the pre-1.1.0 behaviour
of not syncing container IPs at all."""

DESCRIPTION_TARGETS = ('comments', 'description')
"""Allowed values of ``DESCRIPTION_TARGET``: the NetBox field that receives the
Proxmox note. ``description`` is a short single-line field, hence the
``comments`` default."""

TEMPLATE_POLICIES = ('tag', 'skip', 'sync')
"""Allowed values of ``TEMPLATE_POLICY``: tag templates, skip them entirely, or
sync them like any other VM (the pre-1.1.0 behaviour)."""

TEMPLATE_TAG_NAME = 'pve-template'
"""NetBox tag applied to Proxmox templates when ``TEMPLATE_POLICY=tag``."""

NB_DESCRIPTION_MAX_LENGTH = 200
"""Length of NetBox's ``description`` field; longer notes are truncated."""

NB_COMMENTS_MAX_LENGTH = 5000
"""Self-imposed cap for ``comments``. The NetBox field is unbounded, but a
runaway note should not turn every sync into a large write."""

NB_PRELOAD_SCOPES = ('cluster', 'all')
"""Allowed values of ``NB_PRELOAD_SCOPE``: load only what is attached to the
target cluster, or every device, VM, interface and disk (pre-1.2.0)."""

_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_VMID_RANGE_RE = re.compile(r'^(\d+)-(\d+)$')

_VMID_RANGE_SPACING_RE = re.compile(r'\s*-\s*')
"""Whitespace around a range dash. Normalised away before the list is split on
whitespace, so that ``900 - 999`` means the range a reader expects rather than
two single IDs and a stray ``-``."""


@dataclass(frozen=True)
class VmidRanges:
    """
    Parsed ``EXCLUDE_VMIDS``: single IDs plus inclusive ranges.

    Kept as ranges rather than expanded into a set — ``900-999999`` is a
    perfectly reasonable thing to write and must not allocate a million ints.
    """
    singles: FrozenSet[int] = frozenset()
    ranges: Tuple[Tuple[int, int], ...] = ()

    def __contains__(self, vmid: object) -> bool:
        if not isinstance(vmid, int):
            return False
        if vmid in self.singles:
            return True
        return any(low <= vmid <= high for low, high in self.ranges)

    def __bool__(self) -> bool:
        return bool(self.singles or self.ranges)

    def __str__(self) -> str:
        parts = [str(vmid) for vmid in sorted(self.singles)]
        parts += [f'{low}-{high}' for low, high in self.ranges]
        return ', '.join(parts)


def parse_vmid_ranges(raw: Optional[str], errors: Optional[List[str]] = None) -> VmidRanges:
    """
    Parse ``EXCLUDE_VMIDS`` (``100,105,900-999``) into a :class:`VmidRanges`.

    Commas and/or whitespace separate; spaces around the dash are tolerated.
    A malformed entry is a configuration error, not a skipped token: a typo in
    an exclusion list would otherwise sync guests believed to be excluded.
    """
    if not raw:
        return VmidRanges()

    singles: List[int] = []
    ranges: List[Tuple[int, int]] = []
    normalised = _VMID_RANGE_SPACING_RE.sub('-', raw.replace(',', ' '))
    for token in normalised.split():
        match = _VMID_RANGE_RE.match(token)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high:
                low, high = high, low
            ranges.append((low, high))
            continue
        if token.isdigit():
            singles.append(int(token))
            continue
        message = f'EXCLUDE_VMIDS: invalid entry "{token}" (expected 100 or 900-999)'
        if errors is None:
            print(f'Warning: {message}', file=sys.stderr)
        else:
            errors.append(message)

    return VmidRanges(singles=frozenset(singles), ranges=tuple(ranges))


def parse_name_list(raw: Optional[str]) -> Tuple[str, ...]:
    """
    Parse a comma- and/or whitespace-separated list of names into a tuple.

    Order is kept and duplicates dropped, so startup logging shows what was
    configured.
    """
    if not raw:
        return ()

    names: List[str] = []
    seen = set()
    for token in raw.replace(',', ' ').split():
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(token)
    return tuple(names)


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
    Fields: lxc_ip_source, sync_description, description_target, sync_platform,
    platform_map, pool_as_tenant, template_policy.
    Filters: sync_nodes, exclude_nodes, sync_pools, include_tags, exclude_tags,
    exclude_vmids.
    Performance: nb_preload_scope.
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
    lxc_ip_source: str = 'auto'
    sync_description: bool = True
    description_target: str = 'comments'
    sync_platform: bool = False
    platform_map: Dict[str, str] = field(default_factory=dict)
    pool_as_tenant: bool = False
    template_policy: str = 'tag'
    sync_nodes: Tuple[str, ...] = ()
    exclude_nodes: Tuple[str, ...] = ()
    sync_pools: Tuple[str, ...] = ()
    include_tags: Tuple[str, ...] = ()
    exclude_tags: Tuple[str, ...] = ()
    exclude_vmids: VmidRanges = VmidRanges()
    nb_preload_scope: str = 'cluster'


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

    lxc_ip_source = os.getenv('LXC_IP_SOURCE', 'auto').strip().lower()
    if lxc_ip_source not in LXC_IP_SOURCES:
        errors.append(
            f'LXC_IP_SOURCE must be one of {", ".join(LXC_IP_SOURCES)}, got "{lxc_ip_source}"'
        )

    description_target = os.getenv('DESCRIPTION_TARGET', 'comments').strip().lower()
    if description_target not in DESCRIPTION_TARGETS:
        errors.append(
            f'DESCRIPTION_TARGET must be one of {", ".join(DESCRIPTION_TARGETS)}, '
            f'got "{description_target}"'
        )

    template_policy = os.getenv('TEMPLATE_POLICY', 'tag').strip().lower()
    if template_policy not in TEMPLATE_POLICIES:
        errors.append(
            f'TEMPLATE_POLICY must be one of {", ".join(TEMPLATE_POLICIES)}, '
            f'got "{template_policy}"'
        )

    nb_preload_scope = os.getenv('NB_PRELOAD_SCOPE', 'cluster').strip().lower()
    if nb_preload_scope not in NB_PRELOAD_SCOPES:
        errors.append(
            f'NB_PRELOAD_SCOPE must be one of {", ".join(NB_PRELOAD_SCOPES)}, '
            f'got "{nb_preload_scope}"'
        )

    sync_nodes = parse_name_list(os.getenv('SYNC_NODES'))
    exclude_nodes = parse_name_list(os.getenv('EXCLUDE_NODES'))
    sync_pools = parse_name_list(os.getenv('SYNC_POOLS'))
    include_tags = parse_name_list(os.getenv('INCLUDE_TAGS'))
    exclude_tags = parse_name_list(os.getenv('EXCLUDE_TAGS'))
    exclude_vmids = parse_vmid_ranges(os.getenv('EXCLUDE_VMIDS'), errors)
    errors.extend(_contradicting_filters(sync_nodes, exclude_nodes, include_tags, exclude_tags))

    if errors:
        print('Configuration errors:', file=sys.stderr)
        for error in errors:
            print(f'  - {error}', file=sys.stderr)
        sys.exit(1)

    primary_subnets = _parse_primary_subnets(os.getenv('PRIMARY_SUBNETS'))
    platform_map = dict(DEFAULT_PLATFORM_MAP)
    platform_map.update(_parse_platform_map(os.getenv('PLATFORM_MAP')))

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
            lxc_ip_source=lxc_ip_source,
            sync_description=_env_flag('SYNC_DESCRIPTION', True),
            description_target=description_target,
            sync_platform=_env_flag('SYNC_PLATFORM', False),
            platform_map=platform_map,
            pool_as_tenant=_env_flag('POOL_AS_TENANT', False),
            template_policy=template_policy,
            sync_nodes=sync_nodes,
            exclude_nodes=exclude_nodes,
            sync_pools=sync_pools,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            exclude_vmids=exclude_vmids,
            nb_preload_scope=nb_preload_scope,
        )
    except (ValueError, TypeError) as e:
        print(f'Configuration parsing error: {e}', file=sys.stderr)
        sys.exit(1)

    return config


def _contradicting_filters(
        sync_nodes: Tuple[str, ...],
        exclude_nodes: Tuple[str, ...],
        include_tags: Tuple[str, ...],
        exclude_tags: Tuple[str, ...],
) -> List[str]:
    """
    Report filter pairs that can never both be satisfied.

    The same node or tag in both an include and an exclude list is always a
    mistake: the exclusion wins and the guest silently disappears.
    """
    problems: List[str] = []
    both_nodes = {n.lower() for n in sync_nodes} & {n.lower() for n in exclude_nodes}
    if both_nodes:
        problems.append(
            f'SYNC_NODES and EXCLUDE_NODES both list: {", ".join(sorted(both_nodes))}'
        )
    both_tags = {t.lower() for t in include_tags} & {t.lower() for t in exclude_tags}
    if both_tags:
        problems.append(
            f'INCLUDE_TAGS and EXCLUDE_TAGS both list: {", ".join(sorted(both_tags))}'
        )
    return problems


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


DEFAULT_PLATFORM_MAP = {
    # QEMU ostype values are coarse — they describe the guest family, not the distro.
    'l24': 'Linux',
    'l26': 'Linux',
    'solaris': 'Solaris',
    'wxp': 'Windows XP',
    'w2k': 'Windows 2000',
    'w2k3': 'Windows Server 2003',
    'w2k8': 'Windows Server 2008',
    'wvista': 'Windows Vista',
    'win7': 'Windows 7',
    'win8': 'Windows 8',
    'win10': 'Windows 10',
    'win11': 'Windows 11',
    # LXC ostype comes from the template and names the distribution exactly.
    'alpine': 'Alpine Linux',
    'archlinux': 'Arch Linux',
    'almalinux': 'AlmaLinux',
    'centos': 'CentOS',
    'debian': 'Debian',
    'devuan': 'Devuan',
    'fedora': 'Fedora',
    'gentoo': 'Gentoo',
    'nixos': 'NixOS',
    'opensuse': 'openSUSE',
    'rocky': 'Rocky Linux',
    'ubuntu': 'Ubuntu',
}
"""Default ``ostype`` → NetBox platform name mapping used by ``SYNC_PLATFORM``.

``other`` and ``unmanaged`` are deliberately absent: they carry no information,
and inventing a platform for them would be worse than leaving the field alone.
Override or extend through ``PLATFORM_MAP``."""


def _parse_platform_map(raw: Optional[str]) -> Dict[str, str]:
    """
    Parse ``PLATFORM_MAP`` (``l26=Linux,win11=Windows 11``) into a dict.

    Keys are lowercased ``ostype`` values; entries override
    ``DEFAULT_PLATFORM_MAP``. An empty value (``l26=``) suppresses the default
    mapping for that ``ostype``. Malformed entries print a warning and are
    skipped rather than aborting the run.
    """
    if not raw:
        return {}

    mapping: Dict[str, str] = {}
    for token in raw.split(','):
        token = token.strip()
        if not token:
            continue
        if '=' not in token:
            print(
                f'Warning: ignoring invalid entry in PLATFORM_MAP: "{token}" '
                f'(expected ostype=Platform Name)',
                file=sys.stderr,
            )
            continue
        ostype, platform_name = token.split('=', 1)
        ostype = ostype.strip().lower()
        platform_name = platform_name.strip()
        if not ostype:
            print(f'Warning: ignoring PLATFORM_MAP entry without ostype: "{token}"',
                  file=sys.stderr)
            continue
        mapping[ostype] = platform_name
    return mapping


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
        ('LXC IP source', config.lxc_ip_source),
        ('Sync description', f'{config.sync_description} → {config.description_target}'),
        ('Sync platform', config.sync_platform),
        ('Pool as tenant', config.pool_as_tenant),
        ('Template policy', config.template_policy),
        ('NetBox preload scope', config.nb_preload_scope),
    ] + describe_filters(config)


def describe_filters(config: Config) -> List[Tuple[str, Any]]:
    """(label, value) pairs for the configured filters; empty when there are none."""
    described: List[Tuple[str, Any]] = []
    for label, values in (
        ('SYNC_NODES', config.sync_nodes),
        ('EXCLUDE_NODES', config.exclude_nodes),
        ('SYNC_POOLS', config.sync_pools),
        ('INCLUDE_TAGS', config.include_tags),
        ('EXCLUDE_TAGS', config.exclude_tags),
    ):
        if values:
            described.append((f'Filter {label}', ', '.join(values)))
    if config.exclude_vmids:
        described.append(('Filter EXCLUDE_VMIDS', str(config.exclude_vmids)))
    return described
