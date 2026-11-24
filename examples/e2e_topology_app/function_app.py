"""Ephemeral real-Azure smoke app, intentionally unindexable with unsafe settings."""

import os

from topology_app import export_app

app = export_app(os.environ)
