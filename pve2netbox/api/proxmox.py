"""Proxmox API utilities and wrappers."""

from typing import Dict, List, Tuple
from proxmoxer import ProxmoxAPI

from ..config import Config
from ..logger import logger


def create_proxmox_api(config: Config) -> ProxmoxAPI:
    """Create and configure Proxmox API instance."""
    return ProxmoxAPI(
        host=config.pve_api_host,
        user=config.pve_api_user,
        token_name=config.pve_api_token,
        token_value=config.pve_api_secret,
        verify_ssl=config.pve_api_verify_ssl,
    )


def quick_check_changes(pve_api: ProxmoxAPI, last_state: Dict, config: Config) -> Tuple[List[int], Dict]:
    """
    Quick check for VM changes without loading full configuration.
    Collects current state of all VMs (QEMU + LXC), compares to last_state,
    returns (list of changed vmid, current_state).
    """
    current_state = {}
    for pve_node in pve_api.nodes.get():
        node_name = pve_node['node']
        if config.sync_vms:
            try:
                for vm in pve_api.nodes(node_name).qemu.get():
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
        if config.sync_lxc:
            try:
                for ct in pve_api.nodes(node_name).lxc.get():
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
        if vmid not in last_state or last_state[vmid] != data:
            changed_vmids.append(vmid)
    for vmid in last_state:
        if vmid not in current_state:
            changed_vmids.append(vmid)
    
    return changed_vmids, current_state
