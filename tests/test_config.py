"""Configuration parsing: the filter lists and the VMID ranges behind them."""

import pytest

from pve2netbox.config import (
    VmidRanges,
    _contradicting_filters,
    describe_filters,
    parse_name_list,
    parse_vmid_ranges,
)

from .conftest import make_config


class TestParseVmidRanges:
    def test_singles_and_ranges(self):
        parsed = parse_vmid_ranges('100,105,900-999')
        assert parsed.singles == frozenset({100, 105})
        assert parsed.ranges == ((900, 999),)

    def test_whitespace_separators(self):
        assert parse_vmid_ranges('100 105').singles == frozenset({100, 105})

    def test_spaces_around_the_dash_still_mean_a_range(self):
        # "900 - 999" is what people type; it must not become two single IDs.
        assert parse_vmid_ranges('900 - 999').ranges == ((900, 999),)
        assert parse_vmid_ranges('900 - 999').singles == frozenset()

    def test_reversed_range_is_normalised(self):
        assert parse_vmid_ranges('999-900').ranges == ((900, 999),)

    def test_membership(self):
        parsed = parse_vmid_ranges('100,900-999')
        assert 100 in parsed
        assert 950 in parsed
        assert 899 not in parsed
        assert 'x' not in parsed

    def test_malformed_entries_are_reported_not_ignored(self):
        # A typo in an exclusion list means guests get synced that the operator
        # believed were excluded, so it has to be a configuration error.
        errors = []
        parsed = parse_vmid_ranges('abc, 100', errors)
        assert parsed.singles == frozenset({100})
        assert len(errors) == 1
        assert 'abc' in errors[0]

    @pytest.mark.parametrize('raw', [None, '', '   '])
    def test_empty(self, raw):
        assert not parse_vmid_ranges(raw)

    def test_str_round_trip(self):
        assert str(parse_vmid_ranges('105,100,900-999')) == '100, 105, 900-999'

    def test_bool(self):
        assert not VmidRanges()
        assert VmidRanges(singles=frozenset({1}))
        assert VmidRanges(ranges=((1, 2),))


class TestParseNameList:
    def test_commas_and_whitespace(self):
        assert parse_name_list('a, b c') == ('a', 'b', 'c')

    def test_duplicates_dropped_case_insensitively_keeping_first_spelling(self):
        assert parse_name_list('Prod, prod, PROD') == ('Prod',)

    def test_order_preserved(self):
        assert parse_name_list('c,a,b') == ('c', 'a', 'b')

    @pytest.mark.parametrize('raw', [None, '', '  '])
    def test_empty(self, raw):
        assert parse_name_list(raw) == ()


class TestContradictingFilters:
    def test_node_named_in_both_lists(self):
        problems = _contradicting_filters(('pve1',), ('PVE1',), (), ())
        assert len(problems) == 1
        assert 'SYNC_NODES and EXCLUDE_NODES' in problems[0]

    def test_tag_named_in_both_lists(self):
        problems = _contradicting_filters((), (), ('x',), ('X',))
        assert len(problems) == 1
        assert 'INCLUDE_TAGS and EXCLUDE_TAGS' in problems[0]

    def test_disjoint_lists_are_fine(self):
        assert _contradicting_filters(('a',), ('b',), ('c',), ('d',)) == []


class TestDescribeFilters:
    def test_empty_when_nothing_is_filtered(self):
        assert describe_filters(make_config()) == []

    def test_lists_what_was_configured(self):
        described = dict(describe_filters(make_config(
            sync_nodes=('pve1', 'pve2'),
            exclude_vmids=parse_vmid_ranges('900-999'),
        )))
        assert described['Filter SYNC_NODES'] == 'pve1, pve2'
        assert described['Filter EXCLUDE_VMIDS'] == '900-999'
