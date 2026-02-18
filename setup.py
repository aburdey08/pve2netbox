"""
Fallback for older setuptools or environments where pyproject.toml metadata is not read.
Prefer building/installing via pyproject.toml when possible.
"""
from setuptools import setup, find_packages

setup(
    name="pve2netbox",
    version="1.0.2",
    install_requires=[
        "pynetbox>=7.4",
        "proxmoxer>=2.2",
        "requests>=2.32",
    ],
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "pve2netbox=pve2netbox:main",
        ],
    },
    python_requires=">=3.8",
)
