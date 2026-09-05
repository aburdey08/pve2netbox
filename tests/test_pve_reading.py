"""
Reading guests out of Proxmox: the /cluster/resources path, the per-node
fallback, and what the quick check decides is a change.
"""

import types

import pytest

from pve2netbox import _collect_pve_guest_metadata, _read_pve_guests, _select_pve_nodes
from pve2netbox.api.proxmox import (
    QUICK_CHECK_SOURCE_CLUSTER,
    QUICK_CHECK_SOURCE_NODES,
    UNKNOWN_STATUS,
    _resolve_status,
    detect_quick_check_source,
    quick_check_changes,
)
from pve2netbox.filters import FilterDecisions, GuestFilters

from .conftest import make_config


class FakeListing:
    def __init__(self, entries):
        self.entries = entries

    def get(self):
        return list(self.entries)


class FakeNode:
    def __init__(self, guests):
        self.qemu = FakeListing(guests.get('qemu', []))
        self.lxc = FakeListing(guests.get('lxc', []))


class FakeNodes:
    def __init__(self, guests_by_node):
        self.guests_by_node = guests_by_node

    def get(self):
        return [{'node': name, 'status': 'online'} for name in self.guests_by_node]

    def __call__(self, name):
        return FakeNode(self.guests_by_node.get(name, {}))


class FakeResources:
    def __init__(self, resources, error=None):
        self.resources = resources
        self.error = error

    def get(self, **_params):
        if self.error is not None:
            raise self.error
        return list(self.resources)


class FakeProxmox:
    def __init__(self, guests_by_node, resources=None, resources_error=None):
        self.nodes = FakeNodes(guests_by_node)
        self.cluster = types.SimpleNamespace(
            resources=FakeResources(resources or [], resources_error))


GUESTS_BY_NODE = {
    'pve1': {'qemu': [{'vmid': 100, 'name': 'web', 'status': 'running'}]},
    'pve9': {'qemu': [{'vmid': 900, 'name': 'lab', 'status': 'running'}]},
}

RESOURCES = [
    {'vmid': 100, 'name': 'web', 'node': 'pve1', 'type': 'qemu',
     'pool': 'prod', 'status': 'running'},
    {'vmid': 900, 'name': 'lab', 'node': 'pve9', 'type': 'qemu', 'status': 'running'},
]


class TestReadPveGuests:
    def test_cluster_resources_knows_pools(self):
        guests, pools_known = _read_pve_guests(FakeProxmox(GUESTS_BY_NODE, RESOURCES))
        assert pools_known
        assert {g.vmid: g.pool for g, _ in guests} == {100: 'prod', 900: None}

    def test_falls_back_to_node_listings(self, caplog):
        pve = FakeProxmox(GUESTS_BY_NODE, resources_error=RuntimeError('403'))
        with caplog.at_level('WARNING'):
            guests, pools_known = _read_pve_guests(pve)
        assert not pools_known
        assert {g.vmid for g, _ in guests} == {100, 900}
        assert 'per-node listings' in caplog.text

    def test_fallback_walks_even_excluded_nodes(self):
        # A guest on a skipped node still has to be evaluated, or cleanup will
        # not know it exists and will delete its NetBox record.
        pve = FakeProxmox(GUESTS_BY_NODE, resources_error=RuntimeError('403'))
        guests, _ = _read_pve_guests(pve)
        assert {g.node for g, _ in guests} == {'pve1', 'pve9'}


class TestCollectMetadata:
    def test_pool_becomes_a_tag(self, config):
        config(sync_tags=False, template_policy='sync')
        decisions = FilterDecisions(GuestFilters())
        tags, pools, templates = _collect_pve_guest_metadata(
            FakeProxmox(GUESTS_BY_NODE, RESOURCES), None, {}, decisions)
        assert pools == {100: 'prod'}
        assert tags[100] == ['Pool/prod']
        assert tags[900] == []
        assert templates == set()

    def test_excluded_guests_are_protected_from_cleanup(self, config):
        config(sync_tags=False, template_policy='sync', exclude_nodes=('pve9',))
        decisions = FilterDecisions(GuestFilters(exclude_nodes=frozenset({'pve9'})))
        tags, _, _ = _collect_pve_guest_metadata(
            FakeProxmox(GUESTS_BY_NODE, RESOURCES), None, {}, decisions)
        assert 900 not in tags
        assert decisions.excluded_vmids == {900}

    def test_sync_pools_without_pool_information_stops_the_sync(self, config):
        # Carrying on would match no guest at all and empty the sync.
        config(sync_tags=False, sync_pools=('prod',))
        decisions = FilterDecisions(GuestFilters(sync_pools=frozenset({'prod'})))
        pve = FakeProxmox(GUESTS_BY_NODE, resources_error=RuntimeError('403'))
        with pytest.raises(RuntimeError, match='SYNC_POOLS'):
            _collect_pve_guest_metadata(pve, None, {}, decisions)

    def test_only_vmids_narrows_metadata_but_not_the_verdicts(self, config):
        config(sync_tags=False, template_policy='sync', exclude_nodes=('pve9',))
        decisions = FilterDecisions(GuestFilters(exclude_nodes=frozenset({'pve9'})))
        tags, _, _ = _collect_pve_guest_metadata(
            FakeProxmox(GUESTS_BY_NODE, RESOURCES), None, {}, decisions, {100})
        assert set(tags) == {100}
        # ...and the guest a quick sync is not touching is still protected.
        assert decisions.excluded_vmids == {900}


class TestSelectPveNodes:
    def test_drops_excluded_nodes(self):
        decisions = FilterDecisions(GuestFilters(exclude_nodes=frozenset({'pve9'})))
        nodes = _select_pve_nodes(FakeProxmox(GUESTS_BY_NODE), decisions)
        assert [n['node'] for n in nodes] == ['pve1']

    def test_quiet_keeps_the_skip_off_the_info_log(self, caplog):
        decisions = FilterDecisions(GuestFilters(exclude_nodes=frozenset({'pve9'})))
        with caplog.at_level('INFO'):
            _select_pve_nodes(FakeProxmox(GUESTS_BY_NODE), decisions, _quiet=True)
        assert 'Skipping node' not in caplog.text


class TestResolveStatus:
    def test_plain_status_is_used(self):
        cfg = make_config()
        assert _resolve_status({'status': 'running'}, {}, cfg) == 'running'

    def test_unknown_reuses_the_last_known_status(self):
        # /cluster/resources reports "unknown" for guests of an unreachable
        # node; it says nothing about the guest.
        cfg = make_config()
        assert _resolve_status(
            {'status': UNKNOWN_STATUS}, {'status': 'running'}, cfg) == 'running'

    def test_unknown_without_history_is_kept(self):
        cfg = make_config()
        assert _resolve_status({'status': UNKNOWN_STATUS}, {}, cfg) == UNKNOWN_STATUS

    def test_transient_lock_reuses_the_last_known_status(self):
        cfg = make_config(ignore_status_when_locked=True)
        entity = {'status': 'running', 'lock': 'backup'}
        assert _resolve_status(entity, {'status': 'stopped'}, cfg) == 'stopped'

    def test_lock_handling_can_be_switched_off(self):
        cfg = make_config(ignore_status_when_locked=False)
        entity = {'status': 'running', 'lock': 'backup'}
        assert _resolve_status(entity, {'status': 'stopped'}, cfg) == 'running'


class TestQuickCheck:
    def test_detects_the_cluster_source(self):
        assert detect_quick_check_source(
            FakeProxmox(GUESTS_BY_NODE, RESOURCES)) == QUICK_CHECK_SOURCE_CLUSTER

    def test_falls_back_to_nodes(self, caplog):
        pve = FakeProxmox(GUESTS_BY_NODE, resources_error=RuntimeError('403'))
        with caplog.at_level('WARNING'):
            assert detect_quick_check_source(pve) == QUICK_CHECK_SOURCE_NODES

    def test_filtered_guests_never_appear_as_changes(self):
        cfg = make_config(exclude_nodes=('pve9',))
        filters = GuestFilters(exclude_nodes=frozenset({'pve9'}))
        changed, state = quick_check_changes(
            FakeProxmox(GUESTS_BY_NODE, RESOURCES), {}, cfg,
            QUICK_CHECK_SOURCE_CLUSTER, filters)
        assert set(state) == {100}
        assert changed == [100]

    def test_unchanged_state_reports_nothing(self):
        cfg = make_config()
        pve = FakeProxmox(GUESTS_BY_NODE, RESOURCES)
        _, first = quick_check_changes(pve, {}, cfg, QUICK_CHECK_SOURCE_CLUSTER, GuestFilters())
        changed, _ = quick_check_changes(
            pve, first, cfg, QUICK_CHECK_SOURCE_CLUSTER, GuestFilters())
        assert changed == []

    def test_a_vanished_guest_is_a_change(self):
        cfg = make_config()
        pve = FakeProxmox(GUESTS_BY_NODE, RESOURCES)
        _, first = quick_check_changes(pve, {}, cfg, QUICK_CHECK_SOURCE_CLUSTER, GuestFilters())
        pve.cluster.resources.resources = RESOURCES[:1]
        changed, _ = quick_check_changes(
            pve, first, cfg, QUICK_CHECK_SOURCE_CLUSTER, GuestFilters())
        assert changed == [900]

    def test_node_source_skips_excluded_nodes(self):
        cfg = make_config(exclude_nodes=('pve9',))
        filters = GuestFilters(exclude_nodes=frozenset({'pve9'}))
        _, state = quick_check_changes(
            FakeProxmox(GUESTS_BY_NODE), {}, cfg, QUICK_CHECK_SOURCE_NODES, filters)
        assert set(state) == {100}
        assert state[100]['pool'] is None
