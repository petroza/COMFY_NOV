# -*- coding: utf-8 -*-
"""Logování appky do konzole a (volitelně) do souboru.

Na Windows appka běží v okně `START_WINDOWS.bat` — po pádu se okno hned
zavře a konzolový výstup je pryč. Log soubor v `data/logs/comfylocal.log`
přežije zavření okna i restart appky (rotuje, takže neroste do nekonečna)
a jde ho poslat jako přílohu k nahlášení chyby nebo stáhnout ze stránky Setup.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from .config import CONFIG

_configured = False


def configure_logging() -> Optional[Path]:
    """Nastaví root logger. Volá se jednou za běh appky (další volání nic nedělají).

    Vrací cestu k log souboru, nebo None když je `log_to_file` vypnuté.
    """
    global _configured
    if _configured:
        return log_file_path() if bool(CONFIG.get("log_to_file", True)) else None
    _configured = True

    level = getattr(logging, str(CONFIG.get("log_level") or "INFO").strip().upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    path = None
    if bool(CONFIG.get("log_to_file", True)):
        path = log_file_path()
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=int(CONFIG.get("log_max_bytes") or 5 * 1024 * 1024),
            backupCount=int(CONFIG.get("log_backup_count") or 5),
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)

    # Ukecané knihovny ať v logu nezanikne appka samotná; comfylocal.* zůstává na `level`.
    for noisy in ("urllib3", "websocket", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    return path


def log_file_path() -> Path:
    return CONFIG.log_file
