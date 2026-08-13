# -*- coding: utf-8 -*-
"""Bezpečná správa API workflow souborů z administrace."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .comfy_client import ComfyClient, ComfyError
from .import_workflow import model_files_in
from .projects import KNOWN
from .workflow import sanitize_workflow, workflow_is_flf2v, workflow_is_photo_edit

MAX_WORKFLOW_BYTES = 8 * 1024 * 1024


def normalize_workflow_name(value: str) -> str:
    """Vrátí bezpečný název `*_template.json` bez možnosti opustit workflows/."""
    name = Path(str(value or "").replace("\\", "/")).name.strip()
    if name.endswith(".json.disabled"):
        name = name[:-len(".disabled")]
    if not name.lower().endswith(".json"):
        raise ValueError("Workflow musí být soubor JSON.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name[:-5]).strip("._-")
    if not stem:
        raise ValueError("Název workflow je prázdný nebo neplatný.")
    if not stem.endswith("_template"):
        stem += "_template"
    return stem + ".json"


def parse_api_workflow(raw: bytes, source: str) -> dict:
    if not raw:
        raise ValueError("Nahraný soubor je prázdný.")
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise ValueError("Workflow je příliš velký (maximum je 8 MB).")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Soubor není platný UTF-8 JSON: {exc}") from exc
    try:
        return sanitize_workflow(data, source)
    except ComfyError as exc:
        raise ValueError(str(exc)) from exc


def save_workflow(workflows_dir: Path, filename: str, raw: bytes, replace: bool = False) -> Path:
    name = normalize_workflow_name(filename)
    workflow = parse_api_workflow(raw, name)
    workflows_dir.mkdir(parents=True, exist_ok=True)
    target = workflows_dir / name
    disabled = workflows_dir / f"{name}.disabled"
    if (target.exists() or disabled.exists()) and not replace:
        raise FileExistsError(f"{name} už existuje. Zaškrtni Přepsat existující soubor.")
    if replace:
        disabled.unlink(missing_ok=True)
    tmp = workflows_dir / f".{name}.uploading"
    tmp.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def set_workflow_enabled(workflows_dir: Path, filename: str, enabled: bool) -> Path:
    name = normalize_workflow_name(filename)
    active = workflows_dir / name
    disabled = workflows_dir / f"{name}.disabled"
    source, target = (disabled, active) if enabled else (active, disabled)
    if not source.is_file():
        state = "vypnuté" if enabled else "aktivní"
        raise FileNotFoundError(f"Workflow {name} není ve stavu {state}.")
    if target.exists():
        raise FileExistsError(f"Cílový soubor už existuje: {target.name}")
    source.replace(target)
    return target


def remove_workflow(workflows_dir: Path, filename: str) -> Path:
    name = normalize_workflow_name(filename)
    candidates = (workflows_dir / name, workflows_dir / f"{name}.disabled")
    source = next((p for p in candidates if p.is_file()), None)
    if source is None:
        raise FileNotFoundError(f"Workflow {name} nebyl nalezen.")
    removed_dir = workflows_dir / "_removed"
    removed_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = removed_dir / f"{source.name}.{stamp}"
    shutil.move(str(source), str(target))
    return target


def list_workflow_files(workflows_dir: Path, client: Optional[ComfyClient] = None) -> List[Dict[str, Any]]:
    paths = list(workflows_dir.glob("*.json")) + list(workflows_dir.glob("*.json.disabled"))
    result: List[Dict[str, Any]] = []
    for path in sorted(paths, key=lambda p: p.name.lower()):
        enabled = path.name.endswith(".json")
        canonical = path.name if enabled else path.name[:-len(".disabled")]
        item: Dict[str, Any] = {
            "filename": path.name,
            "canonical_name": canonical,
            "enabled": enabled,
            "name": str((KNOWN.get(canonical) or {}).get("name") or Path(canonical).stem),
            "size": path.stat().st_size,
            "models": [],
            "missing_models": [],
        }
        try:
            workflow = sanitize_workflow(json.loads(path.read_text(encoding="utf-8")), path.name)
            item["nodes"] = len(workflow)
            item["kind"] = "photo_edit" if workflow_is_photo_edit(workflow) else (
                "2pict" if workflow_is_flf2v(workflow) else "1pict")
            item["models"] = model_files_in(workflow)
            if client is not None:
                item["missing_models"] = [m.get("value") or m.get("model") or str(m)
                                          for m in client.missing_models(workflow)]
        except Exception as exc:
            item["error"] = str(exc)
        result.append(item)
    return result

