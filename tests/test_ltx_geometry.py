# -*- coding: utf-8 -*-
"""Geometrie LTX 2.3: co appka slíbí, to musí ComfyUI doručit.

Šablona jede první průchod v polovičním rozlišení a pak ho zdvojnásobí
upscalerem, takže reálný výstup je `2 * ((rozměr / 2) // 32) * 32`. Když
appka pošle rozměr, který tomu neodpovídá, uživatel dostane potichu jinou
velikost, než jakou zadal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfylocal.workflow import (align_ltx_av_length, ltx_delivered_size,  # noqa: E402
                                 ltx_geometry_note, ltx_safe_frames, ltx_safe_size)
from comfylocal.comfy_client import ltx_av_noise_hint  # noqa: E402


@pytest.mark.parametrize("requested", [1920, 1088, 1472, 1280, 1024, 1984, 1080, 1440, 720, 999, 1366])
def test_safe_size_is_always_actually_deliverable(requested):
    """Klíčové pravidlo: srovnaný rozměr se musí rovnat tomu, co reálně vyleze."""
    safe = ltx_safe_size(requested)
    assert safe == ltx_delivered_size(safe), f"{requested} → {safe}, ale doručí se {ltx_delivered_size(safe)}"


def test_720_is_snapped_because_ltx_cannot_do_it():
    """Regrese: 720 dřív prošlo, ale ComfyUI z něj dělal 704.

    Appka tedy tvrdila 1280×720 a doručila 1280×704.
    """
    assert ltx_delivered_size(720) == 704
    assert ltx_safe_size(720) == 704


def test_common_presets_stay_untouched():
    """Ostatní presety jsou násobky 64 a nesmí se změnit."""
    for size in (1920, 1088, 1472, 1280, 1024, 1984):
        assert ltx_safe_size(size) == size


def test_fhd_still_rounds_up_to_1088():
    """1080 není platné, ale 1088 je blíž FHD než 1024 — nesmí to spadnout dolů."""
    assert ltx_safe_size(1080) == 1088


def test_safe_size_survives_nonsense():
    assert ltx_safe_size(None) == 1280
    assert ltx_safe_size("abc", default=1024) == 1024
    assert ltx_safe_size(10) >= 256
    assert ltx_safe_size(99999) <= 4096


def test_geometry_note_reproduces_the_real_crash():
    """Vzorce musí dát přesně čísla ze skutečné chyby na uživatelově stroji.

    Reálná chyba byla: tensor a (7680) vs tensor b (999168) při 1920×1080,
    25 fps, 5 s. Když to vyjde, víme, že chybu umíme rozklíčovat.
    """
    note = ltx_geometry_note(1920, 1080, 25, 5)
    assert "noise tokens=7680" in note
    assert "AV pack=999168" in note


def test_geometry_note_differs_for_1088():
    """Důkaz, že crash NEmohl přijít z 1920×1088 — čísla by byla jiná."""
    note = ltx_geometry_note(1920, 1088, 25, 5)
    assert "noise tokens=8160" in note
    assert "AV pack=1060608" in note


def test_av_noise_hint_recognises_known_comfyui_bug():
    msg = "The size of tensor a (7680) must match the size of tensor b (999168) at non-singleton dimension 2"
    hint = ltx_av_noise_hint(msg)
    assert "chyba ComfyUI" in hint
    assert "126" in hint          # 999168/128 - 7680 = 126 audio latentů
    assert "#13692" in hint


def test_av_noise_hint_ignores_unrelated_mismatches():
    """Nesmí to hlásit chybu ComfyUI u čehokoliv, co jen náhodou zmiňuje tenzory."""
    assert ltx_av_noise_hint("shape '[187, 4096]' is invalid for input of size 672767") == ""
    # b není dělitelné 128 → jiná příčina
    assert ltx_av_noise_hint("size of tensor a (100) must match the size of tensor b (777)") == ""
    # b < a → nesmysl pro tuhle signaturu
    assert ltx_av_noise_hint("size of tensor a (999) must match the size of tensor b (128)") == ""


# ── délka videa vs. audia ───────────────────────────────────
def latent_t(frames: int) -> int:
    """Časový rozměr video latentu, jak ho počítá LTX."""
    return (frames - 1) // 8 + 1


@pytest.mark.parametrize("fps,duration", [(25, 5), (25, 3), (24, 5), (30, 5), (25, 10),
                                          (25, 1), (50, 5), (25, 2.5), (60, 8)])
def test_frame_snapping_never_changes_the_video(fps, duration):
    """Nejdůležitější pojistka celé téhle úpravy.

    Srovnání frejmů smí opravit jen délku audia. Kdyby se změnilo `T`, změnil
    by se i celý video latent — tedy jiný render, jiná cena za GPU čas.
    """
    original = int(round(fps * duration)) + 1     # co počítá šablona dnes
    snapped = ltx_safe_frames(fps, duration)
    assert snapped <= original, "srovnávat se smí jen dolů, jinak by render byl delší"
    assert latent_t(snapped) == latent_t(original), "změnil by se video latent — to nesmí nastat"


@pytest.mark.parametrize("fps,duration", [(25, 5), (24, 5), (30, 5), (25, 2.5), (60, 8)])
def test_snapped_frames_decode_to_themselves(fps, duration):
    """Po srovnání se video dekóduje přesně na tolik frejmů, kolik dostalo audio."""
    frames = ltx_safe_frames(fps, duration)
    decoded = (latent_t(frames) - 1) * 8 + 1
    assert decoded == frames


def test_default_5s_at_25fps_matches_reality():
    """25 fps × 5 s: šablona říká 126, video umí 121 — audio se srovná na 121."""
    assert ltx_safe_frames(25, 5) == 121
    assert latent_t(126) == latent_t(121) == 16


def test_safe_frames_survives_nonsense():
    assert ltx_safe_frames(None, None) == 121
    assert ltx_safe_frames("x", 5) == 121
    assert ltx_safe_frames(0, 0) >= 1


def test_align_av_length_rewrites_only_the_length_node():
    wf = {
        "320:323": {"class_type": "ComfyMathExpression",
                    "inputs": {"expression": "a * b + 1", "values.a": ["320:301", 0]}},
        "320:292": {"class_type": "ComfyMathExpression", "inputs": {"expression": "a/2"}},
        "320:283": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["320:276", 0]}},
    }
    patched = align_ltx_av_length(wf, 25, 5)

    assert wf["320:323"]["inputs"]["expression"] == "121"
    assert wf["320:292"]["inputs"]["expression"] == "a/2", "půlení rozlišení se nesmí dotknout"
    assert wf["320:283"]["inputs"]["noise"] == ["320:276", 0], "sampler zůstává beze změny"
    assert len(patched) == 1


def test_align_av_length_is_safe_on_foreign_workflow():
    """Šablona bez toho nodu se nesmí rozbít ani nic nenahlásit."""
    wf = {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}
    assert align_ltx_av_length(wf, 25, 5) == []
    assert wf == {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}
