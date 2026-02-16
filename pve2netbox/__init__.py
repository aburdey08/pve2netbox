# pylint: disable=fixme,too-many-branches

"""
pve2netbox: Synchronize Proxmox Virtual Environment (PVE) information to a NetBox instance.
"""

import os
import sys
import time
from typing import Optional, Dict, Any

import pynetbox
import requests
import urllib3
from proxmoxer import ProxmoxAPI, ResourceException
from urllib3.util.retry import Retry

from .config import Config, load_config
from .logger import logger, log_section
from .metrics import metrics
from .utils import (
    parse_pve_network_definition as _parse_pve_network_definition,
    parse_pve_disk_definition as _parse_pve_disk_definition,
    parse_pve_disk_size as _process_pve_disk_size,
    get_virtual_machine_vcpus as _get_virtual_machine_vcpus,
)

_config: Optional[Config] = None
"""Current configuration; set in main()."""


class _RateLimitRetryAdapter(requests.adapters.HTTPAdapter):
    """HTTP adapter with optional delay before each request and retry on 502/503/429."""

    def __init__(self, delay_seconds: float = 0.0, retry: Optional[Retry] = None, *args, **kwargs):
        super().__init__(*args, max_retries=retry, **kwargs)
        self._delay_seconds = delay_seconds

    def send(self, request, **kwargs):
        if self._delay_seconds > 0:
            time.sleep(self._delay_seconds)
        return super().send(request, **kwargs)


def _make_netbox_session() -> requests.Session:
    """Create requests session with retry on 502/503/429 and optional delay between requests."""
    delay = float(os.getenv('NB_API_DELAY_SECONDS', '0.2'))
    retry_total = int(os.getenv('NB_API_RETRY_TOTAL', '5'))
    retry_backoff = float(os.getenv('NB_API_RETRY_BACKOFF', '1.0'))

    retries = Retry(
        total=retry_total,
        backoff_factor=retry_backoff,
        status_forcelist=(502, 503, 429),
        allowed_methods=('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'),
    )
    adapter = _RateLimitRetryAdapter(delay_seconds=delay, retry=retries)
    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def _provision_custom_fields(_nb_api: pynetbox.api) -> None:
    """Create required custom fields in NetBox if they do not exist."""
    if _config and _config.dry_run:
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
    vm_role_name = os.getenv('VM_ROLE')
    lxc_role_name = os.getenv('LXC_ROLE')
    
    if not vm_role_name and not lxc_role_name:
        logger.info('Provisioning device roles...')
        logger.info('  No VM_ROLE or LXC_ROLE configured, skipping')
        return
    
    if _config and _config.dry_run:
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


def _load_nb_objects(_nb_api: pynetbox.api) -> dict:
    """
    Load all NetBox objects needed for full sync into a single cache dict.

    Loads devices, virtual machines, interfaces, MAC addresses, prefixes,
    IP addresses, VLANs, virtual disks, tags, and device roles. Keys are
    normalized (e.g. device name lowercased, VM by serial). Returns dict
    with keys as above.
    """
    logger.info('Loading NetBox objects...')
    _nb_objects = {
        'devices': {},
        'virtual_machines': {},
        'virtual_machines_interfaces': {},
        'mac_addresses': {},
        'prefixes': {},
        'ip_addresses': {},
        'vlans': {},
        'disks': {},
        'tags': {},
        'roles': {},
    }
    logger.debug('  - Loading devices...')
    for _nb_device in _nb_api.dcim.devices.all():
        _nb_objects['devices'][_nb_device.name.lower()] = _nb_device
    logger.debug('  - Loading virtual machines...')
    vm_ids = []
    for _nb_virtual_machine in _nb_api.virtualization.virtual_machines.all():
        _nb_objects['virtual_machines'][_nb_virtual_machine.serial] = _nb_virtual_machine
        vm_ids.append(_nb_virtual_machine.id)
    logger.debug('  - Loading interfaces...')
    interfaces_list = list(_nb_api.virtualization.interfaces.all())
    for _nb_interface in interfaces_list:
        if _nb_interface.virtual_machine.id not in _nb_objects['virtual_machines_interfaces']:
            _nb_objects['virtual_machines_interfaces'][_nb_interface.virtual_machine.id] = {}
        _nb_objects['virtual_machines_interfaces'][_nb_interface.virtual_machine.id][_nb_interface.name] = _nb_interface
    logger.debug('  - Loading MAC addresses...')
    for _nb_mac_address in _nb_api.dcim.mac_addresses.all():
        _nb_objects['mac_addresses'][_nb_mac_address.mac_address] = _nb_mac_address
    logger.debug('  - Loading prefixes...')
    for _nb_prefix in _nb_api.ipam.prefixes.all():
        _nb_objects['prefixes'][_nb_prefix.prefix] = _nb_prefix
    logger.debug('  - Loading IP addresses...')
    for _nb_ip_address in _nb_api.ipam.ip_addresses.all():
        _nb_objects['ip_addresses'][_nb_ip_address['address']] = _nb_ip_address
    logger.debug('  - Loading VLANs...')
    for _nb_vlan in _nb_api.ipam.vlans.all():
        _nb_objects['vlans'][str(_nb_vlan.vid)] = _nb_vlan
    logger.debug('  - Loading virtual disks...')
    for _nb_disk in _nb_api.virtualization.virtual_disks.all():
        if _nb_disk.virtual_machine.id not in _nb_objects['disks']:
            _nb_objects['disks'][_nb_disk.virtual_machine.id] = {}
        _nb_objects['disks'][_nb_disk.virtual_machine.id][_nb_disk.name] = _nb_disk
    logger.debug('  - Loading tags...')
    for _nb_tag in _nb_api.extras.tags.all():
        _nb_objects['tags'][_nb_tag.name] = _nb_tag
    logger.debug('  - Loading device roles...')
    for _nb_role in _nb_api.dcim.device_roles.all():
        _nb_objects['roles'][_nb_role.name] = _nb_role
        _nb_objects['roles'][str(_nb_role.id)] = _nb_role
    logger.info('NetBox objects loaded.')
    return _nb_objects


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


def _get_role_id(_nb_objects: dict, role_name_or_id: Optional[str]) -> Optional[int]:
    """Resolve device role ID by name or ID (e.g. from VM_ROLE/LXC_ROLE env)."""
    if not role_name_or_id:
        return None
    role = _nb_objects['roles'].get(role_name_or_id)
    if role:
        return role.id
    
    return None


def _process_pve_lxc_container(
        _pve_api: ProxmoxAPI,
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _nb_device: any,
        _pve_tags: [str],
        _pve_container: dict,
        _is_replicated: bool,
        _has_ha: bool,
) -> dict:
    """
    Sync one LXC container from Proxmox to NetBox.
    Creates or updates the VM record, then syncs network interfaces and disks.
    LXC is represented in NetBox as a virtual machine; role from LXC_ROLE env.
    """
    _pve_node_name = _nb_device.name.lower()
    pve_container_config = _pve_api.nodes(_pve_node_name).lxc(_pve_container['vmid']).config.get()
    lxc_role_id = _get_role_id(_nb_objects, os.getenv('LXC_ROLE'))
    nb_virtual_machine = _nb_objects['virtual_machines'].get(str(_pve_container['vmid']))
    if nb_virtual_machine is None:
        create_params = {
            'serial': _pve_container['vmid'],
            'name': pve_container_config.get('hostname', _pve_container['name']),
            'site': _nb_device.site.id,
            'cluster': os.environ.get('NB_CLUSTER_ID', 1),
            'device': _nb_device.id,
            'vcpus': pve_container_config.get('cores', 1),
            'memory': int(pve_container_config.get('memory', 512)),
            'status': 'active' if _pve_container['status'] == 'running' else 'offline',
            'tags': list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags)),
            'custom_fields': {
                'autostart': pve_container_config.get('onboot') == 1,
                'replicated': _is_replicated,
                'ha': _has_ha,
            }
        }
        if lxc_role_id:
            create_params['role'] = lxc_role_id
        
        nb_virtual_machine = _nb_api.virtualization.virtual_machines.create(**create_params)
        _nb_objects['virtual_machines'][str(_pve_container['vmid'])] = nb_virtual_machine
    else:
        nb_virtual_machine.name = pve_container_config.get('hostname', _pve_container['name'])
        nb_virtual_machine.site = _nb_device.site.id
        nb_virtual_machine.cluster = os.environ.get('NB_CLUSTER_ID', 1)
        nb_virtual_machine.device = _nb_device.id
        nb_virtual_machine.vcpus = pve_container_config.get('cores', 1)
        nb_virtual_machine.memory = int(pve_container_config.get('memory', 512))
        nb_virtual_machine.status = 'active' if _pve_container['status'] == 'running' else 'offline'
        nb_virtual_machine.tags = list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags))
        if lxc_role_id:
            nb_virtual_machine.role = lxc_role_id
        nb_virtual_machine.custom_fields['autostart'] = pve_container_config.get('onboot') == 1
        nb_virtual_machine.custom_fields['replicated'] = _is_replicated
        nb_virtual_machine.custom_fields['ha'] = _has_ha
        nb_virtual_machine.save()
    _process_pve_lxc_network_interfaces(
        _nb_api,
        _nb_objects,
        pve_container_config,
        nb_virtual_machine,
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
    vm_role_id = _get_role_id(_nb_objects, os.getenv('VM_ROLE'))
    nb_virtual_machine = _nb_objects['virtual_machines'].get(str(_pve_virtual_machine['vmid']))
    if nb_virtual_machine is None:
        create_params = {
            'serial': _pve_virtual_machine['vmid'],
            'name': _pve_virtual_machine['name'],
            'site': _nb_device.site.id,
            'cluster': os.environ.get('NB_CLUSTER_ID', 1),
            'device': _nb_device.id,
            'vcpus': _get_virtual_machine_vcpus(pve_virtual_machine_config),
            'memory': int(pve_virtual_machine_config['memory']),
            'status': 'active' if _pve_virtual_machine['status'] == 'running' else 'offline',
            'tags': list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags)),
            'custom_fields': {
                'autostart': pve_virtual_machine_config.get('onboot') == 1,
                'replicated': _is_replicated,
                'ha': _has_ha,
            }
        }
        if vm_role_id:
            create_params['role'] = vm_role_id
        
        nb_virtual_machine = _nb_api.virtualization.virtual_machines.create(**create_params)
    else:
        nb_virtual_machine.name = _pve_virtual_machine['name']
        nb_virtual_machine.site = _nb_device.site.id
        nb_virtual_machine.cluster = os.environ.get('NB_CLUSTER_ID', 1)
        nb_virtual_machine.device = _nb_device.id
        nb_virtual_machine.vcpus = _get_virtual_machine_vcpus(pve_virtual_machine_config)
        nb_virtual_machine.memory = int(pve_virtual_machine_config['memory'])
        nb_virtual_machine.status = 'active' if _pve_virtual_machine['status'] == 'running' else 'offline'
        nb_virtual_machine.tags = list(map(lambda _pve_tag_name: _nb_objects['tags'][_pve_tag_name].id, _pve_tags))
        if vm_role_id:
            nb_virtual_machine.role = vm_role_id
        nb_virtual_machine.custom_fields['autostart'] = pve_virtual_machine_config.get('onboot') == 1
        nb_virtual_machine.custom_fields['replicated'] = _is_replicated
        nb_virtual_machine.custom_fields['ha'] = _has_ha
        nb_virtual_machine.save()
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
    return _nb_objects


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
        _prefix_network_address = '.'.join(_virtual_machine_address.split('.')[:-1]) + '.0'
        _prefix_network_full_address = f'{_prefix_network_address}/{_virtual_machine_address_mask}'

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

        _nb_virtual_machine.primary_ip4 = nb_ip_address.id
        _nb_virtual_machine.save()
        logger.info(f'        ✓ Set primary IPv4: {_virtual_machine_full_address}')
    else:
        logger.debug(f'        Interface {_interface_name}: no IPv4 address found from guest agent')
        if _interface_vlan_id is not None:
            nb_vlan = _nb_objects['vlans'].get(str(_interface_vlan_id))
            if nb_vlan is None:
                nb_vlan = _nb_api.ipam.vlans.create(
                    vid=_interface_vlan_id,
                    name=f'VLAN {_interface_vlan_id}',
                )
                _nb_objects['vlans'][_interface_vlan_id] = nb_vlan

            nb_prefix.vlan = nb_vlan.id
            nb_prefix.save()

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
        _nb_api.virtualization.virtual_disks.create(
            name=_disk_name,
            size=_disk_size,
            virtual_machine=_nb_virtual_machine.id,
            custom_fields={
                'backup': _has_backup,
            }
        )
    else:
        nb_disk.size = _disk_size
        nb_disk.custom_fields['backup'] = _has_backup
        nb_disk.save()

    return _nb_objects


def _process_pve_lxc_network_interfaces(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _pve_container_config: dict,
        _nb_virtual_machine: any,
) -> dict:
    """Sync LXC container network interfaces (net0, net1, ...). Uses hwaddr for MAC, name= for interface name."""
    for (_config_key, _config_value) in _pve_container_config.items():
        if not _config_key.startswith('net'):
            continue
        _network_definition = _parse_pve_network_definition(_config_value)
        network_mac_address = _network_definition.get('hwaddr')
        if network_mac_address is None:
            continue
        interface_name = _network_definition.get('name', _config_key)
        _process_pve_virtual_machine_network_interface(
            _nb_api,
            _nb_objects,
            _nb_virtual_machine,
            _config_key,
            interface_name,
            network_mac_address,
            _network_definition.get('tag'),  # VLAN tag
            _network_definition.get('mtu'),  # MTU
            {},  # LXC has no guest agent, IP addresses empty
        )

    return _nb_objects


def _process_pve_lxc_disks(
        _nb_api: pynetbox.api,
        _nb_objects: dict,
        _pve_container_config: dict,
        _nb_virtual_machine: any,
) -> dict:
    """Sync LXC disks: rootfs (root) and mp0, mp1, ... (mount points)."""
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


def quick_check_changes(_pve_api: ProxmoxAPI, _last_state: dict) -> tuple[list[int], dict]:
    """
    Quick check for VM changes without loading full config.
    Returns (list of changed vmid, current_state dict). Uses SYNC_VMS/SYNC_LXC env.
    """
    current_state = {}
    sync_vms = os.getenv('SYNC_VMS', 'true').lower() == 'true'
    sync_lxc = os.getenv('SYNC_LXC', 'true').lower() == 'true'
    for pve_node in _pve_api.nodes.get():
        node_name = pve_node['node']
        if sync_vms:
            try:
                for vm in _pve_api.nodes(node_name).qemu.get():
                    current_state[vm['vmid']] = {
                        'type': 'qemu',
                        'status': vm['status'],
                        'name': vm['name'],
                        'node': node_name,
                        'maxmem': vm.get('maxmem', 0),
                        'maxdisk': vm.get('maxdisk', 0),
                    }
            except Exception as e:
                logger.warning(f'Failed to get QEMU VMs from node {node_name}: {e}')
        if sync_lxc:
            try:
                for ct in _pve_api.nodes(node_name).lxc.get():
                    current_state[ct['vmid']] = {
                        'type': 'lxc',
                        'status': ct['status'],
                        'name': ct['name'],
                        'node': node_name,
                        'maxmem': ct.get('maxmem', 0),
                        'maxdisk': ct.get('maxdisk', 0),
                    }
            except Exception as e:
                logger.warning(f'Failed to get LXC containers from node {node_name}: {e}')
    changed_vmids = []
    for vmid, data in current_state.items():
        if vmid not in _last_state or _last_state[vmid] != data:
            changed_vmids.append(vmid)
    for vmid in _last_state:
        if vmid not in current_state:
            changed_vmids.append(vmid)
    
    return changed_vmids, current_state


def _load_specific_objects(_nb_api: pynetbox.api, _changed_vmids: list[int]) -> dict:
    """
    Load from NetBox only objects related to the given VM IDs.
    Lighter-weight than _load_nb_objects for incremental (quick) sync.
    """
    logger.info(f'Loading NetBox objects for {len(_changed_vmids)} VMs...')
    _nb_objects = {
        'devices': {},
        'virtual_machines': {},
        'virtual_machines_interfaces': {},
        'mac_addresses': {},
        'prefixes': {},
        'ip_addresses': {},
        'vlans': {},
        'disks': {},
        'tags': {},
        'roles': {},
    }
    logger.debug('  - Loading devices...')
    for _nb_device in _nb_api.dcim.devices.all():
        _nb_objects['devices'][_nb_device.name.lower()] = _nb_device
    logger.debug(f'  - Loading {len(_changed_vmids)} specific virtual machines...')
    for vmid in _changed_vmids:
        try:
            vms = _nb_api.virtualization.virtual_machines.filter(serial=str(vmid))
            for vm in vms:
                _nb_objects['virtual_machines'][vm.serial] = vm
        except Exception as e:
            logger.warning(f'Failed to load VM {vmid}: {e}')
    logger.debug('  - Loading interfaces for changed VMs...')
    vm_ids = [vm.id for vm in _nb_objects['virtual_machines'].values()]
    for vm_id in vm_ids:
        try:
            interfaces = _nb_api.virtualization.interfaces.filter(virtual_machine_id=vm_id)
            if vm_id not in _nb_objects['virtual_machines_interfaces']:
                _nb_objects['virtual_machines_interfaces'][vm_id] = {}
            for iface in interfaces:
                _nb_objects['virtual_machines_interfaces'][vm_id][iface.name] = iface
        except Exception as e:
            logger.warning(f'Failed to load interfaces for VM {vm_id}: {e}')
    logger.debug('  - Loading MAC addresses...')
    for vm_interfaces in _nb_objects['virtual_machines_interfaces'].values():
        for iface in vm_interfaces.values():
            if hasattr(iface, 'primary_mac_address') and iface.primary_mac_address:
                try:
                    mac = _nb_api.dcim.mac_addresses.get(iface.primary_mac_address.id)
                    if mac:
                        _nb_objects['mac_addresses'][mac.mac_address] = mac
                except Exception as e:
                    logger.warning(f'Failed to load MAC for interface {iface.id}: {e}')
    logger.debug('  - Loading prefixes...')
    for _nb_prefix in _nb_api.ipam.prefixes.all():
        _nb_objects['prefixes'][_nb_prefix.prefix] = _nb_prefix
    logger.debug('  - Loading IP addresses for changed VMs...')
    for vm_id in vm_ids:
        try:
            ips = _nb_api.ipam.ip_addresses.filter(virtual_machine_id=vm_id)
            for ip in ips:
                _nb_objects['ip_addresses'][ip['address']] = ip
        except Exception as e:
            logger.warning(f'Failed to load IPs for VM {vm_id}: {e}')
    logger.debug('  - Loading VLANs...')
    for _nb_vlan in _nb_api.ipam.vlans.all():
        _nb_objects['vlans'][str(_nb_vlan.vid)] = _nb_vlan
    logger.debug('  - Loading virtual disks for changed VMs...')
    for vm_id in vm_ids:
        try:
            disks = _nb_api.virtualization.virtual_disks.filter(virtual_machine_id=vm_id)
            if vm_id not in _nb_objects['disks']:
                _nb_objects['disks'][vm_id] = {}
            for disk in disks:
                _nb_objects['disks'][vm_id][disk.name] = disk
        except Exception as e:
            logger.warning(f'Failed to load disks for VM {vm_id}: {e}')
    logger.debug('  - Loading tags...')
    for _nb_tag in _nb_api.extras.tags.all():
        _nb_objects['tags'][_nb_tag.name] = _nb_tag
    logger.debug('  - Loading device roles...')
    for _nb_role in _nb_api.dcim.device_roles.all():
        _nb_objects['roles'][_nb_role.name] = _nb_role
        _nb_objects['roles'][str(_nb_role.id)] = _nb_role

    logger.info('NetBox objects loaded.')
    return _nb_objects


def sync_specific_vms(
        _pve_api: ProxmoxAPI,
        _nb_api: pynetbox.api,
        _changed_vmids: list[int],
) -> None:
    """
    Sync only the given VM IDs to NetBox (incremental quick sync).
    Loads only needed NetBox objects, processes tags and HA, then syncs VMs per node.
    """
    if not _changed_vmids:
        logger.info('No changes detected, skipping sync.')
        return
    logger.info(f'Quick sync: processing {len(_changed_vmids)} changed VMs...')
    nb_objects = _load_specific_objects(_nb_api, _changed_vmids)
    _process_pve_tags(_pve_api, _nb_api, nb_objects)
    logger.info('Fetching VM metadata from Proxmox...')
    pve_vm_tags = {}
    for pve_vm_resource in _pve_api.cluster.resources.get(type='vm'):
        if pve_vm_resource['vmid'] in _changed_vmids:
            pve_vm_tags[pve_vm_resource['vmid']] = []
            if 'pool' in pve_vm_resource:
                pve_vm_tags[pve_vm_resource['vmid']].append(f'Pool/{pve_vm_resource["pool"]}')
            if 'tags' in pve_vm_resource:
                pass  # TODO: add tags support
    
    pve_ha_virtual_machine_ids = list(
        map(
            lambda r: int(r['sid'].split(':')[1]),
            filter(lambda r: r['type'] == 'service', _pve_api.cluster.ha.status.current.get())
        )
    )
    vms_by_node = {}
    nodes_info = {}
    for pve_node in _pve_api.nodes.get():
        node_name = pve_node['node']
        nodes_info[node_name] = pve_node
        vms_by_node[node_name] = {'qemu': [], 'lxc': []}
        sync_vms = os.getenv('SYNC_VMS', 'true').lower() == 'true'
        if sync_vms:
            for vm in _pve_api.nodes(node_name).qemu.get():
                if vm['vmid'] in _changed_vmids:
                    vms_by_node[node_name]['qemu'].append(vm)
        sync_lxc = os.getenv('SYNC_LXC', 'true').lower() == 'true'
        if sync_lxc:
            for ct in _pve_api.nodes(node_name).lxc.get():
                if ct['vmid'] in _changed_vmids:
                    vms_by_node[node_name]['lxc'].append(ct)
    for node_name, vms in vms_by_node.items():
        if not vms['qemu'] and not vms['lxc']:
            continue
        logger.info(f'  Processing node: {node_name}')
        pve_replicated_virtual_machine_ids = list(
            map(lambda r: r['guest'], _pve_api.nodes(node_name).replication.get())
        )
        
        nb_device = nb_objects['devices'].get(node_name.lower())
        if nb_device is None:
            logger.warning(f'Device {node_name} not found in NetBox, skipping.')
            continue
        pve_node = nodes_info[node_name]
        nb_device.status = 'active' if pve_node['status'] == 'online' else 'offline'
        nb_device.save()
        for vm in vms['qemu']:
            logger.info(f'    Quick sync VM: {vm["name"]} (ID: {vm["vmid"]})')
            _process_pve_virtual_machine(
                _pve_api,
                _nb_api,
                nb_objects,
                nb_device,
                pve_vm_tags.get(vm['vmid'], []),
                vm,
                vm['vmid'] in pve_replicated_virtual_machine_ids,
                vm['vmid'] in pve_ha_virtual_machine_ids,
            )
        for ct in vms['lxc']:
            logger.info(f'    Quick sync LXC: {ct["name"]} (ID: {ct["vmid"]})')
            _process_pve_lxc_container(
                _pve_api,
                _nb_api,
                nb_objects,
                nb_device,
                pve_vm_tags.get(ct['vmid'], []),
                ct,
                ct['vmid'] in pve_replicated_virtual_machine_ids,
                ct['vmid'] in pve_ha_virtual_machine_ids,
            )
    
    logger.info('Quick sync completed successfully!')


def cleanup_stale_vms(nb_api: pynetbox.api, nb_objects: dict, current_vmids: set, dry_run: bool = False) -> None:
    """
    Remove VMs from NetBox that no longer exist in Proxmox.
    
    Args:
        nb_api: NetBox API instance
        nb_objects: Dictionary of NetBox objects
        current_vmids: Set of current VM IDs from Proxmox
        dry_run: If True, only log what would be deleted
    """
    logger.info('Checking for stale VMs in NetBox...')
    
    stale_vms = []
    for serial, nb_vm in nb_objects['virtual_machines'].items():
        try:
            vmid = int(serial)
            if vmid not in current_vmids:
                stale_vms.append((vmid, nb_vm))
        except (ValueError, TypeError):
            continue
    
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


def main():
    """
    Main entrypoint: load config, connect to Proxmox and NetBox, provision custom fields
    and roles, load NetBox objects, then sync all nodes/VMs/LXC. Metrics server is
    started in __main__.py so it runs once per process, not on every sync cycle.
    """
    global _config
    _config = load_config()
    log_section('Starting pve2netbox')
    
    if _config.dry_run:
        logger.warning('DRY RUN MODE: No changes will be made to NetBox')
    pve_api = ProxmoxAPI(
        host=_config.pve_api_host,
        user=_config.pve_api_user,
        token_name=_config.pve_api_token,
        token_value=_config.pve_api_secret,
        verify_ssl=_config.pve_api_verify_ssl,
    )
    nb_api = pynetbox.api(
        url=_config.nb_api_url,
        token=_config.nb_api_token,
    )
    nb_api.http_session = _make_netbox_session()
    _provision_custom_fields(nb_api)
    _provision_roles(nb_api)
    nb_objects = _load_nb_objects(nb_api)
    sync_start_time = time.time()
    current_vmids = set()
    logger.info('Processing Proxmox tags...')
    _process_pve_tags(
        pve_api,
        nb_api,
        nb_objects,
    )
    logger.info('Fetching VM tags from Proxmox...')
    pve_vm_tags = {}
    for pve_vm_resource in pve_api.cluster.resources.get(type='vm'):
        pve_vm_tags[pve_vm_resource['vmid']] = []

        if 'pool' in pve_vm_resource:
            pve_vm_tags[pve_vm_resource['vmid']].append(f'Pool/{pve_vm_resource["pool"]}')

        if 'tags' in pve_vm_resource:
            pass  # TODO: pve_vm_tags[pve_vm_resource['vmid']].append(pve_vm_resource['tags'])

    pve_ha_virtual_machine_ids = list(
        map(
            lambda r: int(r['sid'].split(':')[1]),
            filter(lambda r: r['type'] == 'service', pve_api.cluster.ha.status.current.get())
        )
    )
    logger.info('Processing Proxmox nodes...')
    vm_count = 0
    lxc_count = 0
    
    for pve_node in pve_api.nodes.get():
        logger.info(f'  Processing node: {pve_node["node"]}')
        pve_replicated_virtual_machine_ids = list(
            map(lambda r: r['guest'], pve_api.nodes(pve_node['node']).replication.get())
        )
        nb_device = nb_objects['devices'].get(pve_node['node'].lower())
        if nb_device is None:
            logger.error(f'The device {pve_node["node"]} is not created on NetBox. Exiting.')
            sys.exit(1)
        else:
            if not _config.dry_run:
                nb_device.status = 'active' if pve_node['status'] == 'online' else 'offline'
                nb_device.save()
        if _config.sync_vms:
            for pve_virtual_machine in pve_api.nodes(pve_node['node']).qemu.get():
                logger.info(f'    Processing VM: {pve_virtual_machine["name"]} (ID: {pve_virtual_machine["vmid"]})')
                current_vmids.add(pve_virtual_machine["vmid"])
                vm_count += 1
                metrics.record_vm_sync()
                _process_pve_virtual_machine(
                    pve_api,
                    nb_api,
                    nb_objects,
                    nb_device,
                    pve_vm_tags.get(pve_virtual_machine['vmid'], []),
                    pve_virtual_machine,
                    pve_virtual_machine['vmid'] in pve_replicated_virtual_machine_ids,
                    pve_virtual_machine['vmid'] in pve_ha_virtual_machine_ids,
                )
        if _config.sync_lxc:
            for pve_container in pve_api.nodes(pve_node['node']).lxc.get():
                logger.info(f'    Processing LXC: {pve_container["name"]} (ID: {pve_container["vmid"]})')
                current_vmids.add(pve_container["vmid"])
                lxc_count += 1
                metrics.record_lxc_sync()
                _process_pve_lxc_container(
                    pve_api,
                    nb_api,
                    nb_objects,
                    nb_device,
                    pve_vm_tags.get(pve_container['vmid'], []),
                    pve_container,
                    pve_container['vmid'] in pve_replicated_virtual_machine_ids,
                    pve_container['vmid'] in pve_ha_virtual_machine_ids,
                )
    if _config.enable_cleanup:
        cleanup_stale_vms(nb_api, nb_objects, current_vmids, _config.dry_run)
    metrics.record_full_sync_end(sync_start_time, vm_count, lxc_count)
    
    log_section('Sync completed successfully!')
    logger.info(f'Synchronized {vm_count} VMs and {lxc_count} LXC containers')
    logger.info(f'Duration: {time.time() - sync_start_time:.2f}s')


if __name__ == '__main__':
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()
