"""
Filter behaviour.

The whole correctness argument of the selection filters is that the full sync,
the quick check and cleanup cannot disagree about a guest. These tests pin the
verdicts and, above all, the protection list cleanup relies on.
"""

import pytest

from pve2netbox.config import VmidRanges
from pve2netbox.filters import (
    LXC,
    QEMU,
    RULE_EXCLUDE_NODES,
    RULE_EXCLUDE_TAGS,
    RULE_EXCLUDE_VMIDS,
    RULE_INCLUDE_TAGS,
    RULE_SYNC_LXC,
    RULE_SYNC_NODES,
    RULE_SYNC_POOLS,
    RULE_SYNC_VMS,
    FilterDecisions,
    Guest,
    GuestFilters,
    get_filters,
    split_pve_tags,
)

from .conftest import make_config


def guest(vmid=100, node='pve1', name='vm', kind=QEMU, pool=None, tags=()):
    return Guest(vmid=vmid, node=node, name=name, kind=kind, pool=pool, tags=tags)


class TestSplitPveTags:
    def test_splits_on_semicolon_and_comma(self):
        assert split_pve_tags('prod;db, web') == ('prod', 'db', 'web')

    def test_drops_empty_fragments(self):
        assert split_pve_tags(';; prod ;') == ('prod',)

    @pytest.mark.parametrize('raw', [None, '', '  '])
    def test_empty_input(self, raw):
        assert split_pve_tags(raw) == ()


class TestGuest:
    def test_from_cluster_resource(self):
        g = Guest.from_cluster_resource(
            {'vmid': '101', 'node': 'pve2', 'name': 'web', 'type': LXC,
             'pool': 'prod', 'tags': 'a;b'})
        assert (g.vmid, g.node, g.kind, g.pool, g.tags) == (101, 'pve2', LXC, 'prod', ('a', 'b'))

    def test_missing_name_falls_back_to_vmid(self):
        assert Guest.from_cluster_resource({'vmid': 7}).name == 'vmid-7'

    def test_empty_pool_is_none(self):
        # An empty pool must not become a meaningless "Pool/" tag downstream.
        assert Guest.from_cluster_resource({'vmid': 7, 'pool': ''}).pool is None

    def test_from_node_entry_takes_pool_from_caller(self):
        g = Guest.from_node_entry({'vmid': 5, 'name': 'x'}, 'pve1', QEMU, pool='dmz')
        assert g.pool == 'dmz'
        assert g.node == 'pve1'


class TestRules:
    def test_no_filters_passes_everything(self):
        assert GuestFilters().reason(guest()) is None

    def test_disabled_kind_is_a_filter(self):
        assert GuestFilters(sync_lxc=False).reason(guest(kind=LXC)) == RULE_SYNC_LXC
        assert GuestFilters(sync_vms=False).reason(guest(kind=QEMU)) == RULE_SYNC_VMS

    def test_sync_nodes_excludes_everything_else(self):
        f = GuestFilters(sync_nodes=frozenset({'pve1'}))
        assert f.reason(guest(node='pve1')) is None
        assert f.reason(guest(node='pve9')) == RULE_SYNC_NODES

    def test_exclude_nodes(self):
        f = GuestFilters(exclude_nodes=frozenset({'pve9'}))
        assert f.reason(guest(node='pve9')) == RULE_EXCLUDE_NODES

    def test_node_matching_is_case_insensitive(self):
        f = GuestFilters(sync_nodes=frozenset({'pve1'}))
        assert f.reason(guest(node='PVE1')) is None

    def test_unknown_placement_is_never_excluded_by_node(self):
        # An empty node name means "we do not know"; guessing would drop the guest.
        assert GuestFilters(sync_nodes=frozenset({'pve1'})).reason(guest(node='')) is None

    def test_sync_pools_excludes_guests_without_a_pool(self):
        f = GuestFilters(sync_pools=frozenset({'prod'}))
        assert f.reason(guest(pool='prod')) is None
        assert f.reason(guest(pool=None)) == RULE_SYNC_POOLS

    def test_exclude_tags_beats_include_tags(self):
        f = GuestFilters(include_tags=frozenset({'netbox'}), exclude_tags=frozenset({'skip'}))
        assert f.reason(guest(tags=('netbox', 'skip'))) == RULE_EXCLUDE_TAGS

    def test_include_tags_requires_one_match(self):
        f = GuestFilters(include_tags=frozenset({'netbox'}))
        assert f.reason(guest(tags=('NetBox',))) is None
        assert f.reason(guest(tags=('other',))) == RULE_INCLUDE_TAGS

    def test_tags_match_whole_words_only(self):
        f = GuestFilters(exclude_tags=frozenset({'db'}))
        assert f.reason(guest(tags=('dbserver',))) is None

    def test_exclude_vmids_range(self):
        f = GuestFilters(exclude_vmids=VmidRanges(ranges=((900, 999),)))
        assert f.reason(guest(vmid=950)) == RULE_EXCLUDE_VMIDS
        assert f.reason(guest(vmid=899)) is None

    def test_first_matching_rule_is_reported(self):
        # Kind is checked before everything else, so the summary blames the
        # most specific reason rather than the last one checked.
        f = GuestFilters(sync_lxc=False, exclude_nodes=frozenset({'pve1'}))
        assert f.reason(guest(kind=LXC, node='pve1')) == RULE_SYNC_LXC

    def test_active_reflects_any_configured_filter(self):
        assert not GuestFilters().active
        assert GuestFilters(sync_lxc=False).active
        assert GuestFilters(exclude_tags=frozenset({'x'})).active
        assert GuestFilters(exclude_vmids=VmidRanges(singles=frozenset({1}))).active

    def test_kind_and_node_shortcuts_agree_with_reason(self):
        f = GuestFilters(sync_lxc=False, exclude_nodes=frozenset({'pve9'}))
        assert f.kind_reason(LXC) == RULE_SYNC_LXC
        assert f.kind_reason(QEMU) is None
        assert f.node_reason('pve9') == RULE_EXCLUDE_NODES
        assert f.node_reason('') is None


class TestFilterDecisions:
    def test_counts_and_protection_list(self):
        d = FilterDecisions(GuestFilters(exclude_tags=frozenset({'skip'})))
        assert not d.is_excluded(guest(vmid=1))
        assert d.is_excluded(guest(vmid=2, tags=('skip',)))
        assert d.excluded_vmids == {2}
        assert d.counts == {RULE_EXCLUDE_TAGS: 1}
        assert d.excluded_count == 1

    def test_verdict_is_memoised_per_vmid(self):
        # The per-node loops re-ask with a weaker guest (no pool); the answer
        # must stay the one reached from /cluster/resources.
        d = FilterDecisions(GuestFilters(sync_pools=frozenset({'prod'})))
        assert not d.is_excluded(guest(vmid=3, pool='prod'))
        assert not d.is_excluded(guest(vmid=3, pool=None))
        assert d.excluded_vmids == set()

    def test_counted_once_per_vmid(self):
        d = FilterDecisions(GuestFilters(exclude_tags=frozenset({'skip'})))
        d.is_excluded(guest(vmid=4, tags=('skip',)))
        d.is_excluded(guest(vmid=4, tags=('skip',)))
        assert d.counts == {RULE_EXCLUDE_TAGS: 1}

    def test_disabled_kind_lands_in_the_protection_list(self):
        # This is what stops ENABLE_CLEANUP from wiping every container
        # the moment somebody sets SYNC_LXC=false.
        d = FilterDecisions(GuestFilters(sync_lxc=False))
        d.is_excluded(guest(vmid=5, kind=LXC))
        assert d.excluded_vmids == {5}

    def test_summary_is_silent_without_filters(self, caplog):
        FilterDecisions(GuestFilters()).log_summary()
        assert caplog.records == []

    def test_summary_names_each_rule(self, caplog):
        d = FilterDecisions(GuestFilters(exclude_tags=frozenset({'skip'})))
        d.is_excluded(guest(vmid=6, tags=('skip',)))
        d.is_excluded(guest(vmid=7))
        with caplog.at_level('INFO'):
            d.log_summary()
        assert 'excluded 1 of 2' in caplog.text
        assert 'EXCLUDE_TAGS: 1' in caplog.text


class TestGetFilters:
    def test_rebuilt_when_the_config_object_is_replaced(self):
        from pve2netbox.config import set_config

        set_config(make_config(exclude_tags=('a',)))
        assert get_filters().exclude_tags == frozenset({'a'})
        set_config(make_config(exclude_tags=('b',)))
        assert get_filters().exclude_tags == frozenset({'b'})

    def test_folds_config_case(self):
        from pve2netbox.config import set_config

        set_config(make_config(sync_nodes=('PVE1',), include_tags=('NetBox',)))
        filters = get_filters()
        assert filters.sync_nodes == frozenset({'pve1'})
        assert filters.include_tags == frozenset({'netbox'})
