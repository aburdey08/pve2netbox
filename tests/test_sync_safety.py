"""
The guards that stand between a failed NetBox read and duplicated or deleted
records. Everything here is about what the sync must *not* do when an answer
from NetBox is missing, partial, or not the one that was asked for.
"""

import types

import pytest

from pve2netbox import (
    NB_FILTER_CHUNK_SIZE,
    _batched_serials_honoured,
    _empty_nb_objects,
    _fetch_cluster_scoped,
    _fetch_filtered,
    _fetch_for_vms,
    _is_filter_rejected,
    _load_nb_devices,
    _mark_incomplete_preload,
    _names_all_present,
    _nb_vm_in_cluster,
    _preload_incomplete,
    cleanup_stale_vms,
)


def http_error(status):
    """An exception shaped like the one pynetbox raises for a rejected query."""
    error = Exception(f'HTTP {status}')
    error.req = types.SimpleNamespace(status_code=status)
    return error


class FakeEndpoint:
    """A NetBox endpoint that answers, or fails, exactly as a test asks it to."""

    def __init__(self, records=(), fail_on=None, error=None):
        self.records = list(records)
        self.fail_on = fail_on or (lambda params: False)
        self.error = error or http_error(500)
        self.calls = []

    def filter(self, **params):
        self.calls.append(params)
        if self.fail_on(params):
            raise self.error
        return [r for r in self.records if self._matches(r, params)]

    def all(self):
        self.calls.append({})
        return list(self.records)

    @staticmethod
    def _matches(record, params):
        for key, wanted in params.items():
            value = getattr(record, key.replace('_id', '_id'), None)
            if key == 'virtual_machine_id':
                value = record.virtual_machine.id
            elif key == 'cluster_id':
                value = getattr(record, 'cluster_id', None)
            if isinstance(wanted, list):
                if value not in wanted:
                    return False
            elif value != wanted:
                return False
        return True


def fake_iface(vm_id, name='eth0'):
    return types.SimpleNamespace(
        name=name, virtual_machine=types.SimpleNamespace(id=vm_id), cluster_id=1)


def fake_vm(vm_id, serial, cluster_id=1, name=None):
    deleted = []
    vm = types.SimpleNamespace(
        id=vm_id,
        serial=str(serial),
        name=name or f'vm{serial}',
        cluster=types.SimpleNamespace(id=cluster_id) if cluster_id is not None else None,
        deleted=deleted,
    )
    vm.delete = lambda: deleted.append(True)
    return vm


class TestIsFilterRejected:
    def test_bad_request_is_a_rejected_filter(self):
        assert _is_filter_rejected(http_error(400))

    @pytest.mark.parametrize('status', [401, 403, 500, 502, 503])
    def test_everything_else_is_not(self, status):
        assert not _is_filter_rejected(http_error(status))

    def test_plain_exception_is_not(self):
        # A timeout carries no status; widening the query on it would turn a
        # blip into a read of the whole inventory.
        assert not _is_filter_rejected(TimeoutError('timed out'))


class TestFetchFiltered:
    def test_falls_back_when_the_filter_is_unknown(self):
        endpoint = FakeEndpoint(
            records=[fake_iface(1)],
            fail_on=lambda p: 'name__ie' in p,
            error=http_error(400),
        )
        records = _fetch_filtered(endpoint, 'devices', [{'name__ie': ['a']}, {}])
        assert len(records) == 1
        assert endpoint.calls == [{'name__ie': ['a']}, {}]

    def test_transient_failure_is_raised_not_widened(self):
        endpoint = FakeEndpoint(fail_on=lambda p: bool(p), error=http_error(502))
        with pytest.raises(Exception):
            _fetch_filtered(endpoint, 'devices', [{'name__ie': ['a']}, {}])
        # The wider query was never attempted.
        assert endpoint.calls == [{'name__ie': ['a']}]

    def test_last_attempt_failing_is_raised(self):
        # Nothing wider is left to try, so even a rejected filter has to surface.
        endpoint = FakeEndpoint(fail_on=lambda p: True, error=http_error(400))
        with pytest.raises(Exception):
            _fetch_filtered(endpoint, 'devices', [{'name': ['a']}])


class TestFetchFilteredCoverage:
    """
    An answer NetBox gives without honouring the filter in full. It raises
    nothing, so only the answer itself can give it away.
    """

    def test_an_answer_that_does_not_cover_is_widened(self):
        endpoint = FakeEndpoint(records=[fake_iface(1), fake_iface(2)])
        records = _fetch_filtered(
            endpoint, 'ifaces', [{'name': ['a']}, {}], _covers=lambda found: len(found) > 5)
        assert len(records) == 2
        assert endpoint.calls == [{'name': ['a']}, {}]

    def test_an_answer_that_covers_is_kept(self):
        endpoint = FakeEndpoint(records=[fake_iface(1)])
        _fetch_filtered(endpoint, 'ifaces', [{'name': ['a']}, {}], _covers=lambda found: True)
        assert endpoint.calls == [{'name': ['a']}]

    def test_the_last_parameter_set_is_accepted_as_it_is(self):
        # Nothing wider is left, so an unsatisfying answer is still the answer.
        endpoint = FakeEndpoint(records=[fake_iface(1)])
        records = _fetch_filtered(endpoint, 'ifaces', [{}], _covers=lambda found: False)
        assert len(records) == 1


class FakeDevices:
    """A dcim.devices endpoint that honours only the lookups it is told to."""

    def __init__(self, names, honours=('name__ie', 'name'), narrow=False):
        self.devices = [types.SimpleNamespace(name=name) for name in names]
        self.honours = honours
        self.narrow = narrow
        self.calls = []

    def filter(self, **params):
        self.calls.append(params)
        (key, wanted), = params.items()
        if key not in self.honours:
            raise http_error(400)
        if self.narrow:
            # What some NetBox versions do with a multi-value filter.
            wanted = wanted[-1:]
        if key == 'name__ie':
            folded = {value.lower() for value in wanted}
            return [d for d in self.devices if d.name.lower() in folded]
        return [d for d in self.devices if d.name in set(wanted)]

    def all(self):
        self.calls.append({})
        return list(self.devices)


def load_devices(devices, node_names, scoped=True):
    """Run _load_nb_devices against a fake endpoint; returns the device cache."""
    nb_objects = _empty_nb_objects()
    nb_api = types.SimpleNamespace(dcim=types.SimpleNamespace(devices=devices))
    _load_nb_devices(nb_api, nb_objects, node_names, scoped)
    return nb_objects['devices']


class TestNamesAllPresent:
    def test_case_folded_comparison(self):
        covers = _names_all_present(['PVE1', 'pve2'])
        assert covers([types.SimpleNamespace(name='pve1'), types.SimpleNamespace(name='PVE2')])

    def test_a_missing_name_is_not_covered(self):
        assert not _names_all_present(['pve1', 'pve2'])([types.SimpleNamespace(name='pve1')])

    def test_a_nameless_record_does_not_crash(self):
        assert not _names_all_present(['pve1'])([types.SimpleNamespace(name=None)])


class TestLoadNbDevices:
    """
    A node whose device is missing from the cache reads as "no such device in
    NetBox": the node is skipped (or the process exits), and a skipped node's
    guests never reach current_vmids, so ENABLE_CLEANUP deletes them. A scoped
    query that answers short has to be widened, not believed.
    """

    def test_case_insensitive_filter_is_enough_on_its_own(self):
        devices = FakeDevices(['PVE1'])
        assert set(load_devices(devices, ['pve1'])) == {'pve1'}
        assert devices.calls == [{'name__ie': ['pve1']}]

    def test_case_sensitive_fallback_missing_a_device_is_widened(self):
        # NetBox too old for name__ie, and the device is spelled differently
        # from the Proxmox node. The narrow query answers nothing, without error.
        devices = FakeDevices(['PVE1'], honours=('name',))
        assert set(load_devices(devices, ['pve1'])) == {'pve1'}
        assert devices.calls == [{'name__ie': ['pve1']}, {'name': ['pve1']}, {}]

    def test_multi_value_filter_narrowed_to_one_value_is_widened(self):
        devices = FakeDevices(['pve1', 'pve2'], narrow=True)
        assert set(load_devices(devices, ['pve1', 'pve2'])) == {'pve1', 'pve2'}
        assert devices.calls[-1] == {}

    def test_a_node_that_really_has_no_device_widens_once_and_stops(self):
        # Widening cannot invent the device; it just proves it is absent.
        devices = FakeDevices(['pve1'])
        assert set(load_devices(devices, ['pve1', 'pve404'])) == {'pve1'}
        assert devices.calls == [
            {'name__ie': ['pve1', 'pve404']},
            {'name': ['pve1', 'pve404']},
            {},
        ]

    def test_a_complete_answer_costs_one_request(self):
        devices = FakeDevices(['pve1', 'pve2'])
        assert set(load_devices(devices, ['pve1', 'pve2'])) == {'pve1', 'pve2'}
        assert len(devices.calls) == 1

    def test_no_nodes_in_scope_asks_nothing(self):
        devices = FakeDevices(['pve1'])
        assert load_devices(devices, []) == {}
        assert devices.calls == []

    def test_unscoped_reads_everything(self):
        devices = FakeDevices(['pve1', 'pve2'])
        assert set(load_devices(devices, ['pve1'], scoped=False)) == {'pve1', 'pve2'}
        assert devices.calls == [{}]


class TestFetchForVms:
    def test_reports_which_vms_could_not_be_loaded(self):
        vm_ids = list(range(1, NB_FILTER_CHUNK_SIZE + 3))
        failing = set(vm_ids[NB_FILTER_CHUNK_SIZE:])
        endpoint = FakeEndpoint(
            records=[fake_iface(vm_id) for vm_id in vm_ids],
            fail_on=lambda p: bool(failing & set(p['virtual_machine_id'])),
            error=http_error(502),
        )
        records, unloaded = _fetch_for_vms(endpoint, 'interfaces', vm_ids)

        # A failed chunk costs only its own VMs; the first chunk survives.
        assert unloaded == failing
        assert len(records) == NB_FILTER_CHUNK_SIZE

    def test_no_vms_makes_no_request(self):
        endpoint = FakeEndpoint()
        assert _fetch_for_vms(endpoint, 'interfaces', []) == ([], set())
        assert endpoint.calls == []


class TestFetchClusterScoped:
    def test_prefers_a_single_cluster_query(self):
        endpoint = FakeEndpoint(records=[fake_iface(1), fake_iface(2)])
        records, unloaded = _fetch_cluster_scoped(endpoint, 'interfaces', 1, [1, 2])
        assert len(records) == 2
        assert unloaded == set()
        assert endpoint.calls == [{'cluster_id': 1}]

    def test_falls_back_per_vm_when_the_endpoint_has_no_cluster_filter(self):
        endpoint = FakeEndpoint(
            records=[fake_iface(1)],
            fail_on=lambda p: 'cluster_id' in p,
            error=http_error(400),
        )
        records, unloaded = _fetch_cluster_scoped(endpoint, 'disks', 1, [1])
        assert len(records) == 1
        assert unloaded == set()

    def test_transient_failure_is_raised(self):
        endpoint = FakeEndpoint(fail_on=lambda p: True, error=http_error(502))
        with pytest.raises(Exception):
            _fetch_cluster_scoped(endpoint, 'disks', 1, [1])


class TestIncompletePreload:
    def test_unloaded_vm_ids_become_skipped_vmids(self):
        nb_objects = _empty_nb_objects()
        nb_objects['virtual_machines']['100'] = fake_vm(1, 100)
        nb_objects['virtual_machines']['101'] = fake_vm(2, 101)

        _mark_incomplete_preload(nb_objects, {2})

        assert not _preload_incomplete(nb_objects, 100)
        assert _preload_incomplete(nb_objects, 101)

    def test_nothing_unloaded_marks_nothing(self):
        nb_objects = _empty_nb_objects()
        nb_objects['virtual_machines']['100'] = fake_vm(1, 100)
        _mark_incomplete_preload(nb_objects, set())
        assert nb_objects['incomplete_vmids'] == set()


class TestBatchedSerialsHonoured:
    def test_single_element_chunk_is_always_honoured(self):
        assert _batched_serials_honoured([], ['100'])

    def test_records_outside_the_chunk_mean_the_filter_was_ignored(self):
        records = [fake_vm(1, 100), fake_vm(2, 999)]
        assert not _batched_serials_honoured(records, ['100', '101'])

    def test_one_match_for_a_large_chunk_looks_like_truncation(self):
        # A single-value filter keeps only the last serial. Re-querying costs a
        # round of requests; trusting it would sync VMs with no cached objects.
        assert not _batched_serials_honoured([fake_vm(2, '101')], ['100', '101'])

    def test_several_matches_within_the_chunk_are_trusted(self):
        records = [fake_vm(1, 100), fake_vm(2, 101)]
        assert _batched_serials_honoured(records, ['100', '101'])


class TestNbVmInCluster:
    def test_matching_cluster(self):
        assert _nb_vm_in_cluster(fake_vm(1, 100, cluster_id=1), 1)

    def test_other_cluster(self):
        assert not _nb_vm_in_cluster(fake_vm(1, 100, cluster_id=2), 1)

    def test_vm_without_a_cluster_is_ours(self):
        assert _nb_vm_in_cluster(fake_vm(1, 100, cluster_id=None), 1)

    def test_no_configured_cluster_means_no_restriction(self):
        assert _nb_vm_in_cluster(fake_vm(1, 100, cluster_id=2), None)


class TestCleanupStaleVms:
    @staticmethod
    def _objects(vms):
        nb_objects = _empty_nb_objects()
        for vm in vms:
            nb_objects['virtual_machines'][vm.serial] = vm
        return nb_objects

    def test_deletes_only_what_is_really_gone(self, config):
        config()
        present, gone = fake_vm(1, 100), fake_vm(2, 101)
        cleanup_stale_vms(None, self._objects([present, gone]), {100})
        assert present.deleted == []
        assert gone.deleted == [True]

    def test_filtered_guests_are_protected(self, config):
        # They still exist in Proxmox; turning on EXCLUDE_TAGS must not delete
        # the very records the operator wanted left alone.
        config()
        filtered = fake_vm(2, 101)
        cleanup_stale_vms(None, self._objects([filtered]), set(), protected_vmids={101})
        assert filtered.deleted == []

    def test_other_clusters_are_never_touched(self, config):
        # Two Proxmox clusters syncing into one NetBox must not delete each
        # other's records, whatever NB_PRELOAD_SCOPE says.
        config(nb_cluster_id=1, nb_preload_scope='all')
        foreign = fake_vm(2, 101, cluster_id=2)
        cleanup_stale_vms(None, self._objects([foreign]), set())
        assert foreign.deleted == []

    def test_dry_run_deletes_nothing(self, config):
        config()
        gone = fake_vm(2, 101)
        cleanup_stale_vms(None, self._objects([gone]), set(), dry_run=True)
        assert gone.deleted == []

    def test_non_numeric_serials_are_ignored(self, config):
        config()
        odd = fake_vm(3, 'not-a-vmid')
        cleanup_stale_vms(None, self._objects([odd]), set())
        assert odd.deleted == []
