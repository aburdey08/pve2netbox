"""Utility functions for parsing and data processing."""

from typing import Dict, Optional


def parse_pve_network_definition(raw_network_definition: str) -> Dict[str, str]:
    """
    Parse Proxmox network interface definition string.
    
    Example: "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=100,mtu=9000"
    Returns: {'virtio': 'AA:BB:CC:DD:EE:FF', 'bridge': 'vmbr0', 'tag': '100', 'mtu': '9000'}
    """
    network_definition = {}
    
    for component in raw_network_definition.split(','):
        component_parts = component.split('=')
        if len(component_parts) == 2:
            network_definition[component_parts[0]] = component_parts[1]
    
    return network_definition


def parse_pve_disk_definition(raw_disk_definition: str) -> Dict[str, str]:
    """
    Parse Proxmox disk definition string.
    
    Example: "local-lvm:vm-100-disk-0,size=32G,backup=1"
    Returns: {'name': 'local-lvm:vm-100-disk-0', 'size': '32G', 'backup': '1'}
    """
    disk_definition = {}
    
    for component in raw_disk_definition.split(','):
        component_parts = component.split('=')
        if len(component_parts) == 1:
            disk_definition['name'] = component_parts[0]
        elif len(component_parts) == 2:
            disk_definition[component_parts[0]] = component_parts[1]
    
    return disk_definition


def parse_pve_disk_size(raw_disk_size: str) -> int:
    """
    Parse Proxmox disk size string to megabytes.
    
    Args:
        raw_disk_size: Size string like '32G', '1024M', '528K', '2T'

    Returns:
        Size in megabytes, or -1 if parsing fails. Kilobytes are rounded up to at least 1 MB (NetBox minimum).
    """
    if not raw_disk_size or len(raw_disk_size) < 2:
        return -1
    try:
        size = raw_disk_size[:-1]
        size_unit = raw_disk_size[-1]
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


def get_virtual_machine_vcpus(pve_vm_config: Dict) -> int:
    """
    Extract vCPU count from Proxmox VM configuration.
    
    Args:
        pve_vm_config: Proxmox VM configuration dictionary
    
    Returns:
        Number of vCPUs
    """
    if 'vcpus' in pve_vm_config:
        return pve_vm_config['vcpus']
    
    return pve_vm_config.get('cores', 1) * pve_vm_config.get('sockets', 1)


def get_mac_address_from_network_definition(network_definition: Dict[str, str]) -> Optional[str]:
    """
    Extract MAC address from Proxmox network definition.
    Supports QEMU models (virtio, e1000) and LXC hwaddr.
    """
    for model in ['virtio', 'e1000']:
        if model in network_definition:
            return network_definition[model]
    if 'hwaddr' in network_definition:
        return network_definition['hwaddr']
    
    return None
