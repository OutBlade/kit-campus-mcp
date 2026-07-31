"""MCP server and client library for the KIT Campus portal."""

from .auth import KitAuthRequiredError, KitError, KitLoginError
from .client import KitCampusClient
from .config import Settings, load_settings
from .watch import SnapshotStore

__all__ = [
    "KitAuthRequiredError",
    "KitCampusClient",
    "KitError",
    "KitLoginError",
    "Settings",
    "SnapshotStore",
    "load_settings",
]

__version__ = "0.1.0"
