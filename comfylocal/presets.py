# -*- coding: utf-8 -*-
"""Presety kamery, stylu a formátu. Převzato 1:1 z PZ COMFY VIDEO REMOTE."""
from __future__ import annotations

from typing import Dict

CAMERA_PRESETS: Dict[str, str] = {
    "Decentní nájezd dopředu": "the camera pushes in only slightly toward the subject in a restrained and minimal slow dolly forward, the framing tightens just a touch over the duration, smooth, stabilized and continuous",
    "Pomalý nájezd dopředu": "the camera slowly pushes in toward the subject in a smooth dolly forward, gradually tightening the framing, stabilized and continuous",
    "Pomalý odjezd dozadu": "the camera slowly pulls back from the subject in a smooth dolly out, gradually revealing more of the surrounding environment, stabilized and continuous",
    "Obíhání kolem objektu": "the camera circles slowly around the subject in a smooth orbital motion, the subject stays centered in frame, steady continuous parallax",
    "Půlkruhový oblouk": "the camera arcs around the subject in a controlled half-circle, smooth and stabilized, gradually revealing the subject from a new angle",
    "Stoupání kamery (dron nahoru)": "the camera rises upward in a smooth aerial drone movement, gradually revealing the wider landscape below, stabilized and continuous",
    "Klesání kamery (pohled dolů)": "the camera descends slowly from a high overhead view looking straight down at the scene, smooth aerial motion, stabilized",
    "Jeřáb nahoru": "the camera cranes upward in a slow controlled vertical rise, the subject remains in frame, smooth and continuous",
    "Jeřáb dolů": "the camera cranes downward in a slow controlled vertical descent, smooth and stabilized, gradually framing the subject from a lower angle",
    "Pomalý posun do strany": "the camera tracks slowly to the side in a smooth horizontal dolly parallel to the subject, stabilized and continuous",
    "Statická kamera (stativ)": "the camera holds completely still on a locked-off tripod, no camera movement, only the subject and the environment evolve over time",
    "Jemný posun (drobný drift)": "the camera drifts with very subtle, almost imperceptible motion, minimal parallax, breathing-like and stabilized",
    "Z ruky (dokumentární)": "the camera follows in a natural handheld documentary style, slight organic motion, observational and credible, lightly stabilized but not locked",
}

STYLE_PRESETS: Dict[str, str] = {
    "None": "",
    "Cinematic": "cinematic film look, shot on 35mm lens, shallow depth of field, soft dramatic lighting, rich color grading",
    "Realistic": "realistic natural look, neutral color grading, balanced natural lighting, accurate proportions, photographic depth of field, sharp authentic detail",
    "Documentary / News": "documentary news footage style, natural daylight, credible journalistic look, neutral colors, sharp realistic detail, broadcast quality",
    "Fashion / Product": "luxury commercial product look, glossy highlights, controlled studio lighting, shallow depth of field, polished color grading, macro detail",
    "Music video": "stylized music video aesthetic, dramatic contrast, vibrant color grading, cinematic atmosphere, expressive lighting",
}

# Rozměry musí sedět na LTX 2.3 mřížku (viz workflow.ltx_safe_size) — jinak
# druhý průchod i2v šablony dostane latent a vodicí obrázek v jiné velikosti
# a render spadne na nesouhlasu tenzorů. Proto tady není 1080 ani 1440.
FORMAT_PRESETS: Dict[str, Dict[str, int]] = {
    "hd_landscape": {"width": 1280, "height": 720},
    "fhd_landscape": {"width": 1920, "height": 1088},
    "hd_portrait": {"width": 720, "height": 1280},
    "fhd_portrait": {"width": 1088, "height": 1920},
    "square": {"width": 1024, "height": 1024},
    "square_2000": {"width": 1984, "height": 1984},
    "classic_4_3": {"width": 1472, "height": 1088},
    "classic_3_4": {"width": 1088, "height": 1472},
}

FORMAT_LABELS: Dict[str, str] = {
    "hd_landscape": "HD 1280×720",
    "fhd_landscape": "FHD 1920×1088",
    "hd_portrait": "HD na výšku 720×1280",
    "fhd_portrait": "FHD na výšku 1088×1920",
    "square": "Kvadrát 1024×1024",
    "square_2000": "Kvadrát 1984×1984",
    "classic_4_3": "Klasika 4:3 1472×1088",
    "classic_3_4": "Klasika 3:4 1088×1472",
    "custom": "Vlastní rozměry",
}


def camera_preset_text(preset: str) -> str:
    return CAMERA_PRESETS.get(str(preset or "").strip(), "")


def style_preset_text(preset: str) -> str:
    return STYLE_PRESETS.get(str(preset or "").strip(), "")


def format_size(fmt: str) -> Dict[str, int]:
    return dict(FORMAT_PRESETS.get(str(fmt or "").strip(), FORMAT_PRESETS["hd_landscape"]))
