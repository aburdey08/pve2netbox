# pylint: disable=fixme,too-many-branches

"""
pve2netbox: Synchronize Proxmox Virtual Environment (PVE) information to a NetBox instance.
"""

import ipaddress
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pynetbox
import requests
from proxmoxer import ProxmoxAPI, ResourceException

from . import shutdown
from .api.netbox import make_netbox_session, resolve_cluster
from .api.proxmox import QUICK_CHECK_SOURCE_CLUSTER, create_proxmox_api
from .api.proxmox import quick_check_changes as _api_quick_check_changes
from .config import (
    Config,
    NB_COMMENTS_MAX_LENGTH,
    NB_DESCRIPTION_MAX_LENGTH,
    TEMPLATE_TAG_NAME,
    TRANSIENT_PVE_LOCKS,
    get_config,
    load_config,
    set_config,
)
from .filters import LXC, QEMU, FilterDecisions, Guest, get_filters
from .logger import logger, log_section
from .lxc import build_lxc_agent_data
from .metrics import metrics
from .utils import (
    parse_pve_network_definition as _parse_pve_network_definition,
    parse_pve_disk_definition as _parse_pve_disk_definition,
    parse_pve_disk_size as _process_pve_disk_size,
    get_virtual_machine_vcpus as _get_virtual_machine_vcpus,
    decode_pve_description,
)
from .version import __version__

__all__ = [
    '__version__',
    'main',
    'quick_check_changes',
    'sync_specific_vms',
    'cleanup_stale_vms',
]


def _cfg() -> Config:
    """
    Configuration for the current process.

    Loaded once by the CLI; falls back to reading the environment on first use
    so the package still works when imported as a library.
    """
    return get_config()


def _make_netbox_session() -> requests.Session:
    """Create requests session with retry on 502/503/429 and optional delay between requests."""
    return make_netbox_session(_cfg())


def _provision_custom_fields(_nb_api: pynetbox.api) -> None:
    """Create required custom fields in NetBox if they do not exist."""
    if _cfg().dry_run:
        logger.info('[DRY RUN] Would provision custom fields')
        return

    logger.info('Provisioning custom fields...')
    existing_fields = {cf.name: cf for cf in _nb_api.extras.custom_fields.all()}
    required_fields = [
        {
            'name': 'autostart',
            'label': 'Autostart',
            'type': 'boolean',
            'object_types': ['virtualization.virtualmachine'],
            'description': 'VM autostart on boot',
        },
        {
            'name': 'replicated',
            'label': 'Replicated',
            'type': 'boolean',
            'object_types': ['virtualization.virtualmachine'],
            'description': 'VM replication enabled',
        },
        {
            'name': 'ha',
            'label': 'Failover',
            'type': 'boolean',
            'object_types': ['virtualization.virtualmachine'],
            'description': 'VM high availability enabled',
        },
        {
            'name': 'backup',
            'label': 'Backup',
            'type': 'boolean',
            'object_types': ['virtualization.virtualdisk'],
            'description': 'Disk backup enabled',
        },
        {
            'name': 'dns_name',
            'label': 'DNS Name',
            'type': 'text',
            'object_types': ['ipam.prefix'],
            'description': 'DNS domain name for the prefix',
        },
    ]
    for field_def in required_fields:
        if field_def['name'] in existing_fields:
            logger.info(f'  ✓ Custom field "{field_def["name"]}" already exists')
        else:
            try:
                _nb_api.extras.custom_fields.create(
                    name=field_def['name'],
                    label=field_def['label'],
                    type=field_def['type'],
                    object_types=field_def['object_types'],
                    description=field_def.get('description', ''),
                )
                logger.info(f'  + Created custom field "{field_def["name"]}"')
            except Exception as e:
                logger.error(f'  ! Failed to create custom field "{field_def["name"]}"')
                logger.error(f'    Error: {e}')
                logger.error(f'    Please create this field manually in NetBox UI:'
                      f' Name="{field_def["name"]}", Type={field_def["type"]}, '
                      f'Object Types={", ".join(field_def["object_types"])}')


def _provision_roles(_nb_api: pynetbox.api) -> None:
    """Create device roles in NetBox if specified in env (VM_ROLE, LXC_ROLE) and do not exist."""
    vm_role_name = _cfg().vm_role
    lxc_role_name = _cfg().lxc_role
    
    if not vm_role_name and not lxc_role_name:
        logger.info('Provisioning device roles...')
        logger.info('  No VM_ROLE or LXC_ROLE configured, skipping')
        return
    
    if _cfg().dry_run:
        logger.info('Provisioning device roles...')
        logger.info('[DRY RUN] Would provision device roles')
        return
    
    logger.info('Provisioning device roles...')
    existing_roles = {role.name: role for role in _nb_api.dcim.device_roles.all()}
    if vm_role_name:
        if vm_role_name in existing_roles:
            logger.info(f'  ✓ Role "{vm_role_name}" already exists (for VMs)')
        else:
            logger.info(f'  Role "{vm_role_name}" not found, will create')
    if lxc_role_name and lxc_role_name != vm_role_name:
        if lxc_role_name in existing_roles:
            logger.info(f'  ✓ Role "{lxc_role_name}" already exists (for LXC)')
        else:
            logger.info(f'  Role "{lxc_role_name}" not found, will create')
    
    roles_to_create = []
    if vm_role_name and vm_role_name not in existing_roles:
        roles_to_create.append({
            'name': vm_role_name,
            'slug': vm_role_name.lower().replace(' ', '-'),
            'color': '2196f3',
            'vm_role': True,
            'description': 'QEMU/KVM Virtual Machine',
        })
    
    if lxc_role_name and lxc_role_name not in existing_roles and lxc_role_name != vm_role_name:
        roles_to_create.append({
            'name': lxc_role_name,
            'slug': lxc_role_name.lower().replace(' ', '-'),
            'color': '4caf50',
            'vm_role': True,
            'description': 'LXC Container',
        })
    
    for role_def in roles_to_create:
        try:
            _nb_api.dcim.device_roles.create(
                name=role_def['name'],
                slug=role_def['slug'],
                color=role_def['color'],
                vm_role=role_def['vm_role'],
                description=role_def.get('description', ''),
            )
            logger.info(f'  + Created role "{role_def["name"]}"')
        except Exception as e:
            logger.error(f'  ! Failed to create role "{role_def["name"]}": {e}')


NB_FILTER_CHUNK_SIZE = 50
"""IDs per filtered NetBox request: short enough for any proxy in front of
NetBox, long enough to replace hundreds of lookups with a handful."""


def _fetch_all(_endpoint: Any, _label: str) -> List[Any]:
    """Read a whole NetBox endpoint, logging what it cost."""
    records = list(_endpoint.all())
    logger.debug(f'  - Loaded {len(records)} {_label} (unfiltered)')
    return records


def _is_filter_rejected(_error: Exception) -> bool:
    """
    True when NetBox answered HTTP 400 — "I do not know that filter".

    Only that means the query must be widened. Widening on a timeout or a 502
    would turn a momentary outage into a read of the whole inventory.
    """
    status = getattr(getattr(_error, 'req', None), 'status_code', None)
    if status is None:
        status = getattr(getattr(_error, 'response', None), 'status_code', None)
    return status == 400


def _fetch_filtered(
        _endpoint: Any,
        _label: str,
        _param_sets: List[dict],
        _covers: Optional[Callable[[List[Any]], bool]] = None,
) -> List[Any]:
    """
    Read a NetBox endpoint, trying each parameter set until one is accepted.

    Which filters exist differs between NetBox versions, so a rejected one
    falls back to the next; the caller ends the list with ``{}`` ("load
    everything") — slower, always correct. Any other failure is raised.

    ``_covers`` catches what a rejection cannot report: a filter NetBox answers
    without honouring it in full — a multi-value query narrowed to its last
    value, or a case-sensitive one missing a differently spelled name. An
    answer it turns down is widened exactly like a rejected query. The last
    parameter set is accepted whatever it returns; there is nothing wider left.
    """
    last = len(_param_sets) - 1
    for index, params in enumerate(_param_sets):
        try:
            records = list(_endpoint.filter(**params) if params else _endpoint.all())
        except Exception as e:  # pylint: disable=broad-except
            if index == last or not _is_filter_rejected(e):
                raise
            logger.warning(
                f'  NetBox rejected {list(params)} for {_label} ({e}); trying a wider query'
            )
            continue

        if index < last and _covers is not None and not _covers(records):
            logger.warning(
                f'  NetBox answered {list(params)} for {_label} with {len(records)} record(s), '
                f'which do not cover what was asked for; trying a wider query'
            )
            continue

        logger.debug(f'  - Loaded {len(records)} {_label} ({params or "unfiltered"})')
        return records
    return []


def _fetch_for_vms(
        _endpoint: Any,
        _label: str,
        _vm_ids: List[int],
) -> Tuple[List[Any], Set[int]]:
    """
    Read the records of the given VM IDs in chunks; returns ``(records, unloaded)``.

    A failed chunk costs only its own VMs, and those count as *unknown*, never
    as *empty*: an empty cache makes the sync duplicate every interface and
    disk the VM already has.
    """
    records: List[Any] = []
    unloaded: Set[int] = set()
    for start in range(0, len(_vm_ids), NB_FILTER_CHUNK_SIZE):
        chunk = _vm_ids[start:start + NB_FILTER_CHUNK_SIZE]
        try:
            records.extend(_endpoint.filter(virtual_machine_id=chunk))
        except Exception as e:  # pylint: disable=broad-except
            unloaded.update(chunk)
            logger.warning(
                f'  Failed to load {_label} for {len(chunk)} VM(s) ({e}); '
                f'those VMs will be skipped this pass'
            )
    logger.debug(
        f'  - Loaded {len(records)} {_label} '
        f'(for {len(_vm_ids) - len(unloaded)} of {len(_vm_ids)} VMs)'
    )
    return records, unloaded


def _fetch_cluster_scoped(
        _endpoint: Any,
        _label: str,
        _cluster_id: int,
        _vm_ids: List[int],
) -> Tuple[List[Any], Set[int]]:
    """
    Read everything of one kind belonging to the cluster's VMs.

    One ``cluster_id`` query, falling back to chunked ``virtual_machine_id``
    lookups where the endpoint has no cluster filter. Any non-400 failure is
    raised: a partial cache must never pass for a complete one.
    """
    if not _vm_ids:
        return [], set()
    try:
        records = list(_endpoint.filter(cluster_id=_cluster_id))
        logger.debug(f'  - Loaded {len(records)} {_label} (cluster {_cluster_id})')
        return records, set()
    except Exception as e:  # pylint: disable=broad-except
        if not _is_filter_rejected(e):
            raise
        logger.debug(f'  - No cluster_id filter for {_label} ({e}); querying by VM')
        return _fetch_for_vms(_endpoint, _label, _vm_ids)


def _empty_nb_objects() -> dict:
    """The cache layout shared by the full and the incremental loader."""
    return {
        'devices': {},
        'virtual_machines': {},
        'virtual_machines_by_name_cluster': {},
        'virtual_machines_interfaces': {},
        'mac_addresses': {},
        'prefixes': {},
        'ip_addresses': {},
        'vlans': {},
        'disks': {},
        'tags': {},
        'roles': {},
        'platforms': {},
        'tenants': {},
        'incomplete_vmids': set(),
    }


def _mark_incomplete_preload(_nb_objects: dict, _unloaded_vm_ids: Set[int]) -> None:
    """
    Record the VMIDs whose NetBox cache could not be filled, so they get skipped.

    Anything missing from the cache gets created, so syncing such a guest would
    duplicate every interface and disk it already has.
    """
    if not _unloaded_vm_ids:
        return
    for serial, nb_vm in _nb_objects['virtual_machines'].items():
        if getattr(nb_vm, 'id', None) in _unloaded_vm_ids:
            try:
                _nb_objects['incomplete_vmids'].add(int(serial))
            except (ValueError, TypeError):
                continue


def _preload_incomplete(_nb_objects: dict, _vmid: int) -> bool:
    """True when this guest's NetBox objects failed to preload; it must be skipped."""
    return _vmid in _nb_objects.get('incomplete_vmids', set())


def _index_nb_interfaces(_nb_objects: dict, _interfaces: List[Any]) -> None:
    """Index VM interfaces by owning VM and interface name."""
    for _nb_interface in _interfaces:
        vm_id = _nb_interface.virtual_machine.id
        _nb_objects['virtual_machines_interfaces'].setdefault(vm_id, {})
        _nb_objects['virtual_machines_interfaces'][vm_id][_nb_interface.name] = _nb_interface


def _index_nb_disks(_nb_objects: dict, _disks: List[Any]) -> None:
    """Index virtual disks by owning VM and disk name."""
    for _nb_disk in _disks:
        vm_id = _nb_disk.virtual_machine.id
        _nb_objects['disks'].setdefault(vm_id, {})
        _nb_objects['disks'][vm_id][_nb_disk.name] = _nb_disk


def _load_nb_shared_objects(_nb_api: pynetbox.api, _nb_objects: dict) -> Dict[str, int]:
    """
    Load the caches that stay global whatever ``NB_PRELOAD_SCOPE`` says.

    An IP or MAC that already exists elsewhere in NetBox must be found, not
    duplicated, so scoping these would change what the sync writes — which is
    exactly what ``NB_PRELOAD_SCOPE`` must not do. VLANs, tags and roles are
    small lookup tables.
    """
    counts: Dict[str, int] = {}

    for _nb_mac_address in _fetch_all(_nb_api.dcim.mac_addresses, 'MAC addresses'):
        _nb_objects['mac_addresses'][_nb_mac_address.mac_address] = _nb_mac_address
    counts['MACs'] = len(_nb_objects['mac_addresses'])

    for _nb_prefix in _fetch_all(_nb_api.ipam.prefixes, 'prefixes'):
        _nb_objects['prefixes'][_nb_prefix.prefix] = _nb_prefix
    counts['prefixes'] = len(_nb_objects['prefixes'])

    for _nb_ip_address in _fetch_all(_nb_api.ipam.ip_addresses, 'IP addresses'):
        _nb_objects['ip_addresses'][_nb_ip_address['address']] = _nb_ip_address
    counts['IPs'] = len(_nb_objects['ip_addresses'])

    for _nb_vlan in _fetch_all(_nb_api.ipam.vlans, 'VLANs'):
        _nb_objects['vlans'][str(_nb_vlan.vid)] = _nb_vlan
    counts['VLANs'] = len(_nb_objects['vlans'])

    for _nb_tag in _fetch_all(_nb_api.extras.tags, 'tags'):
        _nb_objects['tags'][_nb_tag.name] = _nb_tag

    for _nb_role in _fetch_all(_nb_api.dcim.device_roles, 'device roles'):
        _nb_objects['roles'][_nb_role.name] = _nb_role
        _nb_objects['roles'][str(_nb_role.id)] = _nb_role

    return counts


def _names_all_present(_wanted: List[str]) -> Callable[[List[Any]], bool]:
    """
    Build a :func:`_fetch_filtered` check: every wanted name is among the answer.

    Case-folded, because that is how the device cache is keyed and looked up.
    """
    wanted = {name.lower() for name in _wanted}

    def _covers(records: List[Any]) -> bool:
        found = {(getattr(record, 'name', '') or '').lower() for record in records}
        missing = wanted - found
        if missing:
            logger.debug(f'  - Not named in the answer: {", ".join(sorted(missing))}')
        return not missing

    return _covers


def _load_nb_devices(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _node_names: Optional[List[str]],
        _scoped: bool,
) -> int:
    """
    Cache the NetBox devices the Proxmox nodes map to.

    Scoped: only devices named like a Proxmox node, matched case-insensitively
    (``name__ie``) as the later lookup is, with a case-sensitive query and an
    unfiltered read as fallbacks for older NetBox versions.

    A scoped answer that does not name every node is widened rather than
    trusted. A node whose device is missing from the cache is read as "no such
    device in NetBox", which stops the whole node (``NODE_MISSING_POLICY=skip``)
    or the process (``fail``) — and a skipped node's guests never reach
    ``current_vmids``, so ``ENABLE_CLEANUP=true`` would delete them. Widening
    costs one broader read; the alternative costs records.
    """
    if _scoped:
        if not _node_names:
            # Every node was filtered out; there is no device left to look up.
            logger.debug('  - No nodes in scope, skipping the device query')
            return 0
        devices = _fetch_filtered(
            _nb_api.dcim.devices,
            'devices',
            [{'name__ie': _node_names}, {'name': _node_names}, {}],
            _covers=_names_all_present(_node_names),
        )
    else:
        devices = _fetch_all(_nb_api.dcim.devices, 'devices')

    for _nb_device in devices:
        _nb_objects['devices'][_nb_device.name.lower()] = _nb_device
    return len(devices)


def _load_nb_objects(
        _nb_api: pynetbox.api,
        _node_names: Optional[List[str]] = None,
) -> dict:
    """
    Load the NetBox objects needed for a full sync into one cache dict.

    Under ``NB_PRELOAD_SCOPE=cluster`` VMs, their interfaces and their disks are
    fetched by cluster and devices narrowed to ``_node_names``; ``all`` reads
    every one of them, which is only useful to adopt a VM from another cluster.
    IPs, prefixes, MACs, VLANs, tags and roles are always global — see
    :func:`_load_nb_shared_objects`.

    Keys are what the sync expects: device name lowercased, VM by serial, and
    interfaces and disks by owning VM id.
    """
    config = _cfg()
    cluster_id = config.nb_cluster_id
    scoped = config.nb_preload_scope == 'cluster' and cluster_id is not None
    logger.info(f'Loading NetBox objects (scope: {"cluster" if scoped else "all"})...')

    _nb_objects = _empty_nb_objects()
    counts: Dict[str, int] = {}

    logger.debug('  - Loading devices...')
    counts['devices'] = _load_nb_devices(_nb_api, _nb_objects, _node_names, scoped)

    logger.debug('  - Loading virtual machines...')
    if scoped:
        virtual_machines = _fetch_filtered(
            _nb_api.virtualization.virtual_machines,
            'virtual machines',
            [{'cluster_id': cluster_id}, {}],
        )
    else:
        virtual_machines = _fetch_all(_nb_api.virtualization.virtual_machines, 'virtual machines')
    for _nb_virtual_machine in virtual_machines:
        _index_nb_virtual_machine(_nb_objects, _nb_virtual_machine)
    vm_ids = [vm.id for vm in virtual_machines]
    counts['VMs'] = len(virtual_machines)

    unloaded: Set[int] = set()

    logger.debug('  - Loading interfaces...')
    if scoped:
        interfaces, failed = _fetch_cluster_scoped(
            _nb_api.virtualization.interfaces, 'interfaces', cluster_id, vm_ids)
        unloaded |= failed
    else:
        interfaces = _fetch_all(_nb_api.virtualization.interfaces, 'interfaces')
    _index_nb_interfaces(_nb_objects, interfaces)
    counts['interfaces'] = len(interfaces)

    logger.debug('  - Loading virtual disks...')
    if scoped:
        disks, failed = _fetch_cluster_scoped(
            _nb_api.virtualization.virtual_disks, 'virtual disks', cluster_id, vm_ids)
        unloaded |= failed
    else:
        disks = _fetch_all(_nb_api.virtualization.virtual_disks, 'virtual disks')
    _index_nb_disks(_nb_objects, disks)
    counts['disks'] = len(disks)

    _mark_incomplete_preload(_nb_objects, unloaded)

    counts.update(_load_nb_shared_objects(_nb_api, _nb_objects))
    _load_nb_platforms_and_tenants(_nb_api, _nb_objects)

    if _nb_objects['incomplete_vmids']:
        logger.warning(
            f'Incomplete NetBox preload for {len(_nb_objects["incomplete_vmids"])} VM(s); '
            f'they are skipped this pass to avoid creating duplicates'
        )
    logger.info(
        'NetBox objects loaded: '
        + ', '.join(f'{value} {name}' for name, value in counts.items())
    )
    return _nb_objects


def _load_nb_platforms_and_tenants(_nb_api: pynetbox.api, _nb_objects: dict) -> None:
    """
    Cache NetBox platforms and tenants by name for SYNC_PLATFORM / POOL_AS_TENANT.

    Both lists are small and only fetched when the corresponding feature is on,
    so a run that does not use them costs nothing extra.
    """
    config = _cfg()
    if config.sync_platform:
        logger.debug('  - Loading platforms...')
        try:
            for _nb_platform in _nb_api.dcim.platforms.all():
                _nb_objects['platforms'][_nb_platform.name] = _nb_platform
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Failed to load platforms: {e}')
    if config.pool_as_tenant:
        logger.debug('  - Loading tenants...')
        try:
            for _nb_tenant in _nb_api.tenancy.tenants.all():
                _nb_objects['tenants'][_nb_tenant.name] = _nb_tenant
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Failed to load tenants: {e}')


def _process_pve_tags(
        _pve_api: ProxmoxAPI,
        _nb_api: pynetbox.api,
        _nb_objects: dict,
) -> dict:
    """
    Ensure Proxmox pools exist as NetBox tags; create tag if missing.
    Pools are represented as tags named ``Pool/<poolid>``.
    """
    for _pve_pool in _pve_api.pools.get():
        _tag_name = f'Pool/{_pve_pool["poolid"]}'
        _nb_tag = _nb_objects['tags'].get(_tag_name)
        if _nb_tag is None:
            _nb_tag = _nb_api.extras.tags.create(
                name=_tag_name,
                slug=f'pool-{_pve_pool["poolid"]}'.lower(),
                description=f'Proxmox pool {_pve_pool["poolid"]}',
            )
            _nb_objects['tags'][_nb_tag.name] = _nb_tag

    return _nb_objects


def _ensure_nb_tag(tag_name: str, _nb_api: pynetbox.api, _nb_objects: dict) -> None:
    """Create a NetBox tag for ``tag_name`` if it does not already exist."""
    if tag_name in _nb_objects['tags']:
        return
    slug = tag_name.lower().replace(' ', '-')
    _nb_tag = _nb_api.extras.tags.create(name=tag_name, slug=slug)
    _nb_objects['tags'][_nb_tag.name] = _nb_tag


def _ensure_nb_platform(platform_name: str, _nb_api: pynetbox.api, _nb_objects: dict) -> Optional[int]:
    """Return the ID of the NetBox platform named ``platform_name``, creating it if missing."""
    nb_platform = _nb_objects['platforms'].get(platform_name)
    if nb_platform is not None:
        return nb_platform.id

    slug = _slugify(platform_name)
    try:
        nb_platform = _nb_api.dcim.platforms.create(name=platform_name, slug=slug)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(f'      Warning: could not create platform "{platform_name}": {e}')
        return None

    _nb_objects['platforms'][nb_platform.name] = nb_platform
    logger.info(f'      Created NetBox platform: {platform_name}')
    return nb_platform.id


def _ensure_nb_tenant(tenant_name: str, _nb_api: pynetbox.api, _nb_objects: dict) -> Optional[int]:
    """Return the ID of the NetBox tenant named ``tenant_name``, creating it if missing."""
    nb_tenant = _nb_objects['tenants'].get(tenant_name)
    if nb_tenant is not None:
        return nb_tenant.id

    slug = _slugify(tenant_name)
    try:
        nb_tenant = _nb_api.tenancy.tenants.create(name=tenant_name, slug=slug)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(f'      Warning: could not create tenant "{tenant_name}": {e}')
        return None

    _nb_objects['tenants'][nb_tenant.name] = nb_tenant
    logger.info(f'      Created NetBox tenant: {tenant_name}')
    return nb_tenant.id


def _slugify(name: str) -> str:
    """Build a NetBox slug from a display name (``Alpine Linux`` → ``alpine-linux``)."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug or 'unnamed'


def _optional_vm_field_values(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _pve_config: dict,
        _pve_pool: Optional[str],
) -> dict:
    """
    Values for the opt-in NetBox fields: description, platform and tenant.

    Only fields that are enabled *and* have something to say are returned. A
    guest without a note, without a recognized ``ostype`` or outside any pool
    leaves the corresponding NetBox field untouched — overwriting a manually
    curated value with an empty one is worse than not syncing it.
    """
    config = _cfg()
    values: Dict[str, Any] = {}

    if config.sync_description:
        max_length = (NB_COMMENTS_MAX_LENGTH if config.description_target == 'comments'
                      else NB_DESCRIPTION_MAX_LENGTH)
        description = decode_pve_description(_pve_config.get('description'), max_length)
        if description:
            values[config.description_target] = description

    if config.sync_platform:
        ostype = str(_pve_config.get('ostype') or '').lower()
        platform_name = config.platform_map.get(ostype)
        if platform_name:
            platform_id = _ensure_nb_platform(platform_name, _nb_api, _nb_objects)
            if platform_id is not None:
                values['platform'] = platform_id
        elif ostype:
            logger.debug(f'      ostype "{ostype}" has no platform mapping, leaving platform unset')

    if config.pool_as_tenant and _pve_pool:
        tenant_id = _ensure_nb_tenant(_pve_pool, _nb_api, _nb_objects)
        if tenant_id is not None:
            values['tenant'] = tenant_id

    return values


def _apply_optional_vm_fields(_nb_virtual_machine: Any, _field_values: dict) -> None:
    """
    Apply the opt-in field values to an existing NetBox VM.

    pynetbox only sends attributes that actually differ, so assigning an
    unchanged value here does not produce a changelog entry.
    """
    for field_name, value in _field_values.items():
        setattr(_nb_virtual_machine, field_name, value)


def _is_pve_entity_transiently_locked(_pve_entity: dict) -> bool:
    """
    Return True if PVE reports a transient ``lock`` that can briefly flip ``status``
    (e.g. ``backup`` temporarily starts a helper QEMU for an offline VM) and the
    IGNORE_STATUS_WHEN_LOCKED feature is enabled.
    """
    if not _cfg().ignore_status_when_locked:
        return False
    return _pve_entity.get('lock') in TRANSIENT_PVE_LOCKS


def _get_role_id(_nb_objects: dict, role_name_or_id: Optional[str]) -> Optional[int]:
    """Resolve device role ID by name or ID (e.g. from VM_ROLE/LXC_ROLE env)."""
    if not role_name_or_id:
        return None
    role = _nb_objects['roles'].get(role_name_or_id)
    if role:
        return role.id
    
    return None


def _index_nb_virtual_machine(_nb_objects: dict, _nb_virtual_machine: Any) -> None:
    """Index VM in local caches by serial and by (name, cluster_id)."""
    serial = getattr(_nb_virtual_machine, 'serial', None)
    if serial not in (None, ''):
        _nb_objects['virtual_machines'][str(serial)] = _nb_virtual_machine

    cluster = getattr(_nb_virtual_machine, 'cluster', None)
    cluster_id = getattr(cluster, 'id', None)
    name = getattr(_nb_virtual_machine, 'name', None)
    if cluster_id is not None and name:
        _nb_objects['virtual_machines_by_name_cluster'][(name, int(cluster_id))] = _nb_virtual_machine


def _get_nb_vm_for_sync(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        vmid: int,
        vm_name: str,
) -> Optional[Any]:
    """
    Find existing NetBox VM for sync.
    First try serial=vmid cache, then fallback to unique (name, cluster_id).
    """
    vm = _nb_objects['virtual_machines'].get(str(vmid))
    if vm is not None:
        return vm

    cluster_id = _cfg().nb_cluster_id
    vm = _nb_objects['virtual_machines_by_name_cluster'].get((vm_name, cluster_id))
    if vm is not None:
        return vm

    # Important for quick sync: changed VM may not be preloaded if serial is empty.
    try:
        candidates = list(_nb_api.virtualization.virtual_machines.filter(name=vm_name, cluster_id=cluster_id))
    except Exception as e:
        logger.warning(f'Failed to load VM by name/cluster ({vm_name}, {cluster_id}): {e}')
        return None

    if not candidates:
        return None

    vm = candidates[0]
    _index_nb_virtual_machine(_nb_objects, vm)
    logger.info(f'      Matched existing NetBox VM by name+cluster: {vm_name} (cluster {cluster_id})')
    return vm


def _process_pve_lxc_container(
        _pve_api: ProxmoxAPI,
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _nb_device: any,
        _pve_tags: [str],
        _pve_container: dict,
        _is_replicated: bool,
        _has_ha: bool,
        _pve_pool: Optional[str] = None,
) -> dict:
    """
    Sync one LXC container from Proxmox to NetBox.
    Creates or updates the VM record, then syncs network interfaces and disks.
    LXC is represented in NetBox as a virtual machine; role from LXC_ROLE env.
    IP addresses come from LXC_IP_SOURCE (runtime interfaces and/or static config).
    """
    _pve_node_name = _nb_device.name.lower()
    pve_container_config = _pve_api.nodes(_pve_node_name).lxc(_pve_container['vmid']).config.get()
    lxc_role_id = _get_role_id(_nb_objects, _cfg().lxc_role)
    vm_name = pve_container_config.get('hostname', _pve_container['name'])
    is_locked = _is_pve_entity_transiently_locked(_pve_container)
    container_is_running = _pve_container['status'] == 'running'
    agent_data_by_mac = build_lxc_agent_data(
        _pve_api,
        _pve_node_name,
        _pve_container['vmid'],
        pve_container_config,
        container_is_running,
        _cfg().lxc_ip_source,
    )
    optional_fields = _optional_vm_field_values(
        _nb_api, _nb_objects, pve_container_config, _pve_pool
    )
    nb_virtual_machine = _get_nb_vm_for_sync(_nb_api, _nb_objects, _pve_container['vmid'], vm_name)
    if nb_virtual_machine is None:
        create_params = {
            'serial': _pve_container['vmid'],
            'name': vm_name,
            'site': _nb_device.site.id,
            'cluster': _cfg().nb_cluster_id,
            'device': _nb_device.id,
            'vcpus': pve_container_config.get('cores', 1),
            'memory': int(pve_container_config.get('memory', 512)),
            # While locked (e.g. backup) PVE status can flip; fall back to 'offline' for
            # creation so we don't persist a transient 'active'.
            'status': 'offline' if is_locked
                      else ('active' if _pve_container['status'] == 'running' else 'offline'),
            'tags': list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags)),
            'custom_fields': {
                'autostart': pve_container_config.get('onboot') == 1,
                'replicated': _is_replicated,
                'ha': _has_ha,
            }
        }
        if lxc_role_id:
            create_params['role'] = lxc_role_id
        create_params.update(optional_fields)

        nb_virtual_machine = _nb_api.virtualization.virtual_machines.create(**create_params)
        _index_nb_virtual_machine(_nb_objects, nb_virtual_machine)
    else:
        nb_virtual_machine.serial = _pve_container['vmid']
        nb_virtual_machine.name = vm_name
        nb_virtual_machine.site = _nb_device.site.id
        nb_virtual_machine.cluster = _cfg().nb_cluster_id
        nb_virtual_machine.device = _nb_device.id
        nb_virtual_machine.vcpus = pve_container_config.get('cores', 1)
        nb_virtual_machine.memory = int(pve_container_config.get('memory', 512))
        if is_locked:
            logger.debug(
                f'      LXC {_pve_container["vmid"]} is locked ({_pve_container.get("lock")}); '
                f'keeping existing NetBox status'
            )
        else:
            nb_virtual_machine.status = 'active' if _pve_container['status'] == 'running' else 'offline'
        nb_virtual_machine.tags = list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags))
        if lxc_role_id:
            nb_virtual_machine.role = lxc_role_id
        nb_virtual_machine.custom_fields['autostart'] = pve_container_config.get('onboot') == 1
        nb_virtual_machine.custom_fields['replicated'] = _is_replicated
        nb_virtual_machine.custom_fields['ha'] = _has_ha
        _apply_optional_vm_fields(nb_virtual_machine, optional_fields)
        nb_virtual_machine.save()
        _index_nb_virtual_machine(_nb_objects, nb_virtual_machine)
    _process_pve_lxc_network_interfaces(
        _nb_api,
        _nb_objects,
        pve_container_config,
        nb_virtual_machine,
        agent_data_by_mac,
    )
    _process_pve_lxc_disks(
        _nb_api,
        _nb_objects,
        pve_container_config,
        nb_virtual_machine,
    )

    return _nb_objects


def _process_pve_virtual_machine(
        _pve_api: ProxmoxAPI,
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _nb_device: any,
        _pve_tags: [str],
        _pve_virtual_machine: dict,
        _is_replicated: bool,
        _has_ha: bool,
        _pve_pool: Optional[str] = None,
) -> dict:
    """
    Sync one QEMU VM from Proxmox to NetBox.
    Uses QEMU guest agent for interface names and IPs when enabled and VM is running.
    Role from VM_ROLE env. Then syncs network interfaces and disks.
    """
    _pve_node_name = _nb_device.name.lower()
    pve_virtual_machine_config = _pve_api.nodes(_pve_node_name).qemu(_pve_virtual_machine['vmid']).config.get()
    agent_enabled = pve_virtual_machine_config.get('agent', '0') == '1' or \
                   (isinstance(pve_virtual_machine_config.get('agent'), str) and
                    pve_virtual_machine_config.get('agent').startswith('1'))
    vm_is_running = _pve_virtual_machine['status'] == 'running'
    agent_data_by_mac = {}

    if agent_enabled and vm_is_running:
        logger.debug('      QEMU guest agent enabled, fetching network data...')
        try:
            pve_virtual_machine_agent_interfaces = _pve_api \
                .nodes(_pve_node_name) \
                .qemu(_pve_virtual_machine['vmid']) \
                .agent('network-get-interfaces') \
                .get()
            for iface in pve_virtual_machine_agent_interfaces.get('result', []):
                if iface.get('name') == 'lo':
                    continue
                mac_address = iface.get('hardware-address', '').lower()
                if not mac_address:
                    continue
                ip_addresses = []
                for ip_info in iface.get('ip-addresses', []):
                    ip_type = ip_info.get('ip-address-type', '')
                    ip_addr = ip_info.get('ip-address')
                    prefix = ip_info.get('prefix')
                    
                    if ip_addr and prefix is not None:
                        ip_addresses.append({
                            'address': ip_addr,
                            'prefix': prefix,
                            'type': ip_type,
                        })
                
                agent_data_by_mac[mac_address] = {
                    'interface_name': iface.get('name'),
                    'ip_addresses': ip_addresses,
                }
                logger.debug(f'        Agent: {iface.get("name")} ({mac_address}) - {len(ip_addresses)} IP(s)')
                
        except (ResourceException, KeyError, AttributeError) as e:
            logger.warning(f'      Warning: Failed to get QEMU agent data: {e}')
            agent_data_by_mac = {}
    elif agent_enabled and not vm_is_running:
        logger.debug('      QEMU guest agent enabled but VM is not running, skipping agent data')
    else:
        logger.debug('      QEMU guest agent not enabled, skipping agent data')
    if agent_enabled and vm_is_running and agent_data_by_mac:
        logger.debug(f'      Total agent interfaces found: {len(agent_data_by_mac)} (will match with Proxmox config by MAC)')
    optional_fields = _optional_vm_field_values(
        _nb_api, _nb_objects, pve_virtual_machine_config, _pve_pool
    )
    vm_role_id = _get_role_id(_nb_objects, _cfg().vm_role)
    vm_name = _pve_virtual_machine['name']
    is_locked = _is_pve_entity_transiently_locked(_pve_virtual_machine)
    nb_virtual_machine = _get_nb_vm_for_sync(_nb_api, _nb_objects, _pve_virtual_machine['vmid'], vm_name)
    if nb_virtual_machine is None:
        create_params = {
            'serial': _pve_virtual_machine['vmid'],
            'name': vm_name,
            'site': _nb_device.site.id,
            'cluster': _cfg().nb_cluster_id,
            'device': _nb_device.id,
            'vcpus': _get_virtual_machine_vcpus(pve_virtual_machine_config),
            'memory': int(pve_virtual_machine_config['memory']),
            # While locked (e.g. backup) PVE status can flip; fall back to 'offline' for
            # creation so we don't persist a transient 'active'.
            'status': 'offline' if is_locked
                      else ('active' if _pve_virtual_machine['status'] == 'running' else 'offline'),
            'tags': list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags)),
            'custom_fields': {
                'autostart': pve_virtual_machine_config.get('onboot') == 1,
                'replicated': _is_replicated,
                'ha': _has_ha,
            }
        }
        if vm_role_id:
            create_params['role'] = vm_role_id
        create_params.update(optional_fields)

        nb_virtual_machine = _nb_api.virtualization.virtual_machines.create(**create_params)
        _index_nb_virtual_machine(_nb_objects, nb_virtual_machine)
    else:
        nb_virtual_machine.serial = _pve_virtual_machine['vmid']
        nb_virtual_machine.name = vm_name
        nb_virtual_machine.site = _nb_device.site.id
        nb_virtual_machine.cluster = _cfg().nb_cluster_id
        nb_virtual_machine.device = _nb_device.id
        nb_virtual_machine.vcpus = _get_virtual_machine_vcpus(pve_virtual_machine_config)
        nb_virtual_machine.memory = int(pve_virtual_machine_config['memory'])
        if is_locked:
            logger.debug(
                f'      VM {_pve_virtual_machine["vmid"]} is locked '
                f'({_pve_virtual_machine.get("lock")}); keeping existing NetBox status'
            )
        else:
            nb_virtual_machine.status = 'active' if _pve_virtual_machine['status'] == 'running' else 'offline'
        nb_virtual_machine.tags = list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags))
        if vm_role_id:
            nb_virtual_machine.role = vm_role_id
        nb_virtual_machine.custom_fields['autostart'] = pve_virtual_machine_config.get('onboot') == 1
        nb_virtual_machine.custom_fields['replicated'] = _is_replicated
        nb_virtual_machine.custom_fields['ha'] = _has_ha
        _apply_optional_vm_fields(nb_virtual_machine, optional_fields)
        nb_virtual_machine.save()
        _index_nb_virtual_machine(_nb_objects, nb_virtual_machine)
    _process_pve_virtual_machine_network_interfaces(
        _nb_api,
        _nb_objects,
        pve_virtual_machine_config,
        nb_virtual_machine,
        agent_data_by_mac,
    )
    _process_pve_virtual_machine_disks(
        _nb_api,
        _nb_objects,
        pve_virtual_machine_config,
        nb_virtual_machine,
    )

    return _nb_objects


def _process_pve_virtual_machine_network_interfaces(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _pve_virtual_machine_config: dict,
        _nb_virtual_machine: any,
        _agent_data_by_mac: dict,
) -> dict:
    """
    Sync VM network interfaces from Proxmox config (net0, net1, ...).
    Matches by MAC to guest agent data for interface name and IPs when available.
    """
    proxmox_interfaces_count = 0
    matched_interfaces_count = 0
    
    for (_config_key, _config_value) in _pve_virtual_machine_config.items():
        if not _config_key.startswith('net'):
            continue
        
        proxmox_interfaces_count += 1

        _network_definition = _parse_pve_network_definition(_config_value)
        network_mac_address = None
        for _model in ['virtio', 'e1000']:
            if _model in _network_definition:
                network_mac_address = _network_definition[_model]
                break

        if network_mac_address is None:
            logger.debug(f'      Interface {_config_key}: No MAC address found, skipping')
            continue
        agent_data = _agent_data_by_mac.get(network_mac_address.lower(), {})
        interface_name = agent_data.get('interface_name', _config_key)
        
        if agent_data:
            matched_interfaces_count += 1
            ip_count = len(agent_data.get('ip_addresses', []))
            logger.debug(f'      Interface {_config_key} ({network_mac_address}) → {interface_name}: matched with guest agent, {ip_count} IP(s)')
        else:
            logger.debug(f'      Interface {_config_key} ({network_mac_address}): no guest agent data, will sync without IPs')
        
        _process_pve_virtual_machine_network_interface(
            _nb_api,
            _nb_objects,
            _nb_virtual_machine,
            _config_key,
            interface_name,
            network_mac_address,
            _network_definition.get('tag'),
            _network_definition.get('mtu'),
            agent_data,
        )
    logger.info(f'      Synced {proxmox_interfaces_count} interface(s) from Proxmox config, {matched_interfaces_count} matched with guest agent')
    _resolve_primary_ip_assignments(
        _nb_objects,
        _nb_virtual_machine,
        _agent_data_by_mac,
    )
    return _nb_objects


def _resolve_primary_ip_assignments(
        _nb_objects: dict,
        _nb_virtual_machine: any,
        _agent_data_by_mac: dict,
) -> None:
    """
    Set ``primary_ip4``/``primary_ip6`` on a VM based on configured
    ``PRIMARY_SUBNETS``. If no subnets are configured, do nothing — current
    NetBox values (manual or already set) are left untouched.

    Subnet order defines priority: for each subnet (in order) the first
    matching IP from QEMU guest-agent data assigned in NetBox is picked.
    Within a subnet candidate IPs are sorted, so the result is deterministic
    regardless of the order Proxmox/agent returns interfaces.
    """
    primary_subnets = _cfg().primary_subnets
    if not primary_subnets:
        return

    candidates_v4 = []
    candidates_v6 = []
    for agent_data in _agent_data_by_mac.values():
        for ip_info in agent_data.get('ip_addresses', []):
            addr = ip_info.get('address')
            prefix = ip_info.get('prefix')
            if not addr or prefix is None:
                continue
            full_address = f'{addr}/{prefix}'
            nb_ip = _nb_objects['ip_addresses'].get(full_address)
            if nb_ip is None:
                continue
            try:
                ip_obj = ipaddress.ip_interface(full_address).ip
            except ValueError:
                continue
            entry = (ip_obj, nb_ip.id, full_address)
            if ip_obj.version == 4:
                candidates_v4.append(entry)
            else:
                candidates_v6.append(entry)

    candidates_v4.sort(key=lambda item: item[0])
    candidates_v6.sort(key=lambda item: item[0])

    chosen_v4 = None
    chosen_v6 = None
    for subnet in primary_subnets:
        if subnet.version == 4 and chosen_v4 is None:
            for ip_obj, nb_ip_id, full_address in candidates_v4:
                if ip_obj in subnet:
                    chosen_v4 = (nb_ip_id, full_address, subnet)
                    break
        elif subnet.version == 6 and chosen_v6 is None:
            for ip_obj, nb_ip_id, full_address in candidates_v6:
                if ip_obj in subnet:
                    chosen_v6 = (nb_ip_id, full_address, subnet)
                    break
        if chosen_v4 is not None and chosen_v6 is not None:
            break

    needs_save = False
    if chosen_v4 is not None:
        current = getattr(_nb_virtual_machine, 'primary_ip4', None)
        current_id = current.id if current is not None else None
        if current_id != chosen_v4[0]:
            _nb_virtual_machine.primary_ip4 = chosen_v4[0]
            needs_save = True
            logger.info(
                f'        ✓ Set primary IPv4: {chosen_v4[1]} (matched {chosen_v4[2]})'
            )
    if chosen_v6 is not None:
        current = getattr(_nb_virtual_machine, 'primary_ip6', None)
        current_id = current.id if current is not None else None
        if current_id != chosen_v6[0]:
            _nb_virtual_machine.primary_ip6 = chosen_v6[0]
            needs_save = True
            logger.info(
                f'        ✓ Set primary IPv6: {chosen_v6[1]} (matched {chosen_v6[2]})'
            )
    if needs_save:
        _nb_virtual_machine.save()


def _process_pve_virtual_machine_network_interface(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _nb_virtual_machine: any,
        _interface_key: str,
        _interface_name: str,
        _interface_mac_address: str,
        _interface_vlan_id: Optional[int],
        _interface_mtu: Optional[str],
        _agent_interface_data: dict,
) -> dict:
    """
    Create or update one VM interface and its MAC/IP from Proxmox and optional agent data.
    Lookup: by config key (net0, net1), then by interface name (eth0, ens18), then by MAC.
    """
    nb_virtual_machines_interface = _nb_objects['virtual_machines_interfaces'] \
        .get(_nb_virtual_machine.id, {}) \
        .get(_interface_key)
    if nb_virtual_machines_interface is None:
        nb_virtual_machines_interface = _nb_objects['virtual_machines_interfaces'] \
            .get(_nb_virtual_machine.id, {}) \
            .get(_interface_name)
    if nb_virtual_machines_interface is None:
            nb_mac_address = _nb_objects['mac_addresses'].get(_interface_mac_address)
            if nb_mac_address and hasattr(nb_mac_address, 'assigned_object_id'):
                try:
                    potential_interface = _nb_api.virtualization.interfaces.get(nb_mac_address.assigned_object_id)
                    if potential_interface and potential_interface.virtual_machine.id == _nb_virtual_machine.id:
                        nb_virtual_machines_interface = potential_interface
                        logger.debug(f'      Found existing interface by MAC {_interface_mac_address}: {potential_interface.name} -> updating to {_interface_name}')
                except Exception:
                    pass

    mtu_value = int(_interface_mtu) if _interface_mtu else None

    if nb_virtual_machines_interface is None:
        create_params = {
            'virtual_machine': _nb_virtual_machine.id,
            'name': _interface_name,
            'description': '',
        }
        if mtu_value:
            create_params['mtu'] = mtu_value
        
        nb_virtual_machines_interface = _nb_api.virtualization.interfaces.create(**create_params)

        if _nb_virtual_machine.id not in _nb_objects['virtual_machines_interfaces']:
            _nb_objects['virtual_machines_interfaces'][_nb_virtual_machine.id] = {}
        _nb_objects['virtual_machines_interfaces'][_nb_virtual_machine.id][_interface_key] = nb_virtual_machines_interface
        _nb_objects['virtual_machines_interfaces'][_nb_virtual_machine.id][_interface_name] = nb_virtual_machines_interface
    else:
        updated = False
        if nb_virtual_machines_interface.name != _interface_name:
            nb_virtual_machines_interface.name = _interface_name
            updated = True
        if mtu_value and nb_virtual_machines_interface.mtu != mtu_value:
            nb_virtual_machines_interface.mtu = mtu_value
            updated = True
        if updated:
            nb_virtual_machines_interface.save()
        if _nb_virtual_machine.id not in _nb_objects['virtual_machines_interfaces']:
            _nb_objects['virtual_machines_interfaces'][_nb_virtual_machine.id] = {}
        
        _nb_objects['virtual_machines_interfaces'][_nb_virtual_machine.id][_interface_key] = nb_virtual_machines_interface
        _nb_objects['virtual_machines_interfaces'][_nb_virtual_machine.id][_interface_name] = nb_virtual_machines_interface

    nb_mac_address = _nb_objects['mac_addresses'].get(_interface_mac_address)
    if nb_mac_address is None:
        nb_mac_address = _nb_api.dcim.mac_addresses.create(
            mac_address=_interface_mac_address,
            assigned_object_type='virtualization.vminterface',
            assigned_object_id=nb_virtual_machines_interface.id,
        )
        _nb_objects['mac_addresses'][_interface_mac_address] = nb_mac_address
        nb_virtual_machines_interface.primary_mac_address = nb_mac_address.id
        nb_virtual_machines_interface.save()
    else:
        if nb_mac_address.assigned_object_id != nb_virtual_machines_interface.id:
            try:
                old_interface = _nb_api.virtualization.interfaces.get(nb_mac_address.assigned_object_id)
                if old_interface and old_interface.virtual_machine.id != _nb_virtual_machine.id:
                    old_vm = old_interface.virtual_machine
                    old_vm_status = old_vm.status.value if hasattr(old_vm, 'status') and hasattr(old_vm.status, 'value') else (old_vm.status if hasattr(old_vm, 'status') else 'unknown')
                    current_vm_status = _nb_virtual_machine.status.value if hasattr(_nb_virtual_machine, 'status') and hasattr(_nb_virtual_machine.status, 'value') else (_nb_virtual_machine.status if hasattr(_nb_virtual_machine, 'status') else 'unknown')
                    if str(old_vm_status).lower() == 'offline':
                        logger.info(f'      MAC {_interface_mac_address} is used by offline VM {old_vm.name} (ID: {old_vm.serial})')
                        logger.info(f'      Safely re-assigning MAC to VM {_nb_virtual_machine.name} (ID: {_nb_virtual_machine.serial})')
                        if hasattr(old_interface, 'primary_mac_address') and old_interface.primary_mac_address:
                            if old_interface.primary_mac_address.id == nb_mac_address.id:
                                old_interface.primary_mac_address = None
                                old_interface.save()
                                logger.debug(f'      Removed primary MAC from old interface {old_interface.name}')
                        nb_mac_address.assigned_object_type = 'virtualization.vminterface'
                        nb_mac_address.assigned_object_id = nb_virtual_machines_interface.id
                        nb_mac_address.save()
                        nb_virtual_machines_interface.primary_mac_address = nb_mac_address.id
                        nb_virtual_machines_interface.save()
                        logger.info(f'      Successfully re-assigned MAC to interface {nb_virtual_machines_interface.name}')
                    else:
                        logger.error('      ❌ ERROR: MAC address conflict detected!')
                        logger.error(f'      MAC {_interface_mac_address} is used by:')
                        logger.error(f'         - VM {old_vm.name} (ID: {old_vm.serial}, status: {old_vm_status})')
                        logger.error(f'         - VM {_nb_virtual_machine.name} (ID: {_nb_virtual_machine.serial}, status: {current_vm_status})')
                        logger.error(f'      ⚠️  ACTION REQUIRED: Change MAC address in Proxmox for one of the VMs!')
                        logger.error(f'      Skipping MAC address assignment for VM {_nb_virtual_machine.name}')
                        return _nb_objects
                else:
                    nb_mac_address.assigned_object_type = 'virtualization.vminterface'
                    nb_mac_address.assigned_object_id = nb_virtual_machines_interface.id
                    nb_mac_address.save()
                    logger.info(f'      Re-assigned MAC {_interface_mac_address} to interface {nb_virtual_machines_interface.name}')
            except Exception as e:
                logger.warning(f'      Warning: Could not verify old interface for MAC {_interface_mac_address}: {e}')
                logger.warning(f'      Attempting to re-assign MAC anyway...')
                try:
                    nb_mac_address.assigned_object_type = 'virtualization.vminterface'
                    nb_mac_address.assigned_object_id = nb_virtual_machines_interface.id
                    nb_mac_address.save()
                    logger.info(f'      Re-assigned MAC {_interface_mac_address} to interface {nb_virtual_machines_interface.name}')
                except Exception as e2:
                    logger.error(f'      ❌ ERROR: Failed to re-assign MAC: {e2}')
                    logger.error(f'      Skipping MAC address assignment for this interface')
                    return _nb_objects
        if not hasattr(nb_virtual_machines_interface, 'primary_mac_address') or \
           nb_virtual_machines_interface.primary_mac_address is None or \
           (hasattr(nb_virtual_machines_interface.primary_mac_address, 'id') and 
            nb_virtual_machines_interface.primary_mac_address.id != nb_mac_address.id):
            nb_virtual_machines_interface.primary_mac_address = nb_mac_address.id
            nb_virtual_machines_interface.save()
    agent_ip_addresses = _agent_interface_data.get('ip_addresses', [])
    has_agent_match = bool(_agent_interface_data)

    if has_agent_match:
        desired_interface_ips = set()
        for ip_info in agent_ip_addresses:
            ip_addr = ip_info.get('address')
            ip_prefix = ip_info.get('prefix')
            if ip_addr and ip_prefix is not None:
                desired_interface_ips.add(f'{ip_addr}/{ip_prefix}')

        # Keep NetBox interface IPs aligned with guest agent state.
        for nb_ip in list(_nb_objects['ip_addresses'].values()):
            assigned_type = getattr(nb_ip, 'assigned_object_type', None)
            assigned_id = getattr(nb_ip, 'assigned_object_id', None)
            ip_address = str(getattr(nb_ip, 'address', ''))

            if assigned_type != 'virtualization.vminterface':
                continue
            if assigned_id != nb_virtual_machines_interface.id:
                continue
            if ip_address in desired_interface_ips:
                continue

            vm_needs_save = False
            if hasattr(_nb_virtual_machine, 'primary_ip4') and _nb_virtual_machine.primary_ip4:
                if _nb_virtual_machine.primary_ip4.id == nb_ip.id:
                    _nb_virtual_machine.primary_ip4 = None
                    vm_needs_save = True
            if hasattr(_nb_virtual_machine, 'primary_ip6') and _nb_virtual_machine.primary_ip6:
                if _nb_virtual_machine.primary_ip6.id == nb_ip.id:
                    _nb_virtual_machine.primary_ip6 = None
                    vm_needs_save = True
            if vm_needs_save:
                _nb_virtual_machine.save()

            removed_ip_address = ip_address
            nb_ip.delete()
            if removed_ip_address in _nb_objects['ip_addresses']:
                _nb_objects['ip_addresses'].pop(removed_ip_address, None)
            logger.info(f'        ✓ Removed stale IP {removed_ip_address} from interface {_interface_name}')

    if not agent_ip_addresses:
        logger.debug(f'        Interface {_interface_name}: no IP addresses from guest agent')
        return _nb_objects
    primary_ipv4 = None
    ipv4_count = 0
    ipv6_count = 0
    
    for ip_info in agent_ip_addresses:
        if ip_info.get('type') == 'ipv4':
            ipv4_count += 1
            if primary_ipv4 is None:
                primary_ipv4 = ip_info
        elif ip_info.get('type') == 'ipv6':
            ipv6_count += 1
    
    logger.debug(f'        Interface {_interface_name}: {ipv4_count} IPv4, {ipv6_count} IPv6 from guest agent')
    
    if primary_ipv4 is not None:
        _virtual_machine_address = primary_ipv4['address']
        _virtual_machine_address_mask = primary_ipv4['prefix']
        _virtual_machine_full_address = f'{_virtual_machine_address}/{_virtual_machine_address_mask}'
        # The containing network has to be computed, not assembled from text:
        # zeroing the last octet only yields a valid prefix for /24.
        _prefix_network_full_address = str(
            ipaddress.ip_interface(_virtual_machine_full_address).network
        )

        nb_prefix = _nb_objects['prefixes'].get(_prefix_network_full_address)
        if nb_prefix is None:
            nb_prefix = _nb_api.ipam.prefixes.create(prefix=_prefix_network_full_address)
            _nb_objects['prefixes'][nb_prefix.prefix] = nb_prefix

        if 'dns_name' in nb_prefix.custom_fields and nb_prefix.custom_fields['dns_name'] is not None:
            ip_address_dns_name = f'{_nb_virtual_machine.name}.{nb_prefix.custom_fields["dns_name"]}'
        else:
            ip_address_dns_name = ''

        nb_ip_address = _nb_objects['ip_addresses'].get(_virtual_machine_full_address)
        if nb_ip_address is None:
            nb_ip_address = _nb_api.ipam.ip_addresses.create(
                address=_virtual_machine_full_address,
                assigned_object_type='virtualization.vminterface',
                assigned_object_id=nb_virtual_machines_interface.id,
                dns_name=ip_address_dns_name
            )
            _nb_objects['ip_addresses'][nb_ip_address.address] = nb_ip_address
            logger.info(f'        ✓ Created IP {_virtual_machine_full_address} on interface {_interface_name}')
        else:
            if nb_ip_address.assigned_object_id != nb_virtual_machines_interface.id:
                try:
                    old_interface = _nb_api.virtualization.interfaces.get(nb_ip_address.assigned_object_id)
                    if old_interface and old_interface.virtual_machine.id != _nb_virtual_machine.id:
                        old_vm = old_interface.virtual_machine
                        old_vm_status = old_vm.status.value if hasattr(old_vm, 'status') and hasattr(old_vm.status, 'value') else (old_vm.status if hasattr(old_vm, 'status') else 'unknown')
                        current_vm_status = _nb_virtual_machine.status.value if hasattr(_nb_virtual_machine, 'status') and hasattr(_nb_virtual_machine.status, 'value') else (_nb_virtual_machine.status if hasattr(_nb_virtual_machine, 'status') else 'unknown')
                        old_ip_vrf = nb_ip_address.vrf.id if hasattr(nb_ip_address, 'vrf') and nb_ip_address.vrf else None
                        new_ip_vrf = nb_prefix.vrf.id if hasattr(nb_prefix, 'vrf') and nb_prefix.vrf else None
                        if old_ip_vrf != new_ip_vrf:
                            old_vrf_name = nb_ip_address.vrf.name if old_ip_vrf else 'Global'
                            new_vrf_name = nb_prefix.vrf.name if new_ip_vrf else 'Global'
                            logger.info(f'      IP {_virtual_machine_full_address} exists in different VRF:')
                            logger.info(f'         - Old: VRF "{old_vrf_name}" (VM {old_vm.name})')
                            logger.info(f'         - New: VRF "{new_vrf_name}" (VM {_nb_virtual_machine.name})')
                            logger.info(f'      Creating new IP address in VRF "{new_vrf_name}"')
                            nb_ip_address = _nb_api.ipam.ip_addresses.create(
                                address=_virtual_machine_full_address,
                                assigned_object_type='virtualization.vminterface',
                                assigned_object_id=nb_virtual_machines_interface.id,
                                dns_name=ip_address_dns_name,
                                vrf=new_ip_vrf
                            )
                            _nb_objects['ip_addresses'][nb_ip_address.address] = nb_ip_address
                        elif str(old_vm_status).lower() == 'offline':
                            logger.info(f'      IP {_virtual_machine_full_address} is used by offline VM {old_vm.name} (ID: {old_vm.serial})')
                            logger.info(f'      Safely re-assigning IP to VM {_nb_virtual_machine.name} (ID: {_nb_virtual_machine.serial})')
                            try:
                                old_vm_full = _nb_api.virtualization.virtual_machines.get(old_vm.id)
                                if old_vm_full:
                                    needs_save = False
                                    if hasattr(old_vm_full, 'primary_ip4') and old_vm_full.primary_ip4:
                                        if old_vm_full.primary_ip4.id == nb_ip_address.id:
                                            old_vm_full.primary_ip4 = None
                                            needs_save = True
                                            logger.debug(f'      Removed primary IPv4 from old VM {old_vm.name}')
                                    if hasattr(old_vm_full, 'primary_ip6') and old_vm_full.primary_ip6:
                                        if old_vm_full.primary_ip6.id == nb_ip_address.id:
                                            old_vm_full.primary_ip6 = None
                                            needs_save = True
                                            logger.debug(f'      Removed primary IPv6 from old VM {old_vm.name}')
                                    if needs_save:
                                        old_vm_full.save()
                            except Exception as e:
                                logger.warning(f'      Warning: Could not remove primary IP from old VM: {e}')
                            nb_ip_address.assigned_object_type = 'virtualization.vminterface'
                            nb_ip_address.assigned_object_id = nb_virtual_machines_interface.id
                            nb_ip_address.dns_name = ip_address_dns_name
                            nb_ip_address.save()
                            logger.info(f'      Successfully re-assigned IP to interface {nb_virtual_machines_interface.name}')
                        else:
                            logger.error('      ❌ ERROR: IP address conflict detected!')
                            logger.error(f'      IP {_virtual_machine_full_address} is used by:')
                            logger.error(f'         - VM {old_vm.name} (ID: {old_vm.serial}, status: {old_vm_status}, interface: {old_interface.name})')
                            logger.error(f'         - VM {_nb_virtual_machine.name} (ID: {_nb_virtual_machine.serial}, status: {current_vm_status})')
                            logger.error(f'      ⚠️  ACTION REQUIRED: Change IP address for one of the VMs!')
                            logger.error(f'      Skipping IP address assignment for VM {_nb_virtual_machine.name}')
                            return _nb_objects
                    else:
                        nb_ip_address.assigned_object_type = 'virtualization.vminterface'
                        nb_ip_address.assigned_object_id = nb_virtual_machines_interface.id
                        nb_ip_address.dns_name = ip_address_dns_name
                        nb_ip_address.save()
                except Exception as e:
                    logger.warning(f'      Warning: Could not verify old interface for IP {_virtual_machine_full_address}: {e}')
                    logger.warning(f'      Attempting to re-assign IP anyway...')
                    try:
                        nb_ip_address.assigned_object_type = 'virtualization.vminterface'
                        nb_ip_address.assigned_object_id = nb_virtual_machines_interface.id
                        nb_ip_address.dns_name = ip_address_dns_name
                        nb_ip_address.save()
                    except Exception as e2:
                        logger.error(f'      ❌ ERROR: Failed to re-assign IP: {e2}')
                        logger.error(f'      Skipping IP address assignment for this interface')
                        return _nb_objects
            else:
                nb_ip_address.dns_name = ip_address_dns_name
                nb_ip_address.save()
                logger.debug(f'        ✓ Updated IP {_virtual_machine_full_address} on interface {_interface_name}')
    else:
        # The VLAN is attached to the prefix derived from the IPv4 address, so
        # without one there is nothing to attach it to. This branch used to
        # reference an unassigned ``nb_prefix`` and raise NameError.
        logger.debug(f'        Interface {_interface_name}: no IPv4 address reported, '
                     f'nothing to attach VLAN {_interface_vlan_id} to')

    return _nb_objects


def _process_pve_virtual_machine_disks(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _pve_virtual_machine_config: dict,
        _nb_virtual_machine: any,
) -> dict:
    """
    Sync VM disks from Proxmox config (scsi, ide, sata, virtio, efidisk).
    Skips non-disk keys (e.g. scsihw, tpm). CD-ROM and entries without size are skipped.
    """
    disk_prefixes = ('scsi', 'ide', 'sata', 'virtio', 'efidisk')
    skip_keys = ('scsihw', 'ide2', 'tpmstate0', 'tpm')
    processed_disk_names = set()
    for (_config_key, _config_value) in _pve_virtual_machine_config.items():
        if _config_key in skip_keys or _config_key.startswith('tpm'):
            continue
        if not any(_config_key.startswith(prefix) for prefix in disk_prefixes):
            continue
        _disk_definition = _parse_pve_disk_definition(_config_value)
        if 'size' not in _disk_definition or 'name' not in _disk_definition:
            continue
        disk_size = _process_pve_disk_size(_disk_definition['size'])
        if disk_size < 0:
            logger.warning(f'      Warning: Skipping disk {_config_key} - unknown size format: {_disk_definition["size"]}')
            continue

        _process_pve_virtual_machine_disk(
            _nb_api,
            _nb_objects,
            _nb_virtual_machine,
            _disk_definition['name'],
            disk_size,
            _disk_definition.get('backup', '1') == '1',
        )
        processed_disk_names.add(_disk_definition['name'])

    _remove_stale_virtual_machine_disks(_nb_objects, _nb_virtual_machine, processed_disk_names)

    return _nb_objects


def _process_pve_virtual_machine_disk(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _nb_virtual_machine: any,
        _disk_name: str,
        _disk_size: int,
        _has_backup: bool,
) -> dict:
    """Create or update one virtual disk in NetBox (size and backup custom field)."""
    nb_disk = _nb_objects['disks'].get(_nb_virtual_machine.id, {}).get(_disk_name)
    if nb_disk is None:
        new_disk = _nb_api.virtualization.virtual_disks.create(
            name=_disk_name,
            size=_disk_size,
            virtual_machine=_nb_virtual_machine.id,
            custom_fields={
                'backup': _has_backup,
            }
        )
        if _nb_virtual_machine.id not in _nb_objects['disks']:
            _nb_objects['disks'][_nb_virtual_machine.id] = {}
        _nb_objects['disks'][_nb_virtual_machine.id][_disk_name] = new_disk
    else:
        nb_disk.size = _disk_size
        nb_disk.custom_fields['backup'] = _has_backup
        nb_disk.save()

    return _nb_objects


def _remove_stale_virtual_machine_disks(
        _nb_objects: dict,
        _nb_virtual_machine: any,
        _processed_disk_names: set,
) -> None:
    """Delete NetBox virtual disks for this VM that are no longer present in Proxmox config."""
    existing_disks = _nb_objects['disks'].get(_nb_virtual_machine.id, {})
    stale_disk_names = [
        disk_name for disk_name in existing_disks
        if disk_name not in _processed_disk_names
    ]
    for disk_name in stale_disk_names:
        nb_disk = existing_disks[disk_name]
        try:
            nb_disk.delete()
            logger.info(f'      Deleted stale disk from NetBox: {disk_name} (VM id {_nb_virtual_machine.id})')
        except Exception as e:
            logger.error(f'      Failed to delete stale disk {disk_name} for VM id {_nb_virtual_machine.id}: {e}')
            continue
        del existing_disks[disk_name]


def _process_pve_lxc_network_interfaces(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _pve_container_config: dict,
        _nb_virtual_machine: any,
        _agent_data_by_mac: dict,
) -> dict:
    """
    Sync LXC container network interfaces (net0, net1, ...).

    Uses hwaddr for MAC and ``name=`` for the interface name. IP addresses come
    from ``_agent_data_by_mac``, matched by MAC exactly as for QEMU, so
    ``PRIMARY_SUBNETS`` applies to containers as well.
    """
    matched_interfaces_count = 0

    for (_config_key, _config_value) in _pve_container_config.items():
        if not _config_key.startswith('net'):
            continue
        _network_definition = _parse_pve_network_definition(_config_value)
        network_mac_address = _network_definition.get('hwaddr')
        if network_mac_address is None:
            continue
        agent_data = _agent_data_by_mac.get(network_mac_address.lower(), {})
        # The container config names the interface; the runtime endpoint only
        # confirms it, so config wins and the agent name is a fallback.
        interface_name = _network_definition.get(
            'name', agent_data.get('interface_name', _config_key)
        )
        if agent_data:
            matched_interfaces_count += 1
            logger.debug(
                f'      Interface {_config_key} ({network_mac_address}) → {interface_name}: '
                f'{len(agent_data.get("ip_addresses", []))} IP(s)'
            )
        _process_pve_virtual_machine_network_interface(
            _nb_api,
            _nb_objects,
            _nb_virtual_machine,
            _config_key,
            interface_name,
            network_mac_address,
            _network_definition.get('tag'),  # VLAN tag
            _network_definition.get('mtu'),  # MTU
            agent_data,
        )

    if _agent_data_by_mac:
        logger.info(f'      Matched {matched_interfaces_count} interface(s) with LXC IP data')

    _resolve_primary_ip_assignments(
        _nb_objects,
        _nb_virtual_machine,
        _agent_data_by_mac,
    )

    return _nb_objects


def _process_pve_lxc_disks(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _pve_container_config: dict,
        _nb_virtual_machine: any,
) -> dict:
    """Sync LXC disks: rootfs (root) and mp0, mp1, ... (mount points)."""
    processed_disk_names = set()
    if 'rootfs' in _pve_container_config:
        _disk_definition = _parse_pve_disk_definition(_pve_container_config['rootfs'])
        if 'size' in _disk_definition and 'name' in _disk_definition:
            disk_size = _process_pve_disk_size(_disk_definition['size'])
            if disk_size >= 0:
                _process_pve_virtual_machine_disk(
                    _nb_api,
                    _nb_objects,
                    _nb_virtual_machine,
                    _disk_definition['name'],
                    disk_size,
                    _disk_definition.get('backup', '1') == '1',
                )
                processed_disk_names.add(_disk_definition['name'])
    for (_config_key, _config_value) in _pve_container_config.items():
        if not _config_key.startswith('mp'):
            continue

        _disk_definition = _parse_pve_disk_definition(_config_value)
        if 'size' in _disk_definition and 'name' in _disk_definition:
            disk_size = _process_pve_disk_size(_disk_definition['size'])
            if disk_size >= 0:
                _process_pve_virtual_machine_disk(
                    _nb_api,
                    _nb_objects,
                    _nb_virtual_machine,
                    _disk_definition['name'],
                    disk_size,
                    _disk_definition.get('backup', '1') == '1',
                )
                processed_disk_names.add(_disk_definition['name'])

    _remove_stale_virtual_machine_disks(_nb_objects, _nb_virtual_machine, processed_disk_names)

    return _nb_objects


def _parse_pve_network_definition(_raw_network_definition: str) -> dict:
    """Parse Proxmox network config string (e.g. virtio=MAC,bridge=vmbr0,tag=100) into key=value dict."""
    _network_definition = {}
    for _component in _raw_network_definition.split(','):
        _component_parts = _component.split('=')
        if len(_component_parts) == 2:
            _network_definition[_component_parts[0]] = _component_parts[1]
    return _network_definition


def _parse_pve_disk_definition(_raw_disk_definition: str) -> dict:
    """Parse Proxmox disk config string (e.g. local-lvm:vm-100-disk-0,size=32G) into key=value dict."""
    _disk_definition = {}
    for _component in _raw_disk_definition.split(','):
        _component_parts = _component.split('=')
        if len(_component_parts) == 1:
            _disk_definition['name'] = _component_parts[0]
        else:
            _disk_definition[_component_parts[0]] = _component_parts[1]

    return _disk_definition


def _process_pve_disk_size(_raw_disk_size: str) -> int:
    """
    Parse Proxmox disk size string ('32G', '1024M', '528K', '2T') to megabytes.
    Returns -1 on parse failure. Kilobytes are rounded up to at least 1 MB (NetBox minimum).
    """
    if not _raw_disk_size or len(_raw_disk_size) < 2:
        return -1
    try:
        size = _raw_disk_size[:-1]
        size_unit = _raw_disk_size[-1]
        if size_unit == 'K':
            return max(1, int(float(size) / 1024))
        if size_unit == 'M':
            return int(size)
        if size_unit == 'G':
            return int(size) * 1_000
        if size_unit == 'T':
            return int(size) * 1_000_000
    except (ValueError, IndexError):
        return -1

    return -1


def _get_virtual_machine_vcpus(_pve_virtual_machine_config: dict) -> int:
    """Return vCPU count from Proxmox VM config (vcpus if set, else cores * sockets)."""
    if 'vcpus' in _pve_virtual_machine_config:
        return _pve_virtual_machine_config['vcpus']
    return _pve_virtual_machine_config['cores'] * _pve_virtual_machine_config['sockets']


def _select_pve_nodes(
        _pve_api: ProxmoxAPI,
        _decisions: FilterDecisions,
        _quiet: bool = False,
) -> List[dict]:
    """
    Return the Proxmox nodes taking part in this sync.

    Excluded nodes are dropped before any per-node call is made for them.
    ``_quiet`` drops the exclusions to DEBUG — the quick sync runs every few
    seconds and an unchanging list is not worth a line each time.
    """
    log = logger.debug if _quiet else logger.info
    nodes = []
    for pve_node in _pve_api.nodes.get():
        reason = _decisions.filters.node_reason(pve_node['node'])
        if reason is not None:
            log(f'  Skipping node {pve_node["node"]}: {reason}')
            continue
        nodes.append(pve_node)
    return nodes


def _read_pve_pool_members(_pve_api: ProxmoxAPI) -> Optional[Dict[int, str]]:
    """
    Map VMID to pool name by reading ``/pools``, or ``None`` when that fails.

    The second source of pool membership, used when ``/cluster/resources`` is
    out of reach. It is a separate permission and a separate endpoint —
    :func:`_process_pve_tags` already reads it — so a token that cannot see the
    cluster-wide resource list can still very well see the pools.

    Partial answers are refused: one unreadable pool would make its guests look
    pool-less, and a guest that looks pool-less loses its ``Pool/*`` tag.
    """
    try:
        pve_pools = _pve_api.pools.get()
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(f'/pools is not available either ({e})')
        return None

    members: Dict[int, str] = {}
    for pve_pool in pve_pools:
        poolid = pve_pool.get('poolid')
        if not poolid:
            continue
        try:
            detail = _pve_api.pools(poolid).get()
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f'Failed to read the members of Proxmox pool "{poolid}" ({e})')
            return None
        # PVE 7 answers GET /pools/<id> with one object; PVE 8 with a list of one.
        for entry in (detail if isinstance(detail, list) else [detail]):
            for member in entry.get('members') or []:
                vmid = member.get('vmid')
                if vmid is not None:
                    members[int(vmid)] = poolid
    return members


def _read_pve_guests(_pve_api: ProxmoxAPI) -> Tuple[List[Tuple[Guest, dict]], bool]:
    """
    Read every guest in the cluster with its raw entry; returns ``(guests, pools_known)``.

    ``/cluster/resources`` is the only endpoint that answers this in one request
    and the only one that sees guests on nodes this sync skips — which is what
    protects excluded guests from cleanup. The fallback walks every node,
    filtered-out ones included, so that protection survives, and rebuilds pool
    membership from ``/pools``: the sync assigns tags wholesale, so a pass that
    cannot see pools would strip ``Pool/*`` off every guest it touches.
    """
    try:
        resources = _pve_api.cluster.resources.get(type='vm')
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(
            f'/cluster/resources is not available ({e}); falling back to per-node listings.'
        )
        pool_members = _read_pve_pool_members(_pve_api)
        if pool_members is None:
            return _read_pve_guests_from_nodes(_pve_api, {}), False
        logger.info(f'Pool membership rebuilt from /pools for {len(pool_members)} guest(s)')
        return _read_pve_guests_from_nodes(_pve_api, pool_members), True

    return [(Guest.from_cluster_resource(r), r) for r in resources], True


def _read_pve_guests_from_nodes(
        _pve_api: ProxmoxAPI,
        _pool_members: Dict[int, str],
) -> List[Tuple[Guest, dict]]:
    """
    Read every guest by walking all nodes — the fallback for :func:`_read_pve_guests`.

    Ignores the node filters on purpose: a guest on a skipped node still has to
    be evaluated so cleanup knows not to delete it. Pools come from
    ``_pool_members``, since a per-node listing does not carry them.
    """
    guests: List[Tuple[Guest, dict]] = []
    for pve_node in _pve_api.nodes.get():
        node_name = pve_node['node']
        for kind in (QEMU, LXC):
            try:
                entries = getattr(_pve_api.nodes(node_name), kind).get()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f'Failed to list {kind} guests on node {node_name}: {e}')
                continue
            for entry in entries:
                pool = _pool_members.get(int(entry['vmid']))
                guests.append((Guest.from_node_entry(entry, node_name, kind, pool), entry))
    return guests


def _collect_pve_guest_metadata(
        _pve_api: ProxmoxAPI,
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _decisions: FilterDecisions,
        _only_vmids: Optional[set] = None,
) -> Tuple[Dict[int, List[str]], Dict[int, str], set]:
    """
    Read every guest once and derive per-guest tags, pools and templates.

    The same pass feeds the filter decisions — this is the only view of the
    whole cluster, so its verdicts are what the per-node loops reuse and what
    protects excluded guests from cleanup. Every guest is evaluated even when
    ``_only_vmids`` narrows the returned metadata to a quick sync's targets.

    Returns (tags per vmid, pool per vmid, template vmids) for guests that pass
    the filters. Raises ``RuntimeError`` if ``SYNC_POOLS`` is set but pools
    could not be read: matching no guest at all would silently empty the sync.
    """
    config = _cfg()
    pve_vm_tags: Dict[int, List[str]] = {}
    pve_vm_pools: Dict[int, str] = {}
    pve_template_vmids: set = set()

    guests, pools_known = _read_pve_guests(_pve_api)
    if not pools_known:
        if config.sync_pools:
            raise RuntimeError(
                'SYNC_POOLS is set but neither /cluster/resources nor /pools can be read, so no '
                'guest can be matched to a pool. Grant the API token read permission on one of '
                'them, or unset SYNC_POOLS.'
            )
        logger.warning(
            'Pool membership is unreadable this pass; the guests synced now lose their '
            'Pool/* tag in NetBox until pools can be read again.'
        )
        if config.pool_as_tenant:
            logger.warning(
                'POOL_AS_TENANT is set but pools are unreadable on this path; '
                'tenants are left untouched this pass.'
            )

    for guest, pve_vm_resource in guests:
        if _decisions.is_excluded(guest):
            continue
        if _only_vmids is not None and guest.vmid not in _only_vmids:
            continue

        pve_vm_tags[guest.vmid] = []
        if guest.pool:
            pve_vm_pools[guest.vmid] = guest.pool
            pve_vm_tags[guest.vmid].append(f'Pool/{guest.pool}')

        if pve_vm_resource.get('template'):
            pve_template_vmids.add(guest.vmid)
            if config.template_policy == 'tag':
                _ensure_nb_tag(TEMPLATE_TAG_NAME, _nb_api, _nb_objects)
                pve_vm_tags[guest.vmid].append(TEMPLATE_TAG_NAME)

        if config.sync_tags:
            for _tag_name in guest.tags:
                _ensure_nb_tag(_tag_name, _nb_api, _nb_objects)
                pve_vm_tags[guest.vmid].append(_tag_name)

    return pve_vm_tags, pve_vm_pools, pve_template_vmids


def _skip_filtered_guest(
        _decisions: FilterDecisions,
        _entry: dict,
        _node_name: str,
        _kind: str,
        _pve_vm_pools: Dict[int, str],
) -> bool:
    """
    True when a guest from a per-node listing is excluded by the filters.

    Re-asks with the same VMID, so the verdict already reached cluster-wide is
    reused and only unseen guests are evaluated fresh.
    """
    guest = Guest.from_node_entry(
        _entry, _node_name, _kind, _pve_vm_pools.get(int(_entry['vmid'])))
    return _decisions.is_excluded(guest)


def quick_check_changes(
        _pve_api: ProxmoxAPI,
        _last_state: Dict,
        _source: str = QUICK_CHECK_SOURCE_CLUSTER,
) -> Tuple[List[int], Dict]:
    """
    Quick check for VM changes; returns ``(changed vmids, current state)``.

    Thin wrapper over :func:`pve2netbox.api.proxmox.quick_check_changes`, kept
    for the historical import path. Deliberately not a second implementation:
    the duplicate that used to live here ignored ``IGNORE_STATUS_WHEN_LOCKED``.
    """
    return _api_quick_check_changes(
        _pve_api, _last_state, _cfg(), source=_source, guest_filters=get_filters())


def _load_specific_objects(
        _nb_api: pynetbox.api,
        _changed_vmids: List[int],
        _node_names: Optional[List[str]] = None,
) -> Dict:
    """
    Load from NetBox only the objects related to the given VM IDs.

    The quick sync's lighter counterpart to :func:`_load_nb_objects`: VMs by
    serial in batches, their interfaces, IPs and disks per VM. Prefixes, VLANs,
    tags and roles are read whole — small, and needed for matching.

    A VM whose objects could not be read goes into ``incomplete_vmids``; an
    empty cache would read as "no interfaces yet" and duplicate every one.
    """
    config = _cfg()
    cluster_id = config.nb_cluster_id
    scoped = config.nb_preload_scope == 'cluster' and cluster_id is not None
    logger.info(f'Loading NetBox objects for {len(_changed_vmids)} VMs...')
    _nb_objects = _empty_nb_objects()

    logger.debug('  - Loading devices...')
    _load_nb_devices(_nb_api, _nb_objects, _node_names, scoped)

    logger.debug(f'  - Loading {len(_changed_vmids)} specific virtual machines...')
    for _nb_virtual_machine in _load_nb_vms_by_serial(_nb_api, _changed_vmids, cluster_id, scoped):
        _index_nb_virtual_machine(_nb_objects, _nb_virtual_machine)

    vm_ids = [vm.id for vm in _nb_objects['virtual_machines'].values()]

    unloaded: Set[int] = set()

    logger.debug('  - Loading interfaces for changed VMs...')
    interfaces, failed = _fetch_for_vms(
        _nb_api.virtualization.interfaces, 'interfaces', vm_ids)
    _index_nb_interfaces(_nb_objects, interfaces)
    unloaded |= failed

    logger.debug('  - Loading MAC addresses...')
    for vm_interfaces in _nb_objects['virtual_machines_interfaces'].values():
        for iface in vm_interfaces.values():
            if hasattr(iface, 'primary_mac_address') and iface.primary_mac_address:
                try:
                    mac = _nb_api.dcim.mac_addresses.get(iface.primary_mac_address.id)
                    if mac:
                        _nb_objects['mac_addresses'][mac.mac_address] = mac
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(f'Failed to load MAC for interface {iface.id}: {e}')

    logger.debug('  - Loading prefixes...')
    for _nb_prefix in _nb_api.ipam.prefixes.all():
        _nb_objects['prefixes'][_nb_prefix.prefix] = _nb_prefix

    logger.debug('  - Loading IP addresses for changed VMs...')
    for vm_id in vm_ids:
        try:
            for ip in _nb_api.ipam.ip_addresses.filter(virtual_machine_id=vm_id):
                _nb_objects['ip_addresses'][ip['address']] = ip
        except Exception as e:  # pylint: disable=broad-except
            unloaded.add(vm_id)
            logger.warning(f'Failed to load IPs for VM {vm_id}: {e}')

    logger.debug('  - Loading VLANs...')
    for _nb_vlan in _nb_api.ipam.vlans.all():
        _nb_objects['vlans'][str(_nb_vlan.vid)] = _nb_vlan

    logger.debug('  - Loading virtual disks for changed VMs...')
    disks, failed = _fetch_for_vms(
        _nb_api.virtualization.virtual_disks, 'virtual disks', vm_ids)
    _index_nb_disks(_nb_objects, disks)
    unloaded |= failed

    logger.debug('  - Loading tags...')
    for _nb_tag in _nb_api.extras.tags.all():
        _nb_objects['tags'][_nb_tag.name] = _nb_tag

    logger.debug('  - Loading device roles...')
    for _nb_role in _nb_api.dcim.device_roles.all():
        _nb_objects['roles'][_nb_role.name] = _nb_role
        _nb_objects['roles'][str(_nb_role.id)] = _nb_role

    _load_nb_platforms_and_tenants(_nb_api, _nb_objects)
    _mark_incomplete_preload(_nb_objects, unloaded)
    if _nb_objects['incomplete_vmids']:
        logger.warning(
            f'Incomplete NetBox preload for {len(_nb_objects["incomplete_vmids"])} VM(s); '
            f'they are skipped this pass to avoid creating duplicates'
        )

    logger.info('NetBox objects loaded.')
    return _nb_objects


def _load_nb_vms_by_serial(
        _nb_api: pynetbox.api,
        _vmids: List[int],
        _cluster_id: Optional[int],
        _scoped: bool,
) -> List[Any]:
    """
    Fetch the NetBox VMs carrying the given VMIDs in their ``serial`` field.

    Batched, with a per-VMID fallback, so a quick check costs a couple of
    requests rather than one per changed guest.

    The fallback cannot wait for NetBox to raise: a single-value ``serial``
    filter silently narrows a list to its last entry, and an unknown one may be
    ignored and answer with everything. So the answer is checked against what
    was asked for rather than trusted.
    """
    scope = {'cluster_id': _cluster_id} if _scoped else {}
    serials = [str(vmid) for vmid in _vmids]
    records: List[Any] = []
    for start in range(0, len(serials), NB_FILTER_CHUNK_SIZE):
        chunk = serials[start:start + NB_FILTER_CHUNK_SIZE]
        batched = None
        try:
            batched = list(_nb_api.virtualization.virtual_machines.filter(serial=chunk, **scope))
        except Exception as e:  # pylint: disable=broad-except
            logger.debug(f'Batched serial lookup rejected by NetBox ({e}); querying one by one')

        if batched is not None and _batched_serials_honoured(batched, chunk):
            records.extend(batched)
            continue

        if batched is not None:
            logger.debug(
                f'NetBox did not honour the batched serial filter for {len(chunk)} VM(s); '
                f'querying one by one'
            )
        for serial in chunk:
            try:
                records.extend(
                    _nb_api.virtualization.virtual_machines.filter(serial=serial, **scope))
            except Exception as inner:  # pylint: disable=broad-except
                logger.warning(f'Failed to load VM {serial}: {inner}')
    return records


def _batched_serials_honoured(_records: List[Any], _chunk: List[str]) -> bool:
    """
    True when a batched ``serial=`` query looks like NetBox actually applied it.

    Two answers say it did not: a record whose serial was never asked for (the
    filter was ignored), and a single serial coming back for a chunk of many
    (a single-value filter kept only the last one). The second test can fire
    when the chunk genuinely has one match; the cost of being wrong is one
    round of per-VMID queries, against silently syncing a VM whose interfaces
    were never loaded.
    """
    if len(_chunk) == 1:
        return True
    asked = set(_chunk)
    found = {str(getattr(record, 'serial', '')) for record in _records}
    if not found <= asked:
        return False
    return len(found) > 1


def sync_specific_vms(
        _pve_api: ProxmoxAPI,
        _nb_api: pynetbox.api,
        _changed_vmids: List[int],
) -> None:
    """
    Sync only the given VM IDs to NetBox (incremental quick sync).

    Loads only the needed NetBox objects, processes tags and HA, then syncs the
    guests per node. Selection filters are applied here as well as in the quick
    check, so a guest that became excluded between two cycles is dropped rather
    than synced one last time.
    """
    if not _changed_vmids:
        logger.info('No changes detected, skipping sync.')
        return
    logger.info(f'Quick sync: processing {len(_changed_vmids)} changed VMs...')

    decisions = FilterDecisions(get_filters())
    pve_nodes = _select_pve_nodes(_pve_api, decisions, _quiet=True)
    node_names = [pve_node['node'] for pve_node in pve_nodes]

    nb_objects = _load_specific_objects(_nb_api, _changed_vmids, node_names)
    _process_pve_tags(_pve_api, _nb_api, nb_objects)
    logger.info('Fetching VM metadata from Proxmox...')
    pve_vm_tags, pve_vm_pools, pve_template_vmids = _collect_pve_guest_metadata(
        _pve_api, _nb_api, nb_objects, decisions, set(_changed_vmids))
    skip_templates = _cfg().template_policy == 'skip'

    pve_ha_virtual_machine_ids = list(
        map(
            lambda r: int(r['sid'].split(':')[1]),
            filter(lambda r: r['type'] == 'service', _pve_api.cluster.ha.status.current.get())
        )
    )
    enabled_kinds = []
    if _cfg().sync_vms:
        enabled_kinds.append(QEMU)
    if _cfg().sync_lxc:
        enabled_kinds.append(LXC)

    sync_errors = 0
    vms_by_node = {}
    nodes_info = {}
    for pve_node in pve_nodes:
        node_name = pve_node['node']
        nodes_info[node_name] = pve_node
        vms_by_node[node_name] = {QEMU: [], LXC: []}
        for kind in enabled_kinds:
            # QEMU and LXC are both the PVE endpoint name and the resource type.
            for entry in getattr(_pve_api.nodes(node_name), kind).get():
                if entry['vmid'] not in _changed_vmids:
                    continue
                if _skip_filtered_guest(decisions, entry, node_name, kind, pve_vm_pools):
                    continue
                if _preload_incomplete(nb_objects, int(entry['vmid'])):
                    sync_errors += 1
                    logger.error(
                        f'    Skipping {entry.get("name", entry["vmid"])} '
                        f'(ID: {entry["vmid"]}): its NetBox objects failed to load, and '
                        f'syncing now would duplicate them'
                    )
                    continue
                vms_by_node[node_name][kind].append(entry)

    for node_name, vms in vms_by_node.items():
        if not vms[QEMU] and not vms[LXC]:
            continue
        logger.info(f'  Processing node: {node_name}')
        pve_replicated_virtual_machine_ids = list(
            map(lambda r: r['guest'], _pve_api.nodes(node_name).replication.get())
        )
        
        nb_device = nb_objects['devices'].get(node_name.lower())
        if nb_device is None:
            # NODE_MISSING_POLICY=fail is enforced by the full sync; a quick check
            # must never take the daemon down mid-cycle.
            sync_errors += 1
            logger.error(
                f'Node {node_name} has no matching device in NetBox, skipping its VMs.'
            )
            continue
        pve_node = nodes_info[node_name]
        if not _cfg().dry_run:
            nb_device.status = 'active' if pve_node['status'] == 'online' else 'offline'
            nb_device.save()
        for vm in vms[QEMU]:
            if shutdown.should_stop():
                logger.warning('Quick sync interrupted by shutdown request')
                return
            if skip_templates and vm['vmid'] in pve_template_vmids:
                logger.info(f'    Skipping template VM: {vm["name"]} (ID: {vm["vmid"]})')
                continue
            logger.info(f'    Quick sync VM: {vm["name"]} (ID: {vm["vmid"]})')
            try:
                _process_pve_virtual_machine(
                    _pve_api,
                    _nb_api,
                    nb_objects,
                    nb_device,
                    pve_vm_tags.get(vm['vmid'], []),
                    vm,
                    vm['vmid'] in pve_replicated_virtual_machine_ids,
                    vm['vmid'] in pve_ha_virtual_machine_ids,
                    pve_vm_pools.get(vm['vmid']),
                )
            except Exception as e:
                sync_errors += 1
                logger.error(
                    f'    Failed quick sync for VM {vm["name"]} (ID: {vm["vmid"]}): {e}',
                    exc_info=True,
                )
        for ct in vms[LXC]:
            if shutdown.should_stop():
                logger.warning('Quick sync interrupted by shutdown request')
                return
            if skip_templates and ct['vmid'] in pve_template_vmids:
                logger.info(f'    Skipping template LXC: {ct["name"]} (ID: {ct["vmid"]})')
                continue
            logger.info(f'    Quick sync LXC: {ct["name"]} (ID: {ct["vmid"]})')
            try:
                _process_pve_lxc_container(
                    _pve_api,
                    _nb_api,
                    nb_objects,
                    nb_device,
                    pve_vm_tags.get(ct['vmid'], []),
                    ct,
                    ct['vmid'] in pve_replicated_virtual_machine_ids,
                    ct['vmid'] in pve_ha_virtual_machine_ids,
                    pve_vm_pools.get(ct['vmid']),
                )
            except Exception as e:
                sync_errors += 1
                logger.error(
                    f'    Failed quick sync for LXC {ct["name"]} (ID: {ct["vmid"]}): {e}',
                    exc_info=True,
                )

    if sync_errors:
        logger.warning(f'Quick sync completed with {sync_errors} error(s).')
    else:
        logger.info('Quick sync completed successfully!')


def _nb_vm_in_cluster(_nb_vm: Any, _cluster_id: Optional[int]) -> bool:
    """
    True when this NetBox VM belongs to the cluster this sync is responsible for.

    A VM with no cluster counts as ours — it belongs to no other Proxmox
    cluster, and skipping it would strand it forever. With no cluster
    configured there is nothing to compare against, so everything is in scope.
    """
    if _cluster_id is None:
        return True
    vm_cluster_id = getattr(getattr(_nb_vm, 'cluster', None), 'id', None)
    if vm_cluster_id is None:
        return True
    return int(vm_cluster_id) == int(_cluster_id)


def cleanup_stale_vms(
        nb_api: pynetbox.api,
        nb_objects: dict,
        current_vmids: set,
        dry_run: bool = False,
        protected_vmids: Optional[set] = None,
) -> None:
    """
    Remove VMs from NetBox that no longer exist in Proxmox.

    Two kinds of VM are deliberately out of reach:

    - ``protected_vmids`` — filtered guests. They still exist in Proxmox, just
      unsynced, so they never reach ``current_vmids``; deleting them would mean
      that enabling ``EXCLUDE_TAGS`` wipes the records it was meant to spare.
    - VMs of another NetBox cluster, which two Proxmox clusters syncing into one
      NetBox would otherwise delete from each other. Checked here rather than
      left to ``NB_PRELOAD_SCOPE`` narrowing the cache, so that
      ``NB_PRELOAD_SCOPE=all`` cannot quietly bring the deletion back.
    """
    logger.info('Checking for stale VMs in NetBox...')

    cluster_id = _cfg().nb_cluster_id
    protected_vmids = protected_vmids or set()
    protected_seen = 0
    foreign_seen = 0
    stale_vms = []
    for serial, nb_vm in nb_objects['virtual_machines'].items():
        try:
            vmid = int(serial)
        except (ValueError, TypeError):
            continue
        if vmid in current_vmids:
            continue
        if vmid in protected_vmids:
            protected_seen += 1
            continue
        if not _nb_vm_in_cluster(nb_vm, cluster_id):
            foreign_seen += 1
            continue
        stale_vms.append((vmid, nb_vm))

    if protected_seen:
        logger.info(f'Cleanup skipped {protected_seen} filtered VM(s) still present in Proxmox')
    if foreign_seen:
        logger.info(f'Cleanup skipped {foreign_seen} VM(s) belonging to another NetBox cluster')
    
    if not stale_vms:
        logger.info('No stale VMs found.')
        return
    
    logger.warning(f'Found {len(stale_vms)} stale VM(s) that exist in NetBox but not in Proxmox:')
    for vmid, nb_vm in stale_vms:
        logger.warning(f'  - VM {nb_vm.name} (ID: {vmid})')
    
    if dry_run:
        logger.info('[DRY RUN] Would delete these VMs from NetBox')
        return
    
    for vmid, nb_vm in stale_vms:
        try:
            logger.info(f'Deleting stale VM: {nb_vm.name} (ID: {vmid})')
            nb_vm.delete()
        except Exception as e:
            logger.error(f'Failed to delete VM {nb_vm.name}: {e}')


def main(
        config: Optional[Config] = None,
        pve_api: Optional[ProxmoxAPI] = None,
        nb_api: Optional[pynetbox.api] = None,
) -> None:
    """
    Run one full synchronization: provision custom fields and roles, load NetBox
    objects, then sync all nodes/VMs/LXC.

    Args:
        config: Configuration to use. Loaded from the environment when omitted.
        pve_api: Existing Proxmox client to reuse; created when omitted.
        nb_api: Existing NetBox client to reuse; created when omitted.

    The HTTP server for metrics and health endpoints is started by the CLI so it
    runs once per process, not on every sync cycle.
    """
    if config is not None:
        set_config(config)
    config = _cfg()
    log_section('Starting pve2netbox')

    if config.dry_run:
        logger.warning(
            'DRY RUN MODE: provisioning, node status and cleanup are skipped '
            '(see README for the current limits of DRY_RUN)'
        )

    if pve_api is None:
        pve_api = create_proxmox_api(config)
    if nb_api is None:
        nb_api = pynetbox.api(
            url=config.nb_api_url,
            token=config.nb_api_token,
        )
        nb_api.http_session = _make_netbox_session()
    if config.nb_cluster_id is None:
        # Only NB_CLUSTER_NAME was given and nobody resolved it yet.
        resolve_cluster(nb_api, config)

    sync_start_time = metrics.record_full_sync_start()
    _provision_custom_fields(nb_api)
    _provision_roles(nb_api)

    decisions = FilterDecisions(get_filters())
    pve_nodes = _select_pve_nodes(pve_api, decisions)
    node_names = [pve_node['node'] for pve_node in pve_nodes]

    nb_objects = _load_nb_objects(nb_api, node_names)
    current_vmids = set()
    logger.info('Processing Proxmox tags...')
    _process_pve_tags(
        pve_api,
        nb_api,
        nb_objects,
    )
    logger.info('Fetching VM tags from Proxmox...')
    pve_vm_tags, pve_vm_pools, pve_template_vmids = _collect_pve_guest_metadata(
        pve_api, nb_api, nb_objects, decisions)
    decisions.log_summary()
    metrics.record_filtered(decisions.excluded_count)

    skip_templates = config.template_policy == 'skip'
    template_count = len(pve_template_vmids)
    if template_count and skip_templates:
        # Templates stay out of current_vmids, so ENABLE_CLEANUP removes the ones
        # a previous run created.
        logger.info(f'Skipping {template_count} template(s) (TEMPLATE_POLICY=skip)')

    pve_ha_virtual_machine_ids = list(
        map(
            lambda r: int(r['sid'].split(':')[1]),
            filter(lambda r: r['type'] == 'service', pve_api.cluster.ha.status.current.get())
        )
    )
    logger.info('Processing Proxmox nodes...')
    vm_count = 0
    lxc_count = 0
    
    sync_errors = 0
    interrupted = False
    for pve_node in pve_nodes:
        if shutdown.should_stop():
            interrupted = True
            break
        logger.info(f'  Processing node: {pve_node["node"]}')
        pve_replicated_virtual_machine_ids = list(
            map(lambda r: r['guest'], pve_api.nodes(pve_node['node']).replication.get())
        )
        nb_device = nb_objects['devices'].get(pve_node['node'].lower())
        if nb_device is None:
            sync_errors += 1
            message = (
                f'Node {pve_node["node"]} has no matching device in NetBox. '
                f'Create a device named "{pve_node["node"]}" in NetBox '
                f'(names must match exactly, case-insensitively).'
            )
            if config.node_missing_policy == 'fail':
                logger.error(f'{message} Exiting (NODE_MISSING_POLICY=fail).')
                sys.exit(1)
            logger.error(f'{message} Skipping the node (NODE_MISSING_POLICY=skip).')
            continue
        if not config.dry_run:
            nb_device.status = 'active' if pve_node['status'] == 'online' else 'offline'
            nb_device.save()
        if config.sync_vms:
            for pve_virtual_machine in pve_api.nodes(pve_node['node']).qemu.get():
                if shutdown.should_stop():
                    interrupted = True
                    break
                if _skip_filtered_guest(
                        decisions, pve_virtual_machine, pve_node['node'], QEMU, pve_vm_pools):
                    continue
                if _preload_incomplete(nb_objects, int(pve_virtual_machine['vmid'])):
                    sync_errors += 1
                    # Still present in Proxmox, so cleanup must not read the
                    # skipped sync as "gone".
                    current_vmids.add(pve_virtual_machine['vmid'])
                    logger.error(
                        f'    Skipping {pve_virtual_machine["name"]} '
                        f'(ID: {pve_virtual_machine["vmid"]}): its NetBox objects '
                        f'failed to load, and syncing now would duplicate them'
                    )
                    continue
                if skip_templates and pve_virtual_machine['vmid'] in pve_template_vmids:
                    logger.debug(
                        f'    Skipping template VM: {pve_virtual_machine["name"]} '
                        f'(ID: {pve_virtual_machine["vmid"]})'
                    )
                    continue
                logger.info(f'    Processing VM: {pve_virtual_machine["name"]} (ID: {pve_virtual_machine["vmid"]})')
                current_vmids.add(pve_virtual_machine["vmid"])
                vm_count += 1
                metrics.record_vm_sync()
                try:
                    _process_pve_virtual_machine(
                        pve_api,
                        nb_api,
                        nb_objects,
                        nb_device,
                        pve_vm_tags.get(pve_virtual_machine['vmid'], []),
                        pve_virtual_machine,
                        pve_virtual_machine['vmid'] in pve_replicated_virtual_machine_ids,
                        pve_virtual_machine['vmid'] in pve_ha_virtual_machine_ids,
                        pve_vm_pools.get(pve_virtual_machine['vmid']),
                    )
                except Exception as e:
                    sync_errors += 1
                    logger.error(
                        f'    Failed sync for VM {pve_virtual_machine["name"]} '
                        f'(ID: {pve_virtual_machine["vmid"]}): {e}',
                        exc_info=True,
                    )
        if interrupted:
            break
        if config.sync_lxc:
            for pve_container in pve_api.nodes(pve_node['node']).lxc.get():
                if shutdown.should_stop():
                    interrupted = True
                    break
                if _skip_filtered_guest(
                        decisions, pve_container, pve_node['node'], LXC, pve_vm_pools):
                    continue
                if _preload_incomplete(nb_objects, int(pve_container['vmid'])):
                    sync_errors += 1
                    # Still present in Proxmox, so cleanup must not read the
                    # skipped sync as "gone".
                    current_vmids.add(pve_container['vmid'])
                    logger.error(
                        f'    Skipping {pve_container["name"]} (ID: {pve_container["vmid"]}): '
                        f'its NetBox objects failed to load, and syncing now '
                        f'would duplicate them'
                    )
                    continue
                if skip_templates and pve_container['vmid'] in pve_template_vmids:
                    logger.debug(
                        f'    Skipping template LXC: {pve_container["name"]} '
                        f'(ID: {pve_container["vmid"]})'
                    )
                    continue
                logger.info(f'    Processing LXC: {pve_container["name"]} (ID: {pve_container["vmid"]})')
                current_vmids.add(pve_container["vmid"])
                lxc_count += 1
                metrics.record_lxc_sync()
                try:
                    _process_pve_lxc_container(
                        pve_api,
                        nb_api,
                        nb_objects,
                        nb_device,
                        pve_vm_tags.get(pve_container['vmid'], []),
                        pve_container,
                        pve_container['vmid'] in pve_replicated_virtual_machine_ids,
                        pve_container['vmid'] in pve_ha_virtual_machine_ids,
                        pve_vm_pools.get(pve_container['vmid']),
                    )
                except Exception as e:
                    sync_errors += 1
                    logger.error(
                        f'    Failed sync for LXC {pve_container["name"]} '
                        f'(ID: {pve_container["vmid"]}): {e}',
                        exc_info=True,
                    )
    if interrupted:
        # A partial pass must not mark cleanup safe: VMs of nodes that were never
        # visited would look stale and be deleted from NetBox.
        logger.warning('Sync interrupted by shutdown request; skipping cleanup')
    elif config.enable_cleanup:
        cleanup_stale_vms(
            nb_api, nb_objects, current_vmids, config.dry_run, decisions.excluded_vmids)

    succeeded = sync_errors == 0 and not interrupted
    metrics.record_full_sync_end(sync_start_time, vm_count, lxc_count, success=succeeded)

    if interrupted:
        log_section('Sync interrupted')
    elif sync_errors:
        log_section('Sync completed with errors')
        logger.warning(f'Sync finished with {sync_errors} error(s)')
    else:
        log_section('Sync completed successfully!')
    logger.info(f'Synchronized {vm_count} VMs and {lxc_count} LXC containers')
    logger.info(f'Duration: {time.time() - sync_start_time:.2f}s')

