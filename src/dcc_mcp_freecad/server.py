from __future__ import annotations

import signal
import threading
from pathlib import Path
from typing import Optional

from dcc_mcp_core import DccServerOptions
from dcc_mcp_core.server_base import DccServerBase

from .__version__ import __version__

_server: Optional["FreecadMcpServer"] = None


class FreecadMcpServer(DccServerBase):
    def __init__(self, port: Optional[int] = None):
        options = DccServerOptions.from_env(
            "freecad",
            Path(__file__).parent / "skills",
            port=port,
            server_name="dcc-mcp-freecad",
            server_version=__version__,
            adapter_version=__version__,
        )
        super().__init__(options=options)

    def _version_string(self):
        return __version__


def start_server(port: Optional[int] = None):
    global _server
    if _server is None or not _server.is_running:
        _server = FreecadMcpServer(port)
        _server.register_builtin_actions()
        _server.start()
    return _server


def stop_server():
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def main():
    event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: event.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: event.set())
    start_server()
    try:
        event.wait()
    finally:
        stop_server()
