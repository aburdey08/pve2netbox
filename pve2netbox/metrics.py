"""Prometheus metrics and health endpoints for monitoring pve2netbox."""

import threading
import time
from typing import Callable, Optional, Tuple
from dataclasses import dataclass

from .version import get_version


@dataclass
class SyncMetrics:
    """
    Metrics for sync operations.

    Attributes:
        full_syncs_total: Total number of full synchronizations (counter).
        quick_checks_total: Total number of quick checks (counter).
        vms_synced_total: Total number of VMs synchronized (counter).
        lxc_synced_total: Total number of LXC containers synchronized (counter).
        errors_total: Total number of errors (counter).
        vms_tracked: Number of VMs currently tracked (gauge).
        lxc_tracked: Number of LXC containers currently tracked (gauge).
        last_sync_duration_seconds: Duration of last sync in seconds (gauge).
        last_sync_timestamp: Timestamp of the last full sync attempt, successful
            or not (gauge).
        last_success_timestamp: Timestamp of the last *successful* full sync
            (gauge). This is the one to alert on and the one /readyz uses.
        changes_detected: Number of changes detected in last quick check (gauge).
        guests_filtered: Guests excluded by the selection filters in the last
            full sync (gauge). A jump here explains a drop in vms_tracked.
    """
    full_syncs_total: int = 0
    quick_checks_total: int = 0
    vms_synced_total: int = 0
    lxc_synced_total: int = 0
    errors_total: int = 0
    vms_tracked: int = 0
    lxc_tracked: int = 0
    last_sync_duration_seconds: float = 0.0
    last_sync_timestamp: float = 0.0
    last_success_timestamp: float = 0.0
    changes_detected: int = 0
    guests_filtered: int = 0

    def record_full_sync_start(self) -> float:
        """Record start of full sync and return start time."""
        self.full_syncs_total += 1
        return time.time()

    def record_full_sync_end(self, start_time: float, vm_count: int, lxc_count: int,
                             success: bool = True) -> None:
        """
        Record end of a full sync attempt.

        ``success=False`` still updates duration and the attempt timestamp, but
        leaves ``last_success_timestamp`` untouched so staleness alerts and
        /readyz keep firing while syncs are failing.
        """
        now = time.time()
        self.last_sync_duration_seconds = now - start_time
        self.last_sync_timestamp = now
        self.vms_tracked = vm_count
        self.lxc_tracked = lxc_count
        if success:
            self.last_success_timestamp = now

    def record_quick_check(self, changes_count: int) -> None:
        """Record quick check operation."""
        self.quick_checks_total += 1
        self.changes_detected = changes_count

    def record_filtered(self, filtered_count: int) -> None:
        """Record how many guests the selection filters excluded."""
        self.guests_filtered = filtered_count

    def record_vm_sync(self) -> None:
        """Record VM sync."""
        self.vms_synced_total += 1

    def record_lxc_sync(self) -> None:
        """Record LXC sync."""
        self.lxc_synced_total += 1

    def record_error(self) -> None:
        """Record error."""
        self.errors_total += 1

    def get_prometheus_metrics(self) -> str:
        """
        Generate Prometheus metrics in text format.

        Returns:
            Metrics in Prometheus exposition format
        """
        return f"""# HELP pve2netbox_build_info Build information
# TYPE pve2netbox_build_info gauge
pve2netbox_build_info{{version="{get_version()}"}} 1

# HELP pve2netbox_full_syncs_total Total number of full synchronizations
# TYPE pve2netbox_full_syncs_total counter
pve2netbox_full_syncs_total {self.full_syncs_total}

# HELP pve2netbox_quick_checks_total Total number of quick checks
# TYPE pve2netbox_quick_checks_total counter
pve2netbox_quick_checks_total {self.quick_checks_total}

# HELP pve2netbox_vms_synced_total Total number of VMs synchronized
# TYPE pve2netbox_vms_synced_total counter
pve2netbox_vms_synced_total {self.vms_synced_total}

# HELP pve2netbox_lxc_synced_total Total number of LXC containers synchronized
# TYPE pve2netbox_lxc_synced_total counter
pve2netbox_lxc_synced_total {self.lxc_synced_total}

# HELP pve2netbox_errors_total Total number of errors
# TYPE pve2netbox_errors_total counter
pve2netbox_errors_total {self.errors_total}

# HELP pve2netbox_vms_tracked Number of VMs currently tracked
# TYPE pve2netbox_vms_tracked gauge
pve2netbox_vms_tracked {self.vms_tracked}

# HELP pve2netbox_lxc_tracked Number of LXC containers currently tracked
# TYPE pve2netbox_lxc_tracked gauge
pve2netbox_lxc_tracked {self.lxc_tracked}

# HELP pve2netbox_last_sync_duration_seconds Duration of last sync in seconds
# TYPE pve2netbox_last_sync_duration_seconds gauge
pve2netbox_last_sync_duration_seconds {self.last_sync_duration_seconds:.2f}

# HELP pve2netbox_last_sync_timestamp_seconds Timestamp of last full sync attempt
# TYPE pve2netbox_last_sync_timestamp_seconds gauge
pve2netbox_last_sync_timestamp_seconds {self.last_sync_timestamp:.0f}

# HELP pve2netbox_last_success_timestamp_seconds Timestamp of last successful full sync
# TYPE pve2netbox_last_success_timestamp_seconds gauge
pve2netbox_last_success_timestamp_seconds {self.last_success_timestamp:.0f}

# HELP pve2netbox_changes_detected Number of changes detected in last quick check
# TYPE pve2netbox_changes_detected gauge
pve2netbox_changes_detected {self.changes_detected}

# HELP pve2netbox_guests_filtered Guests excluded by the selection filters in the last full sync
# TYPE pve2netbox_guests_filtered gauge
pve2netbox_guests_filtered {self.guests_filtered}
"""


metrics = SyncMetrics()
"""Global metrics instance for the application."""

ReadinessCheck = Callable[[], Tuple[bool, str]]
"""Returns (ready, human-readable reason)."""


def default_readiness(max_age_seconds: Optional[float] = None) -> ReadinessCheck:
    """
    Build a readiness check based on the age of the last successful full sync.

    Args:
        max_age_seconds: Fail readiness when the last successful sync is older
            than this. ``None`` (single-run mode) only requires that one sync
            has succeeded.
    """
    def _check() -> Tuple[bool, str]:
        if metrics.last_success_timestamp == 0.0:
            return False, 'no successful sync yet'
        age = time.time() - metrics.last_success_timestamp
        if max_age_seconds is not None and age > max_age_seconds:
            return False, (f'last successful sync was {age:.0f}s ago '
                           f'(limit {max_age_seconds:.0f}s)')
        return True, f'last successful sync {age:.0f}s ago'

    return _check


def start_http_server(
        port: int = 9090,
        enable_metrics: bool = True,
        enable_health: bool = True,
        readiness: Optional[ReadinessCheck] = None,
) -> None:
    """
    Start the HTTP server exposing metrics and health endpoints.

    Endpoints:
        ``/metrics`` — Prometheus exposition format (when ``enable_metrics``).
        ``/healthz`` — liveness, 200 while the process runs (when ``enable_health``).
        ``/readyz``  — readiness, 503 when the last successful sync is too old.

    Args:
        port: Port to listen on.
        enable_metrics: Serve ``/metrics``.
        enable_health: Serve ``/healthz`` and ``/readyz``.
        readiness: Readiness check; defaults to "at least one successful sync".
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler  # pylint: disable=import-outside-toplevel

    check = readiness or default_readiness()

    class RequestHandler(BaseHTTPRequestHandler):
        """Serves /metrics, /healthz and /readyz. Other paths return 404."""

        def _respond(self, status: int, body: str, content_type: str = 'text/plain') -> None:
            payload = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # pylint: disable=invalid-name
            if self.path == '/metrics' and enable_metrics:
                self._respond(200, metrics.get_prometheus_metrics(),
                              'text/plain; version=0.0.4')
            elif self.path == '/healthz' and enable_health:
                self._respond(200, 'ok\n')
            elif self.path == '/readyz' and enable_health:
                ready, reason = check()
                self._respond(200 if ready else 503, f'{reason}\n')
            else:
                self._respond(404, 'not found\n')

        def log_message(self, format, *args):  # pylint: disable=redefined-builtin
            """Suppress HTTP server access logs to avoid noise."""

    server = HTTPServer(('0.0.0.0', port), RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def start_metrics_server(port: int = 9090) -> None:
    """Deprecated alias of :func:`start_http_server` kept for compatibility."""
    start_http_server(port=port)
