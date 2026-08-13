# -*- coding: utf-8 -*-
"""Překlad přes jazykový model ve ComfyUI — bez internetu.

Testuje se skládání workflow (aby appka nepoužila node, který ComfyUI nemá)
a hlavně čištění odpovědi: model rád přidá „Here is the translation:",
uvozovky nebo zopakuje zadání, a nic z toho nesmí skončit v promptu.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfylocal import translate_comfy  # noqa: E402
from comfylocal.comfy_client import ComfyError  # noqa: E402

FULL_INFO = {
    "LTXAVTextEncoderLoader": {"input": {"required": {
        "text_encoder": [["gemma_3_12B_it_fp4_mixed.safetensors", "t5xxl.safetensors"]],
        "ckpt_name": [["ltx-2.3-22b-dev-fp8.safetensors"]],
        "device": [["default", "cpu"]]}}},
    "TextGenerateLTX2Prompt": {"input": {"required": {
        "sampling_mode": [["on", "off"]]}}},
    "PreviewAny": {"input": {"required": {}}},
}


class FakeClient:
    """ComfyClient bez sítě — vrací object_info a předstírá odeslání promptu."""

    def __init__(self, info=None, reply="a cat sits on the window"):
        self._info = FULL_INFO if info is None else info
        self.reply = reply
        self.submitted = None

    def object_info(self):
        return self._info

    def combo_options(self, class_type, input_name):
        try:
            cfg = self._info[class_type]["input"]["required"][input_name]
            return [str(x) for x in cfg[0]]
        except Exception:
            return []

    def submit(self, workflow, client_id):
        self.submitted = workflow
        return "fake-prompt-id"

    def history(self, prompt_id, allow_empty=False):
        return {"status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"3": {"text": [self.reply]}}}


# ── skládání workflow ───────────────────────────────────────
def test_workflow_uses_gemma_and_reads_text_back():
    client = FakeClient()
    wf = translate_comfy.build_translate_workflow(client, "kočka sedí na okně", "cs", "en")

    assert wf["1"]["class_type"] == "LTXAVTextEncoderLoader"
    assert "gemma" in wf["1"]["inputs"]["text_encoder"].lower(), "má se vybrat Gemma, ta umí česky"
    assert wf["2"]["class_type"] == "TextGenerateLTX2Prompt"
    assert wf["2"]["inputs"]["clip"] == ["1", 0], "generátor musí dostat načtený model"
    # Bez sinku by text zůstal v grafu a appka by se k němu nedostala.
    assert wf["3"]["class_type"] == "PreviewAny"
    assert wf["3"]["inputs"]["source"] == ["2", 0]


def test_workflow_disables_the_video_prompt_template():
    """S výchozí šablonou by z toho model udělal video prompt, ne překlad."""
    wf = translate_comfy.build_translate_workflow(FakeClient(), "kočka", "cs", "en")
    assert wf["2"]["inputs"]["use_default_template"] is False


def test_workflow_prefers_deterministic_sampling():
    wf = translate_comfy.build_translate_workflow(FakeClient(), "kočka", "cs", "en")
    assert wf["2"]["inputs"]["sampling_mode"] == "off"


def test_workflow_tames_sampling_when_it_cannot_be_switched_off():
    info = {**FULL_INFO, "TextGenerateLTX2Prompt": {
        "input": {"required": {"sampling_mode": [["on"]]}}}}
    wf = translate_comfy.build_translate_workflow(FakeClient(info), "kočka", "cs", "en")
    assert wf["2"]["inputs"]["sampling_mode"] == "on"
    assert wf["2"]["inputs"]["sampling_mode.temperature"] == 0.1


def test_prompt_contains_the_text_and_both_languages():
    wf = translate_comfy.build_translate_workflow(FakeClient(), "kočka na okně", "cs", "en")
    instruction = wf["2"]["inputs"]["prompt"]
    assert "kočka na okně" in instruction
    assert "Czech" in instruction and "English" in instruction


def test_missing_nodes_are_reported_not_guessed():
    """Když ComfyUI ty nody nemá, musí to říct — ne poslat rozbité workflow."""
    client = FakeClient({"KSampler": {"input": {"required": {}}}})
    avail = translate_comfy.availability(client)
    assert avail["ok"] is False
    assert "TextGenerateLTX2Prompt" in avail["reason"]
    with pytest.raises(ComfyError):
        translate_comfy.build_translate_workflow(client, "kočka", "cs", "en")


def test_missing_text_sink_is_detected():
    info = {k: v for k, v in FULL_INFO.items() if k != "PreviewAny"}
    avail = translate_comfy.availability(FakeClient(info))
    assert avail["ok"] is False
    assert "PreviewAny" in avail["reason"]


def test_alternative_sink_is_accepted():
    info = {k: v for k, v in FULL_INFO.items() if k != "PreviewAny"}
    info["ShowText"] = {"input": {"required": {}}}
    wf = translate_comfy.build_translate_workflow(FakeClient(info), "kočka", "cs", "en")
    assert wf["3"]["class_type"] == "ShowText"
    assert wf["3"]["inputs"]["text"] == ["2", 0]


# ── čištění odpovědi modelu ─────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("a cat sits on the window", "a cat sits on the window"),
    ('"a cat sits on the window"', "a cat sits on the window"),
    ("Here is the translation: a cat sits on the window", "a cat sits on the window"),
    ("Translation: a cat sits on the window", "a cat sits on the window"),
    ("Sure! a cat sits on the window", "a cat sits on the window"),
    ("  a cat sits   on the window \n", "a cat sits on the window"),
    ("a cat sits on the window<end_of_turn>", "a cat sits on the window"),
    ("<start_of_turn>model\na cat sits on the window<end_of_turn>", "a cat sits on the window"),
    ("a cat sits on the window\n\nNote: informal tone.", "a cat sits on the window"),
])
def test_clean_translation_strips_the_noise(raw, expected):
    assert translate_comfy.clean_translation(raw, "kočka sedí na okně") == expected


def test_clean_translation_drops_the_echoed_input():
    raw = "kočka sedí na okně\na cat sits on the window"
    assert translate_comfy.clean_translation(raw, "kočka sedí na okně") == "a cat sits on the window"


def test_clean_translation_on_empty_input():
    assert translate_comfy.clean_translation("", "kočka") == ""
    assert translate_comfy.clean_translation(None, "kočka") == ""


# ── celý průběh ─────────────────────────────────────────────
def test_translate_end_to_end_without_internet(monkeypatch):
    monkeypatch.setattr(translate_comfy.time, "sleep", lambda *_: None)
    client = FakeClient(reply="Here is the translation: a cat sits on the window")
    result = translate_comfy.translate_via_comfy("kočka sedí na okně", "cs", "en", client=client)

    assert result["success"] is True
    assert result["translated"] == "a cat sits on the window"
    assert result["provider"] == "comfy"
    assert "gemma" in result["model"].lower()


def test_same_language_is_not_translated():
    result = translate_comfy.translate_via_comfy("kočka", "cs", "cs", client=FakeClient())
    assert result["success"] is True
    assert result["translated"] == "kočka"


def test_model_returning_only_the_input_is_an_error(monkeypatch):
    """Když model jen zopakuje zadání, nesmíme to vydávat za překlad."""
    monkeypatch.setattr(translate_comfy.time, "sleep", lambda *_: None)
    client = FakeClient(reply="kočka sedí na okně")
    with pytest.raises(ComfyError, match="původní text"):
        translate_comfy.translate_via_comfy("kočka sedí na okně", "cs", "en", client=client)


def test_render_error_is_surfaced(monkeypatch):
    monkeypatch.setattr(translate_comfy.time, "sleep", lambda *_: None)

    class Failing(FakeClient):
        def history(self, prompt_id, allow_empty=False):
            return {"status": {"status_str": "error", "completed": False, "messages": [
                ["execution_error", {"node_id": "2", "exception_message": "out of memory"}]]}}

    with pytest.raises(ComfyError, match="out of memory"):
        translate_comfy.translate_via_comfy("kočka", "cs", "en", client=Failing())


def test_empty_output_is_an_error(monkeypatch):
    monkeypatch.setattr(translate_comfy.time, "sleep", lambda *_: None)

    class Silent(FakeClient):
        def history(self, prompt_id, allow_empty=False):
            return {"status": {"status_str": "success", "completed": True, "messages": []},
                    "outputs": {}}

    with pytest.raises(ComfyError, match="nevrátil žádný text"):
        translate_comfy.translate_via_comfy("kočka", "cs", "en", client=Silent())
