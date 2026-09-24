"""
``NB_PRELOAD_SCOPE`` is a performance knob, and a performance knob that changes
what the sync writes is a bug. These tests hold the two halves of that promise:
the scoped preload reads strictly less, and what it hands the sync for this
cluster is identical to what the unscoped one hands it.
"""

import types

import pytest

from pve2netbox import _empty_nb_objects, _load_nb_objects

CLUSTER = 1
OTHER_CLUSTER = 2


class Rec:
    """A NetBox record: attributes, and the item access the IP cache uses."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getitem__(self, key):
        return self.__dict__[key]


class FakeEndpoint:
    """An endpoint that honours the filters the loader actually sends."""

    def __init__(self, records=()):
        self.records = list(records)
        self.calls = []
        self.records_served = 0

    def all(self):
        self.calls.append({})
        return self._serve(self.records)

    def filter(self, **params):
        self.calls.append(params)
        return self._serve([r for r in self.records if self._matches(r, params)])

    def _serve(self, records):
        self.records_served += len(records)
        return list(records)

    @staticmethod
    def _matches(record, params):
        for key, wanted in params.items():
            wanted = wanted if isinstance(wanted, list) else [wanted]
            if key == 'cluster_id':
                value = getattr(record, 'cluster_id', None)
            elif key == 'virtual_machine_id':
                value = record.virtual_machine.id
            elif key == 'name__ie':
                value = (getattr(record, 'name', '') or '').lower()
                wanted = [str(w).lower() for w in wanted]
            else:
                value = getattr(record, key, None)
            if value not in wanted:
                return False
        return True


def vm(vm_id, vmid, cluster_id=CLUSTER):
    return Rec(id=vm_id, serial=str(vmid), name=f'vm{vmid}', cluster_id=cluster_id,
               cluster=types.SimpleNamespace(id=cluster_id))


def child(obj_id, owner, name, cluster_id=CLUSTER):
    return Rec(id=obj_id, name=name, cluster_id=cluster_id,
               virtual_machine=types.SimpleNamespace(id=owner))


def build_api():
    """
    A NetBox holding two clusters: ours (nodes pve1/pve2, VMs 100-101) and a
    neighbour's, plus the globally shared IPAM the scope must never touch.
    """
    ours = [vm(1, 100), vm(2, 101)]
    theirs = [vm(3, 200, OTHER_CLUSTER), vm(4, 201, OTHER_CLUSTER)]

    return types.SimpleNamespace(
        dcim=types.SimpleNamespace(
            devices=FakeEndpoint([
                Rec(id=1, name='pve1'), Rec(id=2, name='pve2'),
                Rec(id=3, name='storage1'), Rec(id=4, name='switch1'),
            ]),
            mac_addresses=FakeEndpoint([
                Rec(id=1, mac_address='BC:24:11:00:00:01'),
                Rec(id=2, mac_address='BC:24:11:00:00:02'),
            ]),
            device_roles=FakeEndpoint([Rec(id=7, name='Virtual Machine')]),
            platforms=FakeEndpoint([]),
        ),
        virtualization=types.SimpleNamespace(
            virtual_machines=FakeEndpoint(ours + theirs),
            interfaces=FakeEndpoint([
                child(11, 1, 'net0'), child(12, 2, 'net0'),
                child(13, 3, 'net0', OTHER_CLUSTER),
            ]),
            virtual_disks=FakeEndpoint([
                child(21, 1, 'scsi0'), child(22, 2, 'scsi0'),
                child(23, 3, 'scsi0', OTHER_CLUSTER),
            ]),
        ),
        ipam=types.SimpleNamespace(
            ip_addresses=FakeEndpoint([
                Rec(id=1, address='10.0.0.1/24'), Rec(id=2, address='10.0.0.2/24'),
            ]),
            prefixes=FakeEndpoint([Rec(id=1, prefix='10.0.0.0/24')]),
            vlans=FakeEndpoint([Rec(id=1, vid=10)]),
        ),
        extras=types.SimpleNamespace(tags=FakeEndpoint([Rec(id=1, name='Pool/prod')])),
        tenancy=types.SimpleNamespace(tenants=FakeEndpoint([])),
    )


def served(nb_api):
    """How many records the whole preload pulled out of NetBox."""
    endpoints = [
        nb_api.dcim.devices, nb_api.dcim.mac_addresses, nb_api.dcim.device_roles,
        nb_api.virtualization.virtual_machines, nb_api.virtualization.interfaces,
        nb_api.virtualization.virtual_disks, nb_api.ipam.ip_addresses,
        nb_api.ipam.prefixes, nb_api.ipam.vlans, nb_api.extras.tags,
    ]
    return sum(e.records_served for e in endpoints)


NODES = ['pve1', 'pve2']

SHARED_CACHES = ('mac_addresses', 'prefixes', 'ip_addresses', 'vlans', 'tags', 'roles')


@pytest.fixture
def preloads(config):
    """The same NetBox read twice, once per scope."""
    def _load(**overrides):
        results = {}
        for scope in ('cluster', 'all'):
            config(nb_cluster_id=CLUSTER, nb_preload_scope=scope, **overrides)
            nb_api = build_api()
            results[scope] = (_load_nb_objects(nb_api, NODES), nb_api)
        return results
    return _load


class TestScopesAgree:
    def test_this_cluster_is_cached_identically(self, preloads):
        scoped, whole = (objects for objects, _ in preloads().values())
        for serial in ('100', '101'):
            assert scoped['virtual_machines'][serial].id == whole['virtual_machines'][serial].id
        for vm_id in (1, 2):
            assert (sorted(scoped['virtual_machines_interfaces'][vm_id])
                    == sorted(whole['virtual_machines_interfaces'][vm_id]))
            assert sorted(scoped['disks'][vm_id]) == sorted(whole['disks'][vm_id])

    def test_the_nodes_devices_are_cached_identically(self, preloads):
        scoped, whole = (objects for objects, _ in preloads().values())
        for node in NODES:
            assert scoped['devices'][node].id == whole['devices'][node].id

    def test_shared_caches_are_never_scoped(self, preloads):
        # An IP or MAC that exists elsewhere must be found, not duplicated, so
        # these must not depend on the scope at all.
        scoped, whole = (objects for objects, _ in preloads().values())
        for cache in SHARED_CACHES:
            assert sorted(scoped[cache]) == sorted(whole[cache])

    def test_nothing_that_matters_is_missing_from_the_scoped_cache(self, preloads):
        scoped, whole = (objects for objects, _ in preloads().values())
        empty = _empty_nb_objects()
        for cache in empty:
            if cache in ('incomplete_vmids', 'platforms', 'tenants'):
                continue
            assert set(scoped[cache]) <= set(whole[cache]), cache


class TestScopeSaves:
    def test_scoped_reads_strictly_less(self, preloads):
        (_, scoped_api), (_, whole_api) = preloads().values()
        assert served(scoped_api) < served(whole_api)

    def test_the_other_clusters_records_are_not_read(self, preloads):
        scoped, _ = preloads()['cluster']
        assert '200' not in scoped['virtual_machines']
        assert 3 not in scoped['virtual_machines_interfaces']
        assert 3 not in scoped['disks']

    def test_devices_that_are_not_nodes_are_not_read(self, preloads):
        scoped, _ = preloads()['cluster']
        assert set(scoped['devices']) == {'pve1', 'pve2'}


class TestScopeFallsBack:
    def test_no_cluster_id_means_no_scoping(self, config):
        # Without NB_CLUSTER_ID there is nothing to scope by; the loader must
        # read everything rather than silently cache nothing.
        config(nb_cluster_id=None, nb_preload_scope='cluster')
        nb_api = build_api()
        objects = _load_nb_objects(nb_api, NODES)
        assert set(objects['virtual_machines']) == {'100', '101', '200', '201'}
        assert nb_api.virtualization.virtual_machines.calls == [{}]

    def test_every_node_filtered_out_costs_no_device_query(self, config):
        config(nb_cluster_id=CLUSTER, nb_preload_scope='cluster')
        nb_api = build_api()
        objects = _load_nb_objects(nb_api, [])
        assert objects['devices'] == {}
        assert nb_api.dcim.devices.calls == []
