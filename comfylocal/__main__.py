# -*- coding: utf-8 -*-
"""Spuštění ComfyLocal: python -m comfylocal"""
from __future__ import annotations

import socket
import threading
import webbrowser

import uvicorn

from .config import CONFIG
from .logging_setup import configure_logging
from .tls import setup_tls


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> None:
    log_path = configure_logging()
    tls_state = setup_tls()
    host = str(CONFIG.get("host") or "0.0.0.0")
    port = int(CONFIG.get("port") or 8770)
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    print("=" * 66)
    print(" ComfyLocal — ComfyUI na lokální síti, bez FTP a bez workeru")
    print(f" Web:      http://{shown_host}:{port}")
    if host in ("0.0.0.0", "::"):
        print(f" V síti:   http://{_local_ip()}:{port}")
    print(f" ComfyUI:  {CONFIG.comfy_base}")
    print(f" API:      {CONFIG.comfy_api_base}")
    print(f" Data:     {CONFIG.data_dir}")
    print(f" Log:      {log_path if log_path else 'jen do konzole (log_to_file: false)'}")
    print(f" TLS:      {tls_state}")
    print(f" PIN:      {'ano' if str(CONFIG.get('access_pin') or '').strip() else 'ne (otevřeno v síti)'}")
    print("=" * 66)

    if CONFIG.get("open_browser"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{shown_host}:{port}")).start()

    uvicorn.run("comfylocal.server:app", host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
