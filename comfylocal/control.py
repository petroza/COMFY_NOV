# -*- coding: utf-8 -*-
"""Ovládání ComfyUI a render loopu z UI — lokální náhrada worker příkazů z webu.

Webová (FTP) verze posílala workeru příkazy `start_comfy` a `restart_worker`.
Tady je „worker" render loop uvnitř appky a ComfyUI běží na stejné síti,
takže:

* start ComfyUI = spuštění příkazu z configu (`comfy_start_cmd` ve složce
  `comfy_dir`) na tomhle PC,
* restart workeru = restart render loopu (běžící job se nechá dokončit
  ComfyUI, appka se k němu při dalším cyklu vrátí).
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from .config import CONFIG

log = logging.getLogger("comfylocal.control")

_LOCK = threading.RLock()
_PROC: Optional[subprocess.Popen] = None
_LAST: Dict[str, Any] = {"started_at": None, "command": None}


def start_command() -> List[str]:
    raw = CONFIG.get("comfy_start_cmd") or ""
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if sys.platform.startswith("win"):
        return [text] if text.lower().endswith((".bat", ".cmd", ".exe")) else shlex.split(text, posix=False)
    return shlex.split(text)


def comfy_process_running() -> bool:
    with _LOCK:
        return _PROC is not None and _PROC.poll() is None


def status() -> Dict[str, Any]:
    return {
        "can_start": bool(start_command()),
        "process_running": comfy_process_running(),
        "started_at": _LAST.get("started_at"),
        "command": _LAST.get("command"),
        "comfy_dir": str(CONFIG.get("comfy_dir") or ""),
    }


def start_comfy() -> Dict[str, Any]:
    """Spustí ComfyUI na tomhle PC podle configu."""
    from .runner import get_runner

    client = get_runner().client
    if client.online():
        return {"success": True, "already_online": True,
                "message": "ComfyUI už odpovídá, spouštět ho není potřeba."}

    cmd = start_command()
    if not cmd:
        return {"success": False,
                "error": ("V config.json není `comfy_start_cmd` — doplň příkaz, kterým se "
                          "ComfyUI na tomhle PC spouští (např. cesta k run_nvidia_gpu.bat), "
                          "případně i `comfy_dir`.")}
    if comfy_process_running():
        return {"success": True, "message": "ComfyUI se už spouští, čekej na zelený stav."}

    workdir = str(CONFIG.get("comfy_dir") or "") or None
    try:
        global _PROC
        with _LOCK:
            kwargs: Dict[str, Any] = {"cwd": workdir, "stdout": subprocess.DEVNULL,
                                      "stderr": subprocess.DEVNULL}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            else:
                kwargs["start_new_session"] = True
            _PROC = subprocess.Popen(cmd, **kwargs)
            _LAST["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _LAST["command"] = " ".join(cmd)
    except Exception as e:
        log.warning("Start ComfyUI selhal: %s", e)
        return {"success": False, "error": f"ComfyUI se nepodařilo spustit: {e}"}

    log.info("ComfyUI spuštěno příkazem: %s (cwd=%s)", " ".join(cmd), workdir or ".")
    return {"success": True,
            "message": "ComfyUI se spouští. Jakmile naběhne, stav nahoře zezelená.",
            "status": status()}


def restart_runner() -> Dict[str, Any]:
    """Restartuje render loop (obdoba restartu workeru na webu)."""
    from . import runner as runner_mod

    old = runner_mod.get_runner()
    active = old.active_job_id
    old.stop_event.set()
    old.join(timeout=5)
    runner_mod._RUNNER = None  # nový thread si postaví i nového ComfyUI klienta
    new = runner_mod.start_runner()
    log.info("Render loop restartován (předchozí aktivní job: %s)", active or "-")
    return {"success": True,
            "message": ("Render loop byl restartován." if not active else
                        f"Render loop byl restartován (job #{active} pokračuje v ComfyUI)."),
            "alive": new.is_alive(), "previous_active_job": active or 0}
