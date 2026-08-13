# -*- coding: utf-8 -*-
"""Převod workflow z UI exportu ComfyUI do API formátu.

Testuje se na skutečných exportech ve `docs/comfyui_projects/` (LTX 2.5 a
MiniMax H3), včetně rozbalení subgrafů — celý pipeline těch workflow je totiž
schovaný v `definitions.subgraphs` a bez rozbalení by z workflow zůstala jen
prázdná skořápka.

Zásadní vlastnost, kterou to hlídá: když si převod není jistý jmény parametrů,
musí **skončit chybou**, ne vrátit workflow s posunutými hodnotami. Špatně
přiřazený parametr by znamenal spadlý (nebo tiše špatný) render.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfylocal.import_workflow import (ImportError_, convert_ui_workflow,  # noqa: E402
                                        is_link_only, model_files_in,
                                        names_from_object_info)
from comfylocal.__main__ import guess_template_name  # noqa: E402

EXPORTS = ROOT / "docs" / "comfyui_projects"
CONTROL = {"fixed", "randomize", "increment", "decrement"}
LINK_TYPES = {"IMAGE", "MASK", "LATENT", "MODEL", "CLIP", "VAE", "CONDITIONING",
              "AUDIO", "VIDEO", "NOISE", "GUIDER", "SAMPLER", "SIGMAS",
              "LATENT_UPSCALE_MODEL", "IC_LORA_PARAMETERS"}

UI_FILES = ["video_ltx2_5_i2v.json", "video_ltx2_5_flf2v.json",
            "video_minimax_h3_i2v.json", "video_minimax_h3_ref2v.json"]


def load_ui(name):
    path = EXPORTS / name
    if not path.is_file():
        pytest.skip(f"{name} není v repu")
    return json.loads(path.read_text(encoding="utf-8"))


def object_info_for(ui):
    """object_info poskládané z UI exportu — nahrazuje živé ComfyUI.

    Jména bere odtud, kde je UI zná, zbytek doplní zástupnými. Testuje se tím
    mechanika (rozbalení subgrafu, propojení drátů), ne konkrétní jména.
    """
    info = {}
    nodes = list(ui.get("nodes") or [])
    for sub in (ui.get("definitions") or {}).get("subgraphs") or []:
        nodes += list(sub.get("nodes") or [])
    for node in nodes:
        class_type = node.get("type", "")
        values = node.get("widgets_values")
        if not isinstance(values, list):
            continue
        named = [(i.get("widget") or {}).get("name")
                 for i in (node.get("inputs") or []) if (i.get("widget") or {}).get("name")]
        real = [v for v in values if not (isinstance(v, str) and v in CONTROL)]
        names = list(named)
        while len(names) < len(real):
            names.append(f"w{len(names)}")
        required = info.setdefault(class_type, {"input": {"required": {}}})["input"]["required"]
        for name in names:
            required.setdefault(name, ["STRING"])
        for item in node.get("inputs") or []:
            if not (item.get("widget") or {}).get("name") and item.get("name"):
                type_name = str(item.get("type") or "").split(",")[0].upper()
                if type_name in LINK_TYPES:
                    required.setdefault(item["name"], [type_name])
    return info


# ── typy vstupů ─────────────────────────────────────────────
def test_link_only_types():
    assert is_link_only("IMAGE") is True
    assert is_link_only("IMAGE,MASK") is True      # kombinovaný typ je taky drát
    assert is_link_only("INT") is False
    assert is_link_only("STRING") is False
    assert is_link_only("") is False


def test_object_info_lists_widgets_not_sockets():
    info = {"X": {"input": {"required": {"model": ["MODEL"], "steps": ["INT"],
                                        "mode": [["on", "off"]]},
                            "optional": {"seed": ["INT"]}}}}
    assert names_from_object_info(info, "X") == ["steps", "mode", "seed"]


# ── skutečné exporty ────────────────────────────────────────
@pytest.mark.parametrize("name", UI_FILES)
def test_real_exports_convert_to_api_format(name):
    ui = load_ui(name)
    api = convert_ui_workflow(ui, object_info=object_info_for(ui))

    assert api, "převod nesmí vrátit prázdno"
    for node_id, node in api.items():
        assert "class_type" in node, f"{node_id} nemá class_type"
        assert isinstance(node.get("inputs"), dict), f"{node_id} nemá inputs"


@pytest.mark.parametrize("name", UI_FILES)
def test_no_input_is_left_unconnected(name):
    """Nepropojený vstup by v ComfyUI skončil chybou „required input missing"."""
    ui = load_ui(name)
    api = convert_ui_workflow(ui, object_info=object_info_for(ui))
    for node_id, node in api.items():
        for key, value in node["inputs"].items():
            assert value is not None, f"{node_id}.{key} zůstal nepropojený"


@pytest.mark.parametrize("name", UI_FILES)
def test_all_links_point_at_existing_nodes(name):
    ui = load_ui(name)
    api = convert_ui_workflow(ui, object_info=object_info_for(ui))
    for node_id, node in api.items():
        for key, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in api, f"{node_id}.{key} míří na neexistující node {value[0]}"


@pytest.mark.parametrize("name", UI_FILES)
def test_subgraph_is_expanded_and_output_rewired(name):
    """SaveVideo musí skončit napojený na video z vnitřku subgrafu."""
    ui = load_ui(name)
    api = convert_ui_workflow(ui, object_info=object_info_for(ui))

    savers = [n for n in api.values() if n["class_type"] == "SaveVideo"]
    assert savers, "workflow nemá SaveVideo, appka by nedostala výsledek"
    source = savers[0]["inputs"].get("video")
    assert isinstance(source, list), "SaveVideo.video musí být drát, ne hodnota"
    assert api[source[0]]["class_type"] in ("CreateVideo", "SaveVideo", "VAEDecode")


def test_editor_only_nodes_are_dropped():
    ui = load_ui("video_ltx2_5_i2v.json")
    api = convert_ui_workflow(ui, object_info=object_info_for(ui))
    assert not [n for n in api.values() if n["class_type"] in ("MarkdownNote", "Note")]


@pytest.mark.parametrize("name", UI_FILES)
def test_models_are_found_for_availability_check(name):
    ui = load_ui(name)
    api = convert_ui_workflow(ui, object_info=object_info_for(ui))
    models = model_files_in(api)
    assert models, "z workflow se nevyčetl ani jeden model — nešlo by ověřit dostupnost"
    assert all(m.endswith((".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".sft")) for m in models)


# ── odmítnutí nejistého převodu ─────────────────────────────
def test_refuses_when_parameter_names_are_unknown():
    """Bez jmen parametrů se převod nesmí „nějak" dokončit."""
    ui = {"nodes": [{"id": 1, "type": "ZcelaNeznamyNode",
                     "inputs": [], "outputs": [],
                     "widgets_values": ["neco", 42, True]}],
          "links": []}
    with pytest.raises(ImportError_) as err:
        convert_ui_workflow(ui, object_info={})
    assert "ZcelaNeznamyNode" in str(err.value)
    assert "Export (API)" in str(err.value), "chyba má poradit spolehlivou cestu"


def test_refuses_non_ui_input():
    with pytest.raises(ImportError_):
        convert_ui_workflow({"1": {"class_type": "KSampler", "inputs": {}}})


def test_wired_input_keeps_widget_positions_aligned():
    """Hodnota přebitá drátem pořád zabírá pozici v `widgets_values`.

    Regrese: dřív se přeskočila, takže všechny další parametry se posunuly
    o jeden — render by dostal steps do cfg a podobně.
    """
    ui = {
        "nodes": [
            {"id": 1, "type": "Zdroj", "inputs": [], "outputs": [{"links": [10]}],
             "widgets_values": [7]},
            {"id": 2, "type": "Cil", "outputs": [],
             "inputs": [{"name": "a", "type": "INT", "widget": {"name": "a"}, "link": 10}],
             "widgets_values": [1, 2, 3]},
        ],
        "links": [[10, 1, 0, 2, 0, "INT"]],
    }
    info = {"Zdroj": {"input": {"required": {"v": ["INT"]}}},
            "Cil": {"input": {"required": {"a": ["INT"], "b": ["INT"], "c": ["INT"]}}}}
    api = convert_ui_workflow(ui, object_info=info)
    target = api["2"]["inputs"]
    assert target["a"] == ["1", 0], "drát musí přebít hodnotu"
    assert target["b"] == 2 and target["c"] == 3, "další parametry se nesmí posunout"


# ── pojmenování šablon ──────────────────────────────────────
@pytest.mark.parametrize("source,expected", [
    ("video_ltx2_5_i2v", "ltx25_i2v_template.json"),
    ("video_ltx2_5_flf2v_dalsi", "ltx25_flf2v_template.json"),
    ("video_minimax_h3_i2v", "minimax_h3_i2v_template.json"),
    ("video_minimax_h3_r2v_dva", "minimax_h3_ref2v_template.json"),
    ("ltx25-flf2v", "ltx25_flf2v_template.json"),
])
def test_template_names_match_registered_projects(source, expected):
    """Jméno šablony musí souhlasit s tím, co projects.py zná — jinak by projekt
    dostal generický název místo pěkného."""
    from comfylocal.projects import KNOWN
    assert guess_template_name(source) == expected
    assert expected in KNOWN, f"{expected} není v projects.KNOWN"


def test_unknown_source_gets_a_safe_name():
    assert guess_template_name("nejaky_muj_export").endswith("_template.json")
