"""Entry point for ``python -m pve2netbox``. Mode dispatch lives in :mod:`pve2netbox.cli`."""

import sys

from pve2netbox.cli import run

if __name__ == '__main__':
    sys.exit(run())
