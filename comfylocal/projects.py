# -*- coding: utf-8 -*-
"""Projekty = workflow šablony ve složce workflows/.

Původní web držel projekty v DB a importoval je z project_workflows/*.json.
Tady je to stejné, jen se seznam dopočítá z lokální složky, aby stačilo hodit
do workflows/ nový API export a appka ho sama nabídla.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from . import db
from .config import CONFIG

log = logging.getLogger("comfylocal.projects")

# Pojmenování musí odpovídat tomu, co frontend pozná jako 1 PICT / 2 PICT / photo edit.
KNOWN: Dict[str, Dict[str, object]] = {
    "ltx23_i2v_template.json": {
        "name": "LTX 2.3 z jedné fotky / 1 PICT",
        "description": "LTX 2.3 image-to-video — jedna vstupní fotka.",
        "sort_order": 10,
    },
    "ltx23_flf2v_template.json": {
        "name": "LTX 2.3 první + poslední frejm / 2 PICT",
        "description": "LTX 2.3 FLF2V — první a poslední frejm / dvě vstupní fotky.",
        "sort_order": 20,
    },
    "flux2_edit_template.json": {
        "name": "Flux.2 úprava fotky / photo edit",
        "description": "Flux.2 edit workflow — na vstupu fotka, na výstupu upravená fotka.",
        "sort_order": 30,
    },
    "firered_edit_template.json": {
        "name": "FireRed úprava fotky / photo edit",
        "description": "FireRed/Qwen edit workflow — na vstupu fotka, na výstupu upravená fotka.",
        "sort_order": 40,
    },
}


def detect_kind(path: Path) -> str:
    """i2v / flf2v / photo_edit podle obsahu API workflow."""
    try:
        from .workflow import sanitize_workflow, workflow_is_flf2v, workflow_is_photo_edit
        wf = sanitize_workflow(json.loads(path.read_text(encoding="utf-8")), path.name)
    except Exception as e:
        log.warning("Workflow %s nejde přečíst: %s", path.name, e)
        return "unknown"
    if workflow_is_photo_edit(wf):
        return "photo_edit"
    return "flf2v" if workflow_is_flf2v(wf) else "i2v"


def _fallback_meta(path: Path, kind: str, index: int) -> Dict[str, object]:
    label = path.stem.replace("_", " ")
    if kind == "flf2v":
        return {"name": f"{label} / 2 PICT",
                "description": "Workflow s prvním a posledním frejmem (FLF2V).",
                "sort_order": 100 + index}
    if kind == "photo_edit":
        return {"name": f"{label} / photo edit",
                "description": "Workflow pro úpravu fotky (photo edit).",
                "sort_order": 200 + index}
    return {"name": f"{label} / 1 PICT",
            "description": "Image-to-video workflow s jednou vstupní fotkou.",
            "sort_order": 100 + index}


def sync_projects() -> List[Dict[str, object]]:
    """Projde workflows/ a doplní/aktualizuje projekty v DB."""
    files = sorted(CONFIG.workflows_dir.glob("*.json"))
    existing_rel: List[str] = []
    for index, path in enumerate(files):
        rel = f"workflows/{path.name}"
        existing_rel.append(rel)
        kind = detect_kind(path)
        meta = dict(KNOWN.get(path.name) or _fallback_meta(path, kind, index))
        db.upsert_project(
            name=str(meta["name"]),
            workflow_file=rel,
            description=str(meta["description"]),
            input_type="image",
            sort_order=int(meta["sort_order"]),
        )
    db.deactivate_projects_except(existing_rel)
    projects = db.list_projects()
    log.info("Projekty ve frontendu: %s", ", ".join(p["name"] for p in projects) or "žádné")
    return projects


def workflow_file_for_project(project_id: Optional[int]) -> Optional[str]:
    if not project_id:
        return None
    project = db.get_project(int(project_id))
    if not project:
        return None
    rel = str(project.get("workflow_file") or "")
    return Path(rel).name or None


def default_project_id(kind: str = "i2v") -> Optional[int]:
    """Výchozí projekt pro daný režim, aby job vždy měl workflow."""
    wanted = {
        "i2v": CONFIG.get("default_workflow"),
        "flf2v": CONFIG.get("flf2v_workflow"),
    }.get(kind)
    for project in db.list_projects():
        if Path(str(project.get("workflow_file") or "")).name == wanted:
            return int(project["id"])
    projects = db.list_projects()
    return int(projects[0]["id"]) if projects else None
