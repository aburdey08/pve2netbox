"""
Which Proxmox guests take part in a sync.

One place for every selection rule, so the full sync, the quick check and
cleanup cannot disagree: a guest hidden from the sync but visible to cleanup
would be deleted from NetBox the moment a filter was turned on.

Node names, pools and tags are matched case-insensitively.
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional, Set, Tuple

from .config import Config, VmidRanges, get_config
from .logger import logger

# Guest type in /cluster/resources; also the name of the per-node endpoint.
QEMU = 'qemu'
LXC = 'lxc'

RULE_SYNC_VMS = 'SYNC_VMS'
RULE_SYNC_LXC = 'SYNC_LXC'
RULE_EXCLUDE_VMIDS = 'EXCLUDE_VMIDS'
RULE_SYNC_NODES = 'SYNC_NODES'
RULE_EXCLUDE_NODES = 'EXCLUDE_NODES'
RULE_SYNC_POOLS = 'SYNC_POOLS'
RULE_EXCLUDE_TAGS = 'EXCLUDE_TAGS'
RULE_INCLUDE_TAGS = 'INCLUDE_TAGS'

RULE_ORDER = (
    RULE_SYNC_VMS,
    RULE_SYNC_LXC,
    RULE_EXCLUDE_VMIDS,
    RULE_SYNC_NODES,
    RULE_EXCLUDE_NODES,
    RULE_SYNC_POOLS,
    RULE_EXCLUDE_TAGS,
    RULE_INCLUDE_TAGS,
)
"""Evaluation and reporting order: the first matching rule is the one logged."""


def split_pve_tags(raw: Optional[str]) -> Tuple[str, ...]:
    """Split the Proxmox ``tags`` string. PVE joins with ``;``; ``,`` is accepted too."""
    if not raw:
        return ()
    return tuple(tag.strip() for tag in raw.replace(',', ';').split(';') if tag.strip())


@dataclass(frozen=True)
class Guest:
    """
    The Proxmox facts the filters need, from whichever endpoint supplied them.

    Only ``/cluster/resources`` knows the pool, hence ``pool`` being optional.
    """
    vmid: int
    node: str
    name: str
    kind: str
    pool: Optional[str] = None
    tags: Tuple[str, ...] = ()

    @classmethod
    def from_cluster_resource(cls, resource: dict) -> 'Guest':
        """Build a guest from one ``/cluster/resources?type=vm`` entry."""
        vmid = int(resource['vmid'])
        return cls(
            vmid=vmid,
            node=resource.get('node') or '',
            name=resource.get('name') or f'vmid-{vmid}',
            kind=resource.get('type') or QEMU,
            pool=resource.get('pool') or None,
            tags=split_pve_tags(resource.get('tags')),
        )

    @classmethod
    def from_node_entry(
            cls,
            entry: dict,
            node: str,
            kind: str,
            pool: Optional[str] = None,
    ) -> 'Guest':
        """Build a guest from a per-node listing; ``pool`` must come from the caller."""
        vmid = int(entry['vmid'])
        return cls(
            vmid=vmid,
            node=node,
            name=entry.get('name') or f'vmid-{vmid}',
            kind=kind,
            pool=pool,
            tags=split_pve_tags(entry.get('tags')),
        )

    @property
    def label(self) -> str:
        """Identification used in log lines."""
        return f'{self.name} (ID: {self.vmid})'


def _lower_set(values: Iterable[str]) -> FrozenSet[str]:
    """Case-folded set used for matching."""
    return frozenset(value.lower() for value in values)


@dataclass(frozen=True)
class GuestFilters:
    """
    The configured filters, pre-folded for matching.

    ``SYNC_VMS``/``SYNC_LXC`` are in here too: routing a disabled guest type
    through the same path is what stops ``ENABLE_CLEANUP`` from deleting every
    container when ``SYNC_LXC=false``.
    """
    sync_vms: bool = True
    sync_lxc: bool = True
    sync_nodes: FrozenSet[str] = frozenset()
    exclude_nodes: FrozenSet[str] = frozenset()
    sync_pools: FrozenSet[str] = frozenset()
    include_tags: FrozenSet[str] = frozenset()
    exclude_tags: FrozenSet[str] = frozenset()
    exclude_vmids: VmidRanges = VmidRanges()

    @classmethod
    def from_config(cls, config: Config) -> 'GuestFilters':
        """Build the matcher from the loaded configuration."""
        return cls(
            sync_vms=config.sync_vms,
            sync_lxc=config.sync_lxc,
            sync_nodes=_lower_set(config.sync_nodes),
            exclude_nodes=_lower_set(config.exclude_nodes),
            sync_pools=_lower_set(config.sync_pools),
            include_tags=_lower_set(config.include_tags),
            exclude_tags=_lower_set(config.exclude_tags),
            exclude_vmids=config.exclude_vmids,
        )

    @property
    def active(self) -> bool:
        """True when at least one filter is configured."""
        return bool(
            not self.sync_vms or not self.sync_lxc
            or self.sync_nodes or self.exclude_nodes or self.sync_pools
            or self.include_tags or self.exclude_tags or self.exclude_vmids
        )

    def kind_reason(self, kind: str) -> Optional[str]:
        """Rule excluding a whole guest type, or ``None``. Lets a caller skip a listing."""
        if kind == QEMU and not self.sync_vms:
            return RULE_SYNC_VMS
        if kind == LXC and not self.sync_lxc:
            return RULE_SYNC_LXC
        return None

    def node_reason(self, node_name: str) -> Optional[str]:
        """
        Rule excluding a whole node, or ``None`` — checked before any per-node call.

        An empty node name (unknown placement) is never excluded.
        """
        if not node_name:
            return None
        node = node_name.lower()
        if self.sync_nodes and node not in self.sync_nodes:
            return RULE_SYNC_NODES
        if node in self.exclude_nodes:
            return RULE_EXCLUDE_NODES
        return None

    def reason(self, guest: Guest) -> Optional[str]:
        """Name of the first rule that excludes ``guest``, or ``None``."""
        kind_reason = self.kind_reason(guest.kind)
        if kind_reason is not None:
            return kind_reason

        if guest.vmid in self.exclude_vmids:
            return RULE_EXCLUDE_VMIDS

        node_reason = self.node_reason(guest.node)
        if node_reason is not None:
            return node_reason

        if self.sync_pools and (guest.pool or '').lower() not in self.sync_pools:
            return RULE_SYNC_POOLS

        tags = _lower_set(guest.tags)
        if self.exclude_tags & tags:
            return RULE_EXCLUDE_TAGS
        if self.include_tags and not (self.include_tags & tags):
            return RULE_INCLUDE_TAGS

        return None


class FilterDecisions:
    """
    Filter verdicts for one sync pass, memoised by VMID.

    The verdict is reached once from the cluster-wide listing (the only source
    that knows the pool) and reused by the per-node loops, which see less. Also
    holds the summary counters and the excluded VMIDs cleanup must not touch.
    """

    def __init__(self, filters: GuestFilters):
        self.filters = filters
        self._verdicts: Dict[int, Optional[str]] = {}
        self.counts: Dict[str, int] = {}

    def evaluate(self, guest: Guest) -> Optional[str]:
        """Rule excluding ``guest``, or ``None``; counted and logged once per VMID."""
        cached = self._verdicts.get(guest.vmid, _UNSET)
        if cached is not _UNSET:
            return cached  # type: ignore[return-value]

        reason = self.filters.reason(guest)
        self._verdicts[guest.vmid] = reason
        if reason is not None:
            self.counts[reason] = self.counts.get(reason, 0) + 1
            logger.debug(f'    Filtered out {guest.label} on {guest.node or "?"}: {reason}')
        return reason

    def is_excluded(self, guest: Guest) -> bool:
        """True when ``guest`` is excluded by a filter."""
        return self.evaluate(guest) is not None

    @property
    def excluded_vmids(self) -> Set[int]:
        """VMIDs excluded so far — the set cleanup must leave alone."""
        return {vmid for vmid, reason in self._verdicts.items() if reason is not None}

    @property
    def excluded_count(self) -> int:
        """How many guests have been excluded so far."""
        return sum(self.counts.values())

    def log_summary(self) -> None:
        """Log what was filtered out and why. Silent when no filter is configured."""
        if not self.filters.active:
            return

        seen = len(self._verdicts)
        excluded = self.excluded_count
        if not excluded:
            logger.info(f'Filters active, no guests excluded ({seen} evaluated)')
            return

        breakdown = ', '.join(
            f'{rule}: {self.counts[rule]}' for rule in RULE_ORDER if rule in self.counts
        )
        logger.info(f'Filters excluded {excluded} of {seen} guest(s) ({breakdown})')


_UNSET = object()
"""Sentinel telling "no verdict yet" apart from a cached ``None`` verdict."""


_cached_config: Optional[Config] = None
_cached_filters: Optional[GuestFilters] = None


def get_filters() -> GuestFilters:
    """Filters for the process-wide config; rebuilt if that config is replaced."""
    global _cached_config, _cached_filters  # pylint: disable=global-statement
    config = get_config()
    if _cached_filters is None or _cached_config is not config:
        _cached_config = config
        _cached_filters = GuestFilters.from_config(config)
    return _cached_filters
