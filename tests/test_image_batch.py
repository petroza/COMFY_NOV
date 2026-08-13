# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib


def load_module(app_modules):
    import comfylocal.image_batch as image_batch
    return importlib.reload(image_batch)


def test_subject_extraction_and_hybrid_choice(app_modules):
    mod = load_module(app_modules)
    assert mod.extract_subject("Close-up of a little mouse, drawn large in the frame") == "a little mouse"
    assert mod.choose_model("a little mouse", "hybrid") == "zimage"
    assert mod.choose_model("The devil took them away", "hybrid") == "qwen"
    assert mod.choose_model("A tyrant always finds an excuse", "hybrid") == "qwen"
    assert mod.choose_model("Always argue with a cat", "hybrid") == "qwen"
    assert mod.choose_model("Roosters should not crow", "hybrid") == "qwen"
    assert mod.choose_model("Permission from the farmer", "hybrid") == "qwen"
    assert mod.choose_model("anything", "qwen") == "qwen"
    assert mod.choose_model("anything", "flux1") == "flux1"
    assert mod.choose_model("anything", "flux2") == "flux2"


def test_batch_is_persistent_and_grouped_by_model(app_modules):
    mod = load_module(app_modules)
    jobs = [
        {"item": "a", "image": "uuid-a", "prompt": "Close-up of The devil took them away, drawn large in the frame"},
        {"item": "b", "image": "uuid-b", "prompt": "Close-up of a little mouse, drawn large in the frame"},
    ]
    batch = mod.create_batch(jobs, "quiz.json", "hybrid", mod.STYLE_DEFAULT)
    assert batch["status"] == "paused"
    assert batch["total"] == 2
    mod.control_batch(batch["id"], "start")
    claimed_batch, item = mod._claim()
    assert claimed_batch["id"] == batch["id"]
    assert item["source_index"] == 2
    assert item["model"] == "zimage"


def test_limit_and_control_states(app_modules):
    mod = load_module(app_modules)
    jobs = [{"prompt": f"Close-up of mouse {i}, drawn large in the frame"} for i in range(5)]
    batch = mod.create_batch(jobs, "many.json", "zimage", "style", limit=3)
    assert batch["total"] == 3
    assert mod.control_batch(batch["id"], "start")["status"] == "running"
    assert mod.control_batch(batch["id"], "pause")["status"] == "paused"
    assert mod.control_batch(batch["id"], "stop")["status"] == "stopped"


def test_story_images_share_seed_and_other_story_differs(app_modules):
    mod = load_module(app_modules)
    jobs = [
        {"item": "story-a", "prompt": "Close-up of a mouse, drawn large in the frame"},
        {"item": "story-a", "prompt": "Close-up of a cat, drawn large in the frame"},
        {"item": "story-b", "prompt": "Close-up of a bird, drawn large in the frame"},
    ]
    batch = mod.create_batch(jobs, "stories.json", "flux2", "style")
    with mod.db._LOCK:
        conn = mod.db.connect()
        rows = conn.execute(
            "SELECT item_ref,model,seed FROM comfy_image_batch_items WHERE batch_id=? ORDER BY source_index",
            (batch["id"],)).fetchall()
    assert rows[0]["seed"] == rows[1]["seed"]
    assert rows[0]["seed"] != rows[2]["seed"]
    assert {row["model"] for row in rows} == {"flux2"}


def test_hybrid_never_mixes_models_inside_one_story(app_modules):
    mod = load_module(app_modules)
    jobs = [
        {"item": "same-story", "prompt": "Close-up of a mouse, drawn large in the frame"},
        {"item": "same-story", "prompt": "Close-up of They ran away from the castle, drawn large in the frame"},
    ]
    batch = mod.create_batch(jobs, "story.json", "hybrid", "style")
    with mod.db._LOCK:
        rows = mod.db.connect().execute(
            "SELECT model,seed FROM comfy_image_batch_items WHERE batch_id=? ORDER BY source_index",
            (batch["id"],)).fetchall()
    assert [row["model"] for row in rows] == ["qwen", "qwen"]
    assert rows[0]["seed"] == rows[1]["seed"]


def test_ocr_retries_do_not_switch_story_model():
    source = (__import__("pathlib").Path(__file__).parents[1] /
              "comfylocal" / "image_batch.py").read_text(encoding="utf-8")
    assert 'actual_model = str(item["model"])' in source
    assert 'attempt == max_attempts - 1 and' not in source


def test_restart_clean_keeps_one_model_per_story(app_modules):
    mod = load_module(app_modules)
    batch = mod.create_batch([
        {"item": "one", "prompt": "Close-up of a mouse, drawn large in the frame"},
        {"item": "one", "prompt": "Close-up of They ran away, drawn large in the frame"},
    ], "restart.json", "hybrid", "style")
    mod.restart_clean(batch["id"])
    with mod.db._LOCK:
        rows = mod.db.connect().execute(
            "SELECT model,seed FROM comfy_image_batch_items WHERE batch_id=? ORDER BY source_index",
            (batch["id"],)).fetchall()
    assert [row["model"] for row in rows] == ["qwen", "qwen"]
    assert rows[0]["seed"] == rows[1]["seed"]


def test_repair_priority_beats_regular_zimage(app_modules):
    mod = load_module(app_modules)
    batch = mod.create_batch([
        {"prompt": "Close-up of a cat, drawn large in the frame"},
        {"prompt": "Close-up of A tyrant always finds an excuse, drawn large in the frame"},
    ], "priority.json", "hybrid", "style")
    db = app_modules["db"]
    with db._LOCK:
        conn = db.connect()
        conn.execute("UPDATE comfy_image_batch_items SET priority=100 WHERE batch_id=? AND source_index=2",
                     (batch["id"],))
        conn.execute("UPDATE comfy_image_batches SET status='running' WHERE id=?", (batch["id"],))
        conn.commit()
    _, item = mod._claim()
    assert item["source_index"] == 2
    assert item["model"] == "qwen"


def test_ocr_filter_ignores_weak_noise_and_accepts_real_text(app_modules):
    mod = load_module(app_modules)
    result = [
        [[[0, 0], [1, 0], [1, 1], [0, 1]], "eye", 0.31],
        [[[0, 0], [1, 0], [1, 1], [0, 1]], "cat", 0.99],
        [[[0, 0], [1, 0], [1, 1], [0, 1]], "Always argue with a cat", 0.998],
    ]
    assert mod._accepted_ocr_lines(result) == ["Always argue with a cat"]


def test_clean_restart_resets_database_and_output(app_modules):
    mod = load_module(app_modules)
    batch = mod.create_batch([{"prompt": "Close-up of a cat, drawn large in the frame"}],
                             "clean.json", "hybrid", "style")
    output = mod.Path(batch["output_dir"])
    (output / "old.png").write_bytes(b"old")
    restarted = mod.restart_clean(batch["id"])
    assert restarted["status"] == "running"
    assert restarted["done_count"] == 0
    assert list(output.iterdir()) == []


def test_original_json_prompt_is_authoritative(app_modules):
    mod = load_module(app_modules)
    original = "Original exact prompt. No text, no letters."
    batch = {"style_prompt": "THIS MUST NOT BE APPENDED"}
    item = {"model": "zimage", "subject": "shortened", "source_prompt": original}
    result = mod._final_prompt(item, batch)
    assert result.startswith(original)
    assert result.endswith("THIS MUST NOT BE APPENDED")


def test_flux1_workflow_uses_official_checkpoint_and_800_square(app_modules):
    mod = load_module(app_modules)
    wf = mod._workflow_flux1("exact prompt", 123)
    assert wf["1"]["inputs"]["ckpt_name"] == "flux1-dev-fp8.safetensors"
    assert wf["2"]["inputs"]["text"] == "exact prompt"
    assert wf["5"]["inputs"]["width"] == 800
    assert wf["5"]["inputs"]["height"] == 800


def test_flux2_workflow_uses_installed_turbo_stack_and_800_square(app_modules):
    mod = load_module(app_modules)
    wf = mod._workflow_flux2("exact prompt", 456)
    assert wf["1"]["inputs"]["unet_name"] == "flux2_dev_fp8mixed.safetensors"
    assert wf["2"]["inputs"]["lora_name"] == "Flux_2-Turbo-LoRA_comfyui.safetensors"
    assert wf["3"]["inputs"]["clip_name"] == "mistral_3_small_flux2_fp8.safetensors"
    assert wf["4"]["inputs"]["text"] == "exact prompt"
    assert wf["9"]["inputs"]["steps"] == 8
    assert wf["10"]["inputs"]["width"] == 800
    assert wf["10"]["inputs"]["height"] == 800
