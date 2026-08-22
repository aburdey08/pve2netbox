"""Single source of the package version, read from installed metadata."""

from importlib import metadata

PACKAGE_NAME = 'pve2netbox'


def get_version() -> str:
    """
    Return the installed package version.

    Falls back to ``0.0.0+unknown`` when running straight from a source
    checkout, where no distribution metadata exists.
    """
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return '0.0.0+unknown'


__version__ = get_version()
