"""Proxmox API utilities and wrappers."""

from typing import Dict, List, Optional, Tuple
from proxmoxer import ProxmoxAPI

from ..config import Config, TRANSIENT_PVE_LOCKS
from ..filters import LXC, QEMU, Guest, GuestFilters
from ..logger import logger

QUICK_CHECK_SOURCE_CLUSTER = 'cluster'
"""One request for the whole cluster, and the only source of ``pool``."""

QUICK_CHECK_SOURCE_NODES = 'nodes'
"""Fallback: ``2 x N`` requests, no pool information."""

UNKNOWN_STATUS = 'unknown'
"""Reported for guests of an unreachable node — says nothing about the guest."""


def create_proxmox_api(config: Config) -> ProxmoxAPI:
    """Create and configure Proxmox API instance."""
    return ProxmoxAPI(
        host=config.pve_api_host,
        user=config.pve_api_user,
        token_name=config.pve_api_token,
        token_value=config.pve_api_secret,
        verify_ssl=config.pve_api_verify_ssl,
    )


def detect_quick_check_source(pve_api: ProxmoxAPI) -> str:
    """
    Decide once, at startup, how the quick check will read guest state.

    Probed once rather than per cycle on purpose: a ``try/except`` every cycle
    changes behaviour halfway through a run and hides the reason (see the
    legacy quick-check fallback removed in 1.0.8).
    """
    try:
        pve_api.cluster.resources.get(type='vm')
        return QUICK_CHECK_SOURCE_CLUSTER
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(
            f'/cluster/resources is not available ({e}); quick check falls back to '
            f'per-node listings. Pool changes will only be picked up by the full sync.'
        )
        return QUICK_CHECK_SOURCE_NODES


def _resolve_status(entity: dict, prev: Dict, config: Config) -> str:
    """
    Status to track, ignoring values known to be noise.

    A transient ``lock`` (a backup briefly starts a helper QEMU for a stopped
    VM) and a node's ``unknown`` both reuse the last observed status.
    """
    status = entity.get('status', UNKNOWN_STATUS)
    prev_status = prev.get('status') if isinstance(prev, dict) else None
    if prev_status is None:
        return status

    if status == UNKNOWN_STATUS:
        return prev_status
    if config.ignore_status_when_locked and entity.get('lock') in TRANSIENT_PVE_LOCKS:
        return prev_status
    return status


def _tracked_state(guest: Guest, entity: dict, prev: Dict, config: Config) -> Dict:
    """
    The per-guest state compared between cycles.

    ``pool`` and ``tags`` are in it so a retag or pool move is caught within one
    interval. On the fallback path ``pool`` is always ``None``, so its absence
    is stable and cannot cause churn.
    """
    return {
        'type': guest.kind,
        'status': _resolve_status(entity, prev, config),
        'name': guest.name,
        'node': guest.node,
        'maxmem': entity.get('maxmem', 0),
        'maxdisk': entity.get('maxdisk', 0),
        'pool': guest.pool,
        'tags': guest.tags,
    }


def _collect_from_cluster(
        pve_api: ProxmoxAPI,
        last_state: Dict,
        config: Config,
        guest_filters: GuestFilters,
) -> Dict:
    """Collect guest state for the whole cluster with one API request."""
    current_state: Dict = {}
    for resource in pve_api.cluster.resources.get(type='vm'):
        guest = Guest.from_cluster_resource(resource)
        if guest_filters.reason(guest) is not None:
            continue
        current_state[guest.vmid] = _tracked_state(
            guest, resource, last_state.get(guest.vmid, {}), config)
    return current_state


def _collect_from_nodes(
        pve_api: ProxmoxAPI,
        last_state: Dict,
        config: Config,
        guest_filters: GuestFilters,
) -> Dict:
    """Collect guest state by walking every node — ``2 x N`` requests, no pools."""
    current_state: Dict = {}
    for pve_node in pve_api.nodes.get():
        node_name = pve_node['node']
        if guest_filters.node_reason(node_name) is not None:
            continue
        for kind, endpoint in ((QEMU, 'qemu'), (LXC, 'lxc')):
            if guest_filters.kind_reason(kind) is not None:
                continue
            try:
                entities = getattr(pve_api.nodes(node_name), endpoint).get()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(f'Failed to get {endpoint} guests from node {node_name}: {e}')
                continue
            for entity in entities:
                guest = Guest.from_node_entry(entity, node_name, kind)
                if guest_filters.reason(guest) is not None:
                    continue
                current_state[guest.vmid] = _tracked_state(
                    guest, entity, last_state.get(guest.vmid, {}), config)
    return current_state


def quick_check_changes(
        pve_api: ProxmoxAPI,
        last_state: Dict,
        config: Config,
        source: str = QUICK_CHECK_SOURCE_CLUSTER,
        guest_filters: Optional[GuestFilters] = None,
) -> Tuple[List[int], Dict]:
    """
    Quick check for guest changes without loading full configuration.

    Compares current guest state against ``last_state`` and returns
    ``(changed vmids, current state)``. Filtered guests are left out of the
    state entirely, so one never shows up as a change.

    ``source`` comes from :func:`detect_quick_check_source`; ``guest_filters``
    is derived from ``config`` when omitted.
    """
    if guest_filters is None:
        guest_filters = GuestFilters.from_config(config)

    if source == QUICK_CHECK_SOURCE_NODES:
        current_state = _collect_from_nodes(pve_api, last_state, config, guest_filters)
    else:
        current_state = _collect_from_cluster(pve_api, last_state, config, guest_filters)

    changed_vmids = []
    for vmid, data in current_state.items():
        if vmid not in last_state or last_state[vmid] != data:
            changed_vmids.append(vmid)
    for vmid in last_state:
        if vmid not in current_state:
            changed_vmids.append(vmid)

    return changed_vmids, current_state
