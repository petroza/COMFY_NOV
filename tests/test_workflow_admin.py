import json
from pathlib import Path

import pytest

from comfylocal.workflow_admin import (
    list_workflow_files,
    normalize_workflow_name,
    parse_api_workflow,
    remove_workflow,
    save_workflow,
    set_workflow_enabled,
)


def _api_workflow(model: str = "example.safetensors") -> bytes:
    return json.dumps({
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": model},
        },
        "2": {
            "class_type": "SaveImage",
            "inputs": {"images": ["1", 0]},
        },
    }).encode("utf-8")


def test_normalize_workflow_name_is_safe():
    assert normalize_workflow_name("../Moje video.json") == "Moje_video_template.json"
    assert normalize_workflow_name("ready_template.json.disabled") == "ready_template.json"
    with pytest.raises(ValueError):
        normalize_workflow_name("workflow.txt")


def test_rejects_comfy_ui_project_instead_of_api_export():
    with pytest.raises(ValueError, match=r"Export \(API\)"):
        parse_api_workflow(b'{"nodes": []}', "ui.json")


def test_save_toggle_and_remove_workflow(tmp_path: Path):
    target = save_workflow(tmp_path, "new workflow.json", _api_workflow())
    assert target.name == "new_workflow_template.json"
    assert target.is_file()

    disabled = set_workflow_enabled(tmp_path, target.name, False)
    assert disabled.name.endswith(".json.disabled")
    assert disabled.is_file()

    active = set_workflow_enabled(tmp_path, target.name, True)
    assert active == target
    assert active.is_file()

    backup = remove_workflow(tmp_path, target.name)
    assert backup.parent.name == "_removed"
    assert backup.is_file()
    assert not target.exists()


def test_save_requires_explicit_replace(tmp_path: Path):
    save_workflow(tmp_path, "same.json", _api_workflow("first.safetensors"))
    with pytest.raises(FileExistsError):
        save_workflow(tmp_path, "same.json", _api_workflow("second.safetensors"))
    target = save_workflow(
        tmp_path, "same.json", _api_workflow("second.safetensors"), replace=True
    )
    assert "second.safetensors" in target.read_text(encoding="utf-8")


def test_list_reports_state_models_and_missing_models(tmp_path: Path):
    target = save_workflow(tmp_path, "model.json", _api_workflow("missing.safetensors"))

    class FakeClient:
        def missing_models(self, workflow):
            return [{"value": "missing.safetensors"}]

    rows = list_workflow_files(tmp_path, FakeClient())
    assert len(rows) == 1
    assert rows[0]["filename"] == target.name
    assert rows[0]["canonical_name"] == target.name
    assert rows[0]["enabled"] is True
    assert rows[0]["name"] == "model_template"
    assert rows[0]["size"] == target.stat().st_size
    assert rows[0]["models"] == ["missing.safetensors"]
    assert rows[0]["missing_models"] == ["missing.safetensors"]
    assert rows[0]["nodes"] == 2
    assert rows[0]["kind"] == "photo_edit"
