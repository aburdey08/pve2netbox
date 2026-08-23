"""
IP address discovery for LXC containers.

QEMU VMs report their addresses through the guest agent; containers have no
agent, so until 1.1.0 their interfaces were synced without any IP. Proxmox
exposes the same information in two other places:

* **runtime** — ``GET /nodes/{node}/lxc/{vmid}/interfaces`` lists the addresses a
  *running* container actually holds, which is the only source that works for
  DHCP;
* **config** — the ``ip=``/``ip6=`` keys of ``net0``, ``net1``… hold the static
  addresses and are readable whether or not the container runs.

Both are converted to the exact structure produced by the QEMU guest-agent
branch::

    {'bc:24:11:aa:bb:cc': {'interface_name': 'eth0',
                           'ip_addresses': [{'address': '10.0.0.5',
                                             'prefix': 24,
                                             'type': 'ipv4'}]}}

Keeping the format identical is the point: interface processing and
``PRIMARY_SUBNETS`` resolution are then shared with QEMU instead of duplicated.
"""

import ipaddress
from typing import Any, Dict, List, Optional

from .logger import logger
from .utils import parse_pve_network_definition

NON_ADDRESS_VALUES = frozenset({'dhcp', 'manual', 'auto', 'none', ''})
"""``ip=``/``ip6=`` values that describe a method rather than an address."""


def build_lxc_agent_data(
        pve_api: Any,
        node: str,
        vmid: int,
        container_config: Dict[str, Any],
        is_running: bool,
        source: str = 'auto',
) -> Dict[str, Dict[str, Any]]:
    """
    Collect container IP addresses in QEMU guest-agent format.

    Args:
        pve_api: Proxmox API client.
        node: Proxmox node hosting the container.
        vmid: Container ID.
        container_config: Result of ``lxc(vmid).config.get()``.
        is_running: Whether the container is currently running.
        source: ``LXC_IP_SOURCE`` — ``auto``, ``runtime``, ``config`` or ``none``.

    Returns:
        Mapping of lowercase MAC address to interface name and IP addresses.
        Empty when the source is ``none`` or nothing could be determined.
    """
    if source == 'none':
        return {}

    config_data = {} if source == 'runtime' else _agent_data_from_config(container_config)

    if source == 'config' or not is_running:
        if source in ('auto', 'runtime') and not is_running:
            logger.debug('      LXC is not running, using static IPs from the container config')
        return config_data

    runtime_data: Dict[str, Dict[str, Any]] = {}
    try:
        interfaces = pve_api.nodes(node).lxc(vmid).interfaces.get()
        runtime_data = _agent_data_from_runtime(interfaces)
    except Exception as e:  # pylint: disable=broad-except
        # Older PVE versions and tokens without VM.Audit fail here. A container
        # must still sync — with its static addresses when we have them.
        logger.warning(
            f'      Warning: failed to read LXC interfaces from Proxmox: {e}'
        )
        if source == 'runtime':
            return {}
        return config_data

    if source == 'runtime':
        return runtime_data

    # auto: runtime wins per MAC, config fills in interfaces it did not report.
    merged = dict(config_data)
    merged.update(runtime_data)
    return merged


def _agent_data_from_config(container_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build agent-format data from the static ``ip=``/``ip6=`` keys of ``netN``."""
    agent_data: Dict[str, Dict[str, Any]] = {}

    for config_key, config_value in container_config.items():
        if not config_key.startswith('net') or not isinstance(config_value, str):
            continue

        network_definition = parse_pve_network_definition(config_value)
        mac_address = network_definition.get('hwaddr')
        if not mac_address:
            continue

        interface_name = network_definition.get('name', config_key)
        if interface_name == 'lo':
            continue

        ip_addresses = []
        for key in ('ip', 'ip6'):
            parsed = _parse_ip_with_prefix(network_definition.get(key))
            if parsed is not None:
                ip_addresses.append(parsed)

        if not ip_addresses:
            # An interface configured as ``ip=dhcp`` says nothing about which
            # address it holds. Reporting it with an empty list would make the
            # shared interface handler treat NetBox's addresses as stale and
            # delete them every time the container is stopped.
            continue

        agent_data[mac_address.lower()] = {
            'interface_name': interface_name,
            'ip_addresses': ip_addresses,
        }

    return agent_data


def _agent_data_from_runtime(interfaces: Any) -> Dict[str, Dict[str, Any]]:
    """Build agent-format data from ``/nodes/{node}/lxc/{vmid}/interfaces``."""
    agent_data: Dict[str, Dict[str, Any]] = {}

    for interface in interfaces or []:
        if not isinstance(interface, dict):
            continue
        interface_name = interface.get('name')
        if not interface_name or interface_name == 'lo':
            continue
        mac_address = (interface.get('hwaddr') or '').lower()
        if not mac_address:
            continue

        ip_addresses = []
        for key in ('inet', 'inet6'):
            for raw_address in _split_addresses(interface.get(key)):
                parsed = _parse_ip_with_prefix(raw_address)
                if parsed is not None:
                    ip_addresses.append(parsed)

        agent_data[mac_address] = {
            'interface_name': interface_name,
            'ip_addresses': ip_addresses,
        }

    return agent_data


def _split_addresses(raw: Any) -> List[str]:
    """Split a PVE ``inet``/``inet6`` value; several addresses may share one field."""
    if not raw or not isinstance(raw, str):
        return []
    return [token for token in raw.replace(',', ' ').split() if token]


def _parse_ip_with_prefix(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Parse ``10.0.0.5/24`` into ``{'address', 'prefix', 'type'}``.

    Returns ``None`` for method keywords (``dhcp``, ``manual``, ``auto``), for
    values that do not parse, and for addresses NetBox has no business storing:
    loopback and link-local (a container's ``fe80::`` address carries no
    inventory value and would only churn the changelog).
    """
    if not raw or not isinstance(raw, str):
        return None

    value = raw.strip()
    if value.lower() in NON_ADDRESS_VALUES:
        return None

    try:
        interface = ipaddress.ip_interface(value)
    except ValueError:
        logger.debug(f'        Ignoring unparsable LXC address "{raw}"')
        return None

    address = interface.ip
    if address.is_loopback or address.is_link_local or address.is_unspecified:
        return None

    return {
        'address': str(address),
        'prefix': interface.network.prefixlen,
        'type': 'ipv4' if address.version == 4 else 'ipv6',
    }
