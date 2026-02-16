"""NetBox API adapter with rate limiting and provisioning."""

import time
from typing import Dict, Any, Optional
import requests
import pynetbox
from urllib3.util.retry import Retry

from ..config import Config, ROLE_COLORS
from ..logger import logger


class RateLimitRetryAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter with rate limiting and retry on 502/503/429."""

    def __init__(self, delay_seconds: float = 0.0, retry: Optional[Retry] = None, 
                 *args: Any, **kwargs: Any):
        super().__init__(*args, max_retries=retry, **kwargs)
        self._delay_seconds = delay_seconds

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        if self._delay_seconds > 0:
            time.sleep(self._delay_seconds)
        return super().send(request, **kwargs)


def make_netbox_session(config: Config) -> requests.Session:
    """Create NetBox session with retry and rate limiting."""
    retries = Retry(
        total=config.nb_api_retry_total,
        backoff_factor=config.nb_api_retry_backoff,
        status_forcelist=(502, 503, 429),
        allowed_methods=('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'),
    )
    adapter = RateLimitRetryAdapter(delay_seconds=config.nb_api_delay_seconds, retry=retries)
    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def create_netbox_api(config: Config) -> pynetbox.api:
    """Create and configure NetBox API instance."""
    nb_api = pynetbox.api(
        url=config.nb_api_url,
        token=config.nb_api_token,
    )
    nb_api.http_session = make_netbox_session(config)
    return nb_api


def provision_custom_fields(nb_api: pynetbox.api, dry_run: bool = False) -> None:
    """Create required custom fields in NetBox if they don't exist."""
    logger.info('Provisioning custom fields...')
    
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
    
    existing_fields = {cf.name: cf for cf in nb_api.extras.custom_fields.all()}
    
    for field_def in required_fields:
        if field_def['name'] in existing_fields:
            logger.info(f'  ✓ Custom field "{field_def["name"]}" already exists')
        else:
            if dry_run:
                logger.info(f'  [DRY RUN] Would create custom field "{field_def["name"]}"')
                continue
            
            try:
                nb_api.extras.custom_fields.create(
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


def provision_roles(nb_api: pynetbox.api, config: Config, dry_run: bool = False) -> None:
    """Create device roles in NetBox if specified and don't exist."""
    if not config.vm_role and not config.lxc_role:
        return
    
    logger.info('Provisioning device roles...')
    
    existing_roles = {role.name: role for role in nb_api.dcim.device_roles.all()}
    
    roles_to_create = []
    if config.vm_role and config.vm_role not in existing_roles:
        roles_to_create.append({
            'name': config.vm_role,
            'slug': config.vm_role.lower().replace(' ', '-'),
            'color': ROLE_COLORS['vm'],
            'vm_role': True,
            'description': 'QEMU/KVM Virtual Machine',
        })
    
    if config.lxc_role and config.lxc_role not in existing_roles and config.lxc_role != config.vm_role:
        roles_to_create.append({
            'name': config.lxc_role,
            'slug': config.lxc_role.lower().replace(' ', '-'),
            'color': ROLE_COLORS['lxc'],
            'vm_role': True,
            'description': 'LXC Container',
        })
    
    for role_def in roles_to_create:
        if dry_run:
            logger.info(f'  [DRY RUN] Would create role "{role_def["name"]}"')
            continue
        
        try:
            nb_api.dcim.device_roles.create(
                name=role_def['name'],
                slug=role_def['slug'],
                color=role_def['color'],
                vm_role=role_def['vm_role'],
                description=role_def.get('description', ''),
            )
            logger.info(f'  + Created role "{role_def["name"]}"')
        except Exception as e:
            logger.error(f'  ! Failed to create role "{role_def["name"]}": {e}')


def load_nb_objects(nb_api: pynetbox.api) -> Dict[str, Dict[str, Any]]:
    """Load all NetBox objects needed for synchronization."""
    logger.info('Loading NetBox objects...')
    nb_objects: Dict[str, Dict[str, Any]] = {
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
    for nb_device in nb_api.dcim.devices.all():
        nb_objects['devices'][nb_device.name.lower()] = nb_device

    logger.debug('  - Loading virtual machines...')
    for nb_vm in nb_api.virtualization.virtual_machines.all():
        nb_objects['virtual_machines'][nb_vm.serial] = nb_vm

    logger.debug('  - Loading interfaces...')
    for nb_interface in nb_api.virtualization.interfaces.all():
        if nb_interface.virtual_machine.id not in nb_objects['virtual_machines_interfaces']:
            nb_objects['virtual_machines_interfaces'][nb_interface.virtual_machine.id] = {}
        nb_objects['virtual_machines_interfaces'][nb_interface.virtual_machine.id][nb_interface.name] = nb_interface

    logger.debug('  - Loading MAC addresses...')
    for nb_mac in nb_api.dcim.mac_addresses.all():
        nb_objects['mac_addresses'][nb_mac.mac_address] = nb_mac

    logger.debug('  - Loading prefixes...')
    for nb_prefix in nb_api.ipam.prefixes.all():
        nb_objects['prefixes'][nb_prefix.prefix] = nb_prefix

    logger.debug('  - Loading IP addresses...')
    for nb_ip in nb_api.ipam.ip_addresses.all():
        nb_objects['ip_addresses'][nb_ip['address']] = nb_ip

    logger.debug('  - Loading VLANs...')
    for nb_vlan in nb_api.ipam.vlans.all():
        nb_objects['vlans'][str(nb_vlan.vid)] = nb_vlan

    logger.debug('  - Loading virtual disks...')
    for nb_disk in nb_api.virtualization.virtual_disks.all():
        if nb_disk.virtual_machine.id not in nb_objects['disks']:
            nb_objects['disks'][nb_disk.virtual_machine.id] = {}
        nb_objects['disks'][nb_disk.virtual_machine.id][nb_disk.name] = nb_disk

    logger.debug('  - Loading tags...')
    for nb_tag in nb_api.extras.tags.all():
        nb_objects['tags'][nb_tag.name] = nb_tag

    logger.debug('  - Loading device roles...')
    for nb_role in nb_api.dcim.device_roles.all():
        nb_objects['roles'][nb_role.name] = nb_role
        nb_objects['roles'][str(nb_role.id)] = nb_role

    logger.info('NetBox objects loaded.')
    return nb_objects
