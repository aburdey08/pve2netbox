"""Prometheus metrics for monitoring pve2netbox."""

import time
from typing import Optional
from dataclasses import dataclass, field


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
        last_sync_timestamp: Timestamp of last successful sync (gauge).
        changes_detected: Number of changes detected in last quick check (gauge).
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
    changes_detected: int = 0
    
    def record_full_sync_start(self) -> float:
        """Record start of full sync and return start time."""
        self.full_syncs_total += 1
        return time.time()
    
    def record_full_sync_end(self, start_time: float, vm_count: int, lxc_count: int) -> None:
        """Record end of full sync."""
        duration = time.time() - start_time
        self.last_sync_duration_seconds = duration
        self.last_sync_timestamp = time.time()
        self.vms_tracked = vm_count
        self.lxc_tracked = lxc_count
    
    def record_quick_check(self, changes_count: int) -> None:
        """Record quick check operation."""
        self.quick_checks_total += 1
        self.changes_detected = changes_count
    
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
        return f"""# HELP pve2netbox_full_syncs_total Total number of full synchronizations
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

# HELP pve2netbox_last_sync_timestamp_seconds Timestamp of last successful sync
# TYPE pve2netbox_last_sync_timestamp_seconds gauge
pve2netbox_last_sync_timestamp_seconds {self.last_sync_timestamp:.0f}

# HELP pve2netbox_changes_detected Number of changes detected in last quick check
# TYPE pve2netbox_changes_detected gauge
pve2netbox_changes_detected {self.changes_detected}
"""


metrics = SyncMetrics()
"""Global metrics instance for the application."""


def start_metrics_server(port: int = 9090) -> None:
    """
    Start simple HTTP server for Prometheus metrics endpoint.
    
    Args:
        port: Port to listen on (default: 9090)
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading
    
    class MetricsHandler(BaseHTTPRequestHandler):
        """Serves /metrics in Prometheus exposition format. Other paths return 404."""

        def do_GET(self):
            if self.path == '/metrics':
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; version=0.0.4')
                self.end_headers()
                self.wfile.write(metrics.get_prometheus_metrics().encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            """Suppress HTTP server access logs to avoid noise."""
            pass
    
    server = HTTPServer(('0.0.0.0', port), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
