from comfylocal.workflow import auto_patch_workflow_nodes


def test_minimax_h3_patches_positive_prompt_and_direct_parameters():
    workflow = {
        "load": {
            "class_type": "LoadImage",
            "inputs": {"image": "example.png"},
        },
        "video": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "prompt": "Original cinematic prompt containing high quality details.",
                "width": ["resolution", 0],
                "height": ["resolution", 1],
                "first_frame": ["load", 0],
            },
        },
        "duration": {
            "class_type": "PrimitiveFloat",
            "inputs": {"value": 5.0},
        },
        "noise": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": 42},
        },
    }

    auto_patch_workflow_nodes(
        workflow,
        {
            "image": "uploaded.png",
            "positive_prompt": "A red cat walks through a snowy forest.",
            "negative_prompt": "low quality, artifacts",
            "width": 768,
            "height": 512,
            "duration": 3,
            "seed": 12345,
        },
    )

    inputs = workflow["video"]["inputs"]
    assert inputs["prompt"] == "A red cat walks through a snowy forest."
    assert inputs["width"] == 768
    assert inputs["height"] == 512
    assert workflow["duration"]["inputs"]["value"] == 3.0
    assert workflow["noise"]["inputs"]["noise_seed"] == 12345
    assert workflow["load"]["inputs"]["image"] == "uploaded.png"
