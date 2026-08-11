# -*- coding: utf-8 -*-
"""Společné přípravy testů.

Každý test dostane vlastní prázdnou databázi v tmp_path, aby testy na sebe
nesahaly a nezávisely na pořadí spuštění.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app_modules(tmp_path, monkeypatch):
    """Naimportuje comfylocal s configem a databází v tmp_path.

    Moduly se reloadují, protože config i připojení k SQLite jsou modulové
    globály — bez reloadu by si testy podávaly jednu databázi.
    """
    cfg = {
        "comfy_url": "http://127.0.0.1:9/",
        "port": 8999,
        "open_browser": False,
        "translate_prompt": False,
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("COMFYLOCAL_CONFIG", str(cfg_path))

    import comfylocal.config as config
    importlib.reload(config)
    # Data (a tedy i SQLite) chceme v tmp_path, ne ve složce projektu.
    monkeypatch.setattr(config, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(type(config.CONFIG), "base_dir", property(lambda self: tmp_path))
    monkeypatch.setattr(type(config.CONFIG), "data_dir",
                        property(lambda self: config.Config._ensure(tmp_path / "data")))
    monkeypatch.setattr(type(config.CONFIG), "workflows_dir",
                        property(lambda self: ROOT / "workflows"))

    import comfylocal.db as db
    importlib.reload(db)
    db.init()

    import comfylocal.users as users
    importlib.reload(users)
    users.ensure_schema()

    return {"config": config, "db": db, "users": users}
