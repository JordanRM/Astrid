"""Small networking helpers shared by the Telegram and OpenRouter clients."""
from __future__ import annotations

import socket
import threading

import requests
import urllib3.util.connection as _cn

_local = threading.local()


def force_ipv4() -> None:
    """Resolve hosts to IPv4 only. A broken IPv6 route makes each new connection stall for many seconds before it falls back."""
    _cn.allowed_gai_family = lambda: socket.AF_INET


def session() -> requests.Session:
    """One keep-alive session per worker thread, so repeat calls skip the TCP and TLS handshake."""
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = requests.Session()
    return s
