#!/usr/bin/env python3
"""
Measure what ``NB_PRELOAD_SCOPE`` costs and what it saves.

Runs :func:`pve2netbox._load_nb_objects` once per scope and reports, for each,
the HTTP requests it makes, the records and bytes NetBox sends back, the wall
time and the peak memory of the resulting cache. Read-only: the loader only
issues GETs, so pointing this at a production NetBox changes nothing.

Two modes:

* ``synthetic`` (default) — a local HTTP server answers with NetBox-shaped
  JSON of a size you choose, and real pynetbox reads it. Pagination, JSON
  parsing and ``Record`` construction are all real; only the network and
  NetBox's own query time are not. Reproducible, needs no inventory.
* ``live`` — the NetBox from the environment (``.env`` is read the way the
  daemon reads it). Node names come from ``--nodes``, or from Proxmox when
  ``--nodes`` is omitted. This is the measurement the Changelog quotes.

Examples::

    python tools/preload_bench.py
    python tools/preload_bench.py --vms-per-cluster 1000 --ips 50000
    python tools/preload_bench.py --mode live --nodes pve1,pve2,pve3
"""

# The path bootstrap below has to run before the package is imported, and the
# inventory generator is one long list of NetBox record shapes on purpose.
# pylint: disable=wrong-import-position,too-many-statements,too-many-locals

import argparse
import dataclasses
import json
import os
import sys
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pynetbox  # noqa: E402
import requests  # noqa: E402

from pve2netbox import _load_nb_objects, _select_pve_nodes  # noqa: E402
from pve2netbox.api.netbox import make_netbox_session  # noqa: E402
from pve2netbox.api.proxmox import create_proxmox_api  # noqa: E402
from pve2netbox.config import Config, load_config, load_env_file, set_config  # noqa: E402
from pve2netbox.filters import FilterDecisions, get_filters  # noqa: E402
from pve2netbox.logger import set_log_level  # noqa: E402

PAGINATE_COUNT = 50
"""Page size when a request names none, as NetBox's ``PAGINATE_COUNT`` default has it."""

MAX_PAGE_SIZE = 1000
"""Cap on a requested page, as NetBox's ``MAX_PAGE_SIZE`` default has it. pynetbox
asks for ``limit=0`` ("everything"), and NetBox answers it one cap-sized page at a
time — which is what makes the request counts here match a real installation."""


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class Measurement:
    """What one preload cost."""
    scope: str
    seconds: float
    requests: int
    bytes_in: int
    peak_kib: float
    counts: Dict[str, int]

    @property
    def records(self) -> int:
        """Records held in the cache after the load."""
        return sum(self.counts.values())


class _Counter:
    """Counts the HTTP traffic of everything that goes through requests."""

    def __init__(self) -> None:
        self.requests = 0
        self.bytes_in = 0
        self._original = requests.Session.send

    def __enter__(self) -> '_Counter':
        counter = self

        def send(session, request, **kwargs):
            # pylint: disable=protected-access  (the saved original, not a stranger's)
            response = counter._original(session, request, **kwargs)
            counter.requests += 1
            counter.bytes_in += len(response.content or b'')
            return response

        requests.Session.send = send
        return self

    def __exit__(self, *exc: Any) -> None:
        requests.Session.send = self._original


def measure(config: Config, nb_api: pynetbox.api, node_names: Optional[List[str]],
            scope: str) -> Measurement:
    """Run one preload under ``scope`` and record what it cost."""
    set_config(dataclasses.replace(config, nb_preload_scope=scope))

    tracemalloc.start()
    tracemalloc.reset_peak()
    with _Counter() as counter:
        started = time.perf_counter()
        nb_objects = _load_nb_objects(nb_api, node_names)
        elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    counts = {
        'devices': len(nb_objects['devices']),
        'VMs': len(nb_objects['virtual_machines']),
        'interfaces': sum(len(v) for v in nb_objects['virtual_machines_interfaces'].values()),
        'disks': sum(len(v) for v in nb_objects['disks'].values()),
        'MACs': len(nb_objects['mac_addresses']),
        'prefixes': len(nb_objects['prefixes']),
        'IPs': len(nb_objects['ip_addresses']),
        'VLANs': len(nb_objects['vlans']),
    }
    del nb_objects
    return Measurement(scope, elapsed, counter.requests, counter.bytes_in,
                       peak / 1024, counts)


# --------------------------------------------------------------------------- #
# Synthetic NetBox
# --------------------------------------------------------------------------- #

def _brief(kind: str, obj_id: int, name: str) -> Dict[str, Any]:
    """A nested object the way NetBox serializes one: id, url, display, name."""
    return {'id': obj_id, 'url': f'/api/{kind}/{obj_id}/', 'display': name, 'name': name}


def _common(kind: str, obj_id: int, name: str) -> Dict[str, Any]:
    """The fields every NetBox record carries, so payload sizes stay honest."""
    return {
        'id': obj_id,
        'url': f'/api/{kind}/{obj_id}/',
        'display_url': f'/{kind}/{obj_id}/',
        'display': name,
        'description': '',
        'comments': '',
        'tags': [],
        'custom_fields': {'proxmox_vmid': None, 'proxmox_node': None},
        'created': '2025-01-01T00:00:00Z',
        'last_updated': '2025-06-01T12:00:00Z',
    }


def build_inventory(  # pylint: disable=too-many-locals
        args: argparse.Namespace) -> Dict[str, List[Dict[str, Any]]]:
    """Generate a NetBox-shaped inventory of the requested size."""
    data: Dict[str, List[Dict[str, Any]]] = {}
    next_id = iter(range(1, 10_000_000))

    data['dcim/devices'] = []
    for i in range(args.devices):
        obj_id = next(next_id)
        # The first --nodes devices are the Proxmox nodes this cluster runs on.
        name = f'pve{i + 1}' if i < args.nodes_count else f'srv-{i + 1:04d}'
        record = _common('dcim/devices', obj_id, name)
        record.update({
            'name': name,
            'device_type': _brief('dcim/device-types', 1, 'PowerEdge R650'),
            'role': _brief('dcim/device-roles', 1, 'Hypervisor'),
            'site': _brief('dcim/sites', 1, 'DC1'),
            'status': {'value': 'active', 'label': 'Active'},
            'primary_ip4': None,
            'serial': f'SN{obj_id:08d}',
        })
        data['dcim/devices'].append(record)

    data['virtualization/virtual-machines'] = []
    interfaces: List[Dict[str, Any]] = []
    disks: List[Dict[str, Any]] = []
    for cluster in range(1, args.clusters + 1):
        for index in range(args.vms_per_cluster):
            vmid = cluster * 1000 + index
            obj_id = next(next_id)
            name = f'vm-c{cluster}-{vmid}'
            record = _common('virtualization/virtual-machines', obj_id, name)
            record.update({
                'name': name,
                'status': {'value': 'active', 'label': 'Active'},
                'site': _brief('dcim/sites', 1, 'DC1'),
                'cluster': _brief('virtualization/clusters', cluster, f'pve-cluster-{cluster}'),
                'device': _brief('dcim/devices', 1 + index % max(args.nodes_count, 1), 'pve1'),
                'role': _brief('dcim/device-roles', 1, 'Virtual Machine'),
                'tenant': None,
                'platform': None,
                'primary_ip4': None,
                'primary_ip6': None,
                'vcpus': 4.0,
                'memory': 8192,
                'disk': 102400,
                'serial': str(vmid),
                'config_template': None,
                'local_context_data': None,
            })
            data['virtualization/virtual-machines'].append(record)

            for n in range(args.interfaces_per_vm):
                iface_id = next(next_id)
                iface_name = f'net{n}'
                iface = _common('virtualization/interfaces', iface_id, iface_name)
                iface.update({
                    'name': iface_name,
                    'virtual_machine': _brief(
                        'virtualization/virtual-machines', obj_id, name),
                    'enabled': True,
                    'mtu': 1500,
                    'mac_address': f'BC:24:11:{iface_id % 256:02X}:'
                                   f'{iface_id // 256 % 256:02X}:{n:02X}',
                    'mode': None,
                    'untagged_vlan': None,
                    'tagged_vlans': [],
                    'count_ipaddresses': 1,
                })
                iface['_cluster_id'] = cluster
                iface['_vm_id'] = obj_id
                interfaces.append(iface)

            for n in range(args.disks_per_vm):
                disk_id = next(next_id)
                disk_name = f'scsi{n}'
                disk = _common('virtualization/virtual-disks', disk_id, disk_name)
                disk.update({
                    'name': disk_name,
                    'virtual_machine': _brief(
                        'virtualization/virtual-machines', obj_id, name),
                    'size': 51200,
                })
                disk['_cluster_id'] = cluster
                disk['_vm_id'] = obj_id
                disks.append(disk)

    data['virtualization/interfaces'] = interfaces
    data['virtualization/virtual-disks'] = disks

    data['ipam/ip-addresses'] = []
    for i in range(args.ips):
        obj_id = next(next_id)
        address = f'10.{i // 65536 % 256}.{i // 256 % 256}.{i % 256}/24'
        record = _common('ipam/ip-addresses', obj_id, address)
        record.update({
            'address': address,
            'family': {'value': 4, 'label': 'IPv4'},
            'vrf': None,
            'tenant': None,
            'status': {'value': 'active', 'label': 'Active'},
            'role': None,
            'assigned_object_type': 'virtualization.vminterface',
            'assigned_object_id': None,
            'assigned_object': None,
            'dns_name': '',
        })
        data['ipam/ip-addresses'].append(record)

    data['ipam/prefixes'] = []
    for i in range(args.prefixes):
        obj_id = next(next_id)
        prefix = f'10.{i // 256 % 256}.{i % 256}.0/24'
        record = _common('ipam/prefixes', obj_id, prefix)
        record.update({
            'prefix': prefix,
            'family': {'value': 4, 'label': 'IPv4'},
            'site': _brief('dcim/sites', 1, 'DC1'),
            'vrf': None,
            'tenant': None,
            'vlan': None,
            'status': {'value': 'active', 'label': 'Active'},
            'role': None,
            'is_pool': False,
        })
        data['ipam/prefixes'].append(record)

    data['dcim/mac-addresses'] = []
    for i in range(args.macs):
        obj_id = next(next_id)
        mac = (f'BC:24:11:{i // 65536 % 256:02X}:'
               f'{i // 256 % 256:02X}:{i % 256:02X}')
        record = _common('dcim/mac-addresses', obj_id, mac)
        record.update({
            'mac_address': mac,
            'assigned_object_type': 'virtualization.vminterface',
            'assigned_object_id': None,
            'assigned_object': None,
        })
        data['dcim/mac-addresses'].append(record)

    data['ipam/vlans'] = []
    for i in range(args.vlans):
        obj_id = next(next_id)
        name = f'vlan-{i + 1}'
        record = _common('ipam/vlans', obj_id, name)
        record.update({
            'vid': i + 1,
            'name': name,
            'site': _brief('dcim/sites', 1, 'DC1'),
            'group': None,
            'tenant': None,
            'status': {'value': 'active', 'label': 'Active'},
            'role': None,
        })
        data['ipam/vlans'].append(record)

    data['extras/tags'] = []
    for i in range(args.tags):
        obj_id = next(next_id)
        name = f'Pool/pool-{i + 1}'
        record = _common('extras/tags', obj_id, name)
        record.update({'name': name, 'slug': f'pool-{i + 1}', 'color': '9e9e9e',
                       'object_types': [], 'tagged_items': 0})
        data['extras/tags'].append(record)

    data['dcim/device-roles'] = []
    for i in range(args.roles):
        obj_id = next(next_id)
        name = f'role-{i + 1}' if i else 'Virtual Machine'
        record = _common('dcim/device-roles', obj_id, name)
        record.update({'name': name, 'slug': name.lower().replace(' ', '-'),
                       'color': '9e9e9e', 'vm_role': True, 'device_count': 0})
        data['dcim/device-roles'].append(record)

    data['dcim/platforms'] = []
    data['tenancy/tenants'] = []
    return data


def _matches(record: Dict[str, Any], query: Dict[str, List[str]]) -> bool:
    """Apply the filters the loader uses; anything else is answered as NetBox would."""
    for key, wanted in query.items():
        if key in ('limit', 'offset', 'brief', 'exclude'):
            continue
        if key == 'cluster_id':
            value = record.get('_cluster_id') or (record.get('cluster') or {}).get('id')
            if str(value) not in wanted:
                return False
        elif key == 'virtual_machine_id':
            value = record.get('_vm_id') or (record.get('virtual_machine') or {}).get('id')
            if str(value) not in wanted:
                return False
        elif key == 'name__ie':
            if (record.get('name') or '').lower() not in {w.lower() for w in wanted}:
                return False
        elif key == 'name':
            if record.get('name') not in wanted:
                return False
        else:
            return False
    return True


class _Handler(BaseHTTPRequestHandler):
    """Serves the generated inventory the way NetBox's REST API does."""

    inventory: Dict[str, List[Dict[str, Any]]] = {}

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """Answer one GET; the name is the one the base class dispatches to."""
        parsed = urlparse(self.path)
        path = parsed.path.strip('/')
        query = parse_qs(parsed.query)

        if path in ('api', 'api/status'):
            self._json({'netbox-version': '4.1.0', 'python-version': '3.12.0'})
            return

        endpoint = path[len('api/'):] if path.startswith('api/') else path
        if endpoint not in self.inventory:
            self._json({'detail': 'Not found.'}, status=404)
            return

        records = [r for r in self.inventory[endpoint] if _matches(r, query)]
        requested = int(query.get('limit', [PAGINATE_COUNT])[0])
        limit = min(requested or MAX_PAGE_SIZE, MAX_PAGE_SIZE)
        offset = int(query.get('offset', [0])[0])
        page = records[offset:offset + limit]

        next_offset = offset + limit
        next_url = None
        if next_offset < len(records):
            base = f'http://{self.headers.get("Host")}/{path}/'
            params = {k: v for k, v in query.items() if k != 'offset'}
            params['limit'] = [str(limit)]
            query_string = '&'.join(
                f'{k}={v}' for k, values in params.items() for v in values)
            next_url = f'{base}?{query_string}&offset={next_offset}'

        self._json({
            'count': len(records),
            'next': next_url,
            'previous': None,
            'results': [{k: v for k, v in r.items() if not k.startswith('_')}
                        for r in page],
        })

    def _json(self, payload: Any, status: int = 200) -> None:
        """Answer one request with a JSON body, as NetBox does."""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Quiet: the benchmark's own output is the only thing worth reading."""


def serve(inventory: Dict[str, List[Dict[str, Any]]]) -> Tuple[ThreadingHTTPServer, str]:
    """Start the synthetic NetBox on a free port; returns the server and its URL."""
    _Handler.inventory = inventory
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f'http://{host}:{port}'


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _fmt(value: float) -> str:
    return f'{value:,.0f}'.replace(',', ' ')


def report(measurements: List[Measurement]) -> None:
    """Print one row per scope, then what the default scope saves."""
    rows = [('scope', 'requests', 'records', 'MiB in', 'seconds', 'peak MiB')]
    for m in measurements:
        rows.append((
            m.scope,
            _fmt(m.requests),
            _fmt(m.records),
            f'{m.bytes_in / 1024 / 1024:.1f}',
            f'{m.seconds:.2f}',
            f'{m.peak_kib / 1024:.1f}',
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    print()
    for index, row in enumerate(rows):
        print('  '.join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            print('  '.join('-' * w for w in widths))

    print('\nPer object type (records held after the load):')
    kinds = list(measurements[0].counts)
    header = ['type'] + [m.scope for m in measurements]
    lines = [header] + [
        [kind] + [_fmt(m.counts[kind]) for m in measurements] for kind in kinds
    ]
    widths = [max(len(row[i]) for row in lines) for i in range(len(header))]
    for index, row in enumerate(lines):
        print('  '.join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            print('  '.join('-' * w for w in widths))

    by_scope = {m.scope: m for m in measurements}
    if 'cluster' in by_scope and 'all' in by_scope:
        scoped, whole = by_scope['cluster'], by_scope['all']
        print('\ncluster vs all:')
        for label, small, big in (
                ('requests', scoped.requests, whole.requests),
                ('records', scoped.records, whole.records),
                ('bytes', scoped.bytes_in, whole.bytes_in),
                ('seconds', scoped.seconds, whole.seconds),
                ('peak memory', scoped.peak_kib, whole.peak_kib),
        ):
            saved = (1 - small / big) * 100 if big else 0.0
            print(f'  {label:<12} -{saved:5.1f}%')


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def _synthetic_config(url: str, args: argparse.Namespace) -> Config:
    """A Config pointed at the synthetic NetBox, with everything else neutral."""
    return Config(
        pve_api_host='pve.example.com', pve_api_user='sync@pve',
        pve_api_token='token', pve_api_secret='secret', pve_api_verify_ssl=False,
        nb_api_url=url, nb_api_token='benchmark', nb_cluster_id=args.cluster_id,
        nb_api_delay_seconds=0.0, nb_api_retry_total=0, nb_api_retry_backoff=0.0,
        sync_vms=True, sync_lxc=True, sync_tags=True,
        sync_interval_seconds=None, quick_check_interval_seconds=None,
        vm_role=None, lxc_role=None, dry_run=True, enable_cleanup=False,
        enable_metrics=False, metrics_port=9100, ignore_status_when_locked=True,
    )


def run_synthetic(args: argparse.Namespace) -> List[Measurement]:
    """Measure both scopes against a generated inventory of the requested size."""
    print('Building the synthetic inventory...')
    inventory = build_inventory(args)
    total = sum(len(v) for v in inventory.values())
    print(f'  {_fmt(total)} records: '
          + ', '.join(f'{_fmt(len(v))} {k.split("/")[-1]}'
                      for k, v in inventory.items() if v))
    server, url = serve(inventory)
    try:
        config = _synthetic_config(url, args)
        node_names = [f'pve{i + 1}' for i in range(args.nodes_count)]
        nb_api = pynetbox.api(url, token=config.nb_api_token)
        return [measure(config, nb_api, node_names, scope) for scope in args.scopes]
    finally:
        server.shutdown()


def run_live(args: argparse.Namespace) -> List[Measurement]:
    """Measure both scopes against the NetBox this installation actually uses."""
    if args.env_file:
        load_env_file(args.env_file)
    elif os.path.isfile('.env'):
        load_env_file('.env')
    config = load_config()
    # get_filters() and the loader both read the process-wide config.
    set_config(config)

    nb_api = pynetbox.api(config.nb_api_url, token=config.nb_api_token)
    nb_api.http_session = make_netbox_session(config)

    if args.nodes:
        node_names = [n.strip() for n in args.nodes.split(',') if n.strip()]
    else:
        # The nodes the sync itself would take, filters and all: a scoped
        # preload asks NetBox for exactly these devices.
        pve_api = create_proxmox_api(config)
        decisions = FilterDecisions(get_filters())
        node_names = [n['node'] for n in _select_pve_nodes(pve_api, decisions, _quiet=True)]
    print(f'NetBox: {config.nb_api_url}, cluster {config.nb_cluster_id}, '
          f'nodes: {", ".join(node_names) or "(none)"}')
    return [measure(config, nb_api, node_names, scope) for scope in args.scopes]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog='preload_bench.py',
        description='Measure the cost of the NetBox preload under each NB_PRELOAD_SCOPE.',
    )
    parser.add_argument('--mode', choices=('synthetic', 'live'), default='synthetic',
                        help='Generated inventory (default) or the configured NetBox.')
    parser.add_argument('--scopes', default='cluster,all',
                        help='Scopes to measure, in order (default: cluster,all).')
    parser.add_argument('--log-level', default='WARNING',
                        help='Log level of the loader itself (default: WARNING).')
    live = parser.add_argument_group('live mode')
    live.add_argument('--env-file', help='Env file to read instead of ./.env.')
    live.add_argument('--nodes', help='Comma-separated Proxmox node names. '
                                      'Read from Proxmox when omitted.')
    size = parser.add_argument_group('synthetic inventory size')
    size.add_argument('--clusters', type=int, default=10)
    size.add_argument('--cluster-id', type=int, default=1,
                      help='Which of them is the one being synced (default: 1).')
    size.add_argument('--vms-per-cluster', type=int, default=300)
    size.add_argument('--interfaces-per-vm', type=int, default=2)
    size.add_argument('--disks-per-vm', type=int, default=2)
    size.add_argument('--nodes-count', type=int, default=10, dest='nodes_count',
                      help='Proxmox nodes in the synced cluster (default: 10).')
    size.add_argument('--devices', type=int, default=500)
    size.add_argument('--ips', type=int, default=20000)
    size.add_argument('--prefixes', type=int, default=2000)
    size.add_argument('--macs', type=int, default=6000)
    size.add_argument('--vlans', type=int, default=300)
    size.add_argument('--tags', type=int, default=100)
    size.add_argument('--roles', type=int, default=30)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Measure every requested scope and print the comparison."""
    args = build_parser().parse_args(argv)
    args.scopes = [s.strip() for s in args.scopes.split(',') if s.strip()]
    set_log_level(args.log_level)

    measurements = run_live(args) if args.mode == 'live' else run_synthetic(args)

    report(measurements)
    return 0


if __name__ == '__main__':
    sys.exit(main())
