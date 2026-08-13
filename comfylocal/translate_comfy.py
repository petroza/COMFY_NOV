# -*- coding: utf-8 -*-
"""Překlad promptu přes jazykový model, který už běží ve ComfyUI.

Původní překladač volal Google Translate a MyMemory, takže appka potřebovala
výstup do internetu. LTX 2.3 šablona ale načítá **Gemma 3 12B Instruct** jako
text encoder — a ta umí česky. Stačí jí tedy poslat malé textové workflow
a přeložit si prompt vlastními silami, bez jediného paketu mimo lokální síť.

Skládá se to ze tří nodů, které šablona používá i pro Prompt Enhance:

    LTXAVTextEncoderLoader  →  TextGenerateLTX2Prompt  →  PreviewAny
    (načte Gemmu)              (vygeneruje překlad)       (vrátí text)

`PreviewAny` je důležitý: bez něj by text zůstal uvnitř grafu a nedostal se
do `/history`, odkud si ho appka přečte.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .comfy_client import ComfyClient, ComfyError
from .config import CONFIG

log = logging.getLogger("comfylocal.translate.comfy")

TEXT_ENCODER_LOADER = "LTXAVTextEncoderLoader"
TEXT_GENERATOR = "TextGenerateLTX2Prompt"

# Nody, které umí dostat hotový string do /history. PreviewAny je v ComfyUI
# součástí jádra a je i v naší LTX šabloně, ostatní jsou náhradní varianty
# pro jinak poskládané instalace.
TEXT_SINKS: Tuple[Tuple[str, str], ...] = (
    ("PreviewAny", "source"),
    ("PreviewString", "value"),
    ("ShowText|pysssss", "text"),
    ("ShowText", "text"),
    ("SaveText", "text"),
)

LANG_NAMES = {
    "cs": "Czech", "en": "English", "sk": "Slovak", "de": "German",
    "pl": "Polish", "fr": "French", "es": "Spanish", "it": "Italian",
}

# Gemma 3 čeká rozhovor v tomhle formátu. Kdyby si ho node přidával sám,
# jde šablona přepsat v config.json (translate_comfy_template).
DEFAULT_TEMPLATE = (
    "<start_of_turn>user\n"
    "Translate the following text from {source_name} to {target_name}.\n"
    "Rules: output ONLY the translation, no explanation, no quotes, no notes. "
    "Keep the meaning, tone and any technical or cinematographic terms. "
    "Do not add or remove content. Keep it on a single line.\n\n"
    "Text:\n{text}<end_of_turn>\n"
    "<start_of_turn>model\n"
)


def lang_name(code: str) -> str:
    code = str(code or "").strip().lower()[:5]
    return LANG_NAMES.get(code.split("-")[0], code.upper() or "English")


def _sink_for(client: ComfyClient) -> Optional[Tuple[str, str]]:
    """První node, kterým se dá text dostat do historie, podle object_info."""
    info = client.object_info() or {}
    for class_type, input_name in TEXT_SINKS:
        if class_type in info:
            return class_type, input_name
    return None


def availability(client: ComfyClient) -> Dict[str, Any]:
    """Co pro překlad ve ComfyUI chybí. Používá to Diagnostika i Setup."""
    info = client.object_info() or {}
    if not info:
        return {"ok": False, "reason": "ComfyUI nevrátil object_info, takže nevím, co umí."}
    missing: List[str] = [n for n in (TEXT_ENCODER_LOADER, TEXT_GENERATOR) if n not in info]
    sink = _sink_for(client)
    if not sink:
        missing.append(" nebo ".join(s[0] for s in TEXT_SINKS[:2]))
    if missing:
        return {"ok": False, "reason": "ComfyUI nemá node: " + ", ".join(missing),
                "missing": missing}
    encoders = client.combo_options(TEXT_ENCODER_LOADER, "text_encoder")
    if not encoders:
        return {"ok": False, "reason": f"{TEXT_ENCODER_LOADER} nenabízí žádný text encoder — "
                                       f"chybí model Gemmy."}
    return {"ok": True, "sink": sink[0], "encoder": _pick_encoder(client, encoders),
            "encoders": encoders}


def _pick_encoder(client: ComfyClient, encoders: List[str]) -> str:
    """Vybere jazykový model. Gemma umí česky, takže má přednost."""
    wanted = str(CONFIG.get("translate_comfy_encoder") or "").strip()
    if wanted:
        for e in encoders:
            if e.lower() == wanted.lower():
                return e
        log.warning("translate_comfy_encoder=%r ComfyUI nenabízí, vybírám sám.", wanted)
    for needle in ("gemma", "t5", "llama", "qwen"):
        for e in encoders:
            if needle in e.lower():
                return e
    return encoders[0]


def _pick_checkpoint(client: ComfyClient) -> Optional[str]:
    """Checkpoint, který loader potřebuje ke konfiguraci text encoderu."""
    wanted = str(CONFIG.get("translate_comfy_checkpoint") or "").strip()
    options = client.combo_options(TEXT_ENCODER_LOADER, "ckpt_name")
    if not options:
        return wanted or None
    if wanted:
        for o in options:
            if o.lower() == wanted.lower():
                return o
    for needle in ("ltx-2.3", "ltx2.3", "ltx"):
        for o in options:
            if needle in o.lower():
                return o
    return options[0]


def build_translate_workflow(client: ComfyClient, text: str, source: str, target: str) -> dict:
    """Textové workflow: naloží Gemmu, vygeneruje překlad, vrátí ho jako text."""
    avail = availability(client)
    if not avail.get("ok"):
        raise ComfyError(str(avail.get("reason") or "ComfyUI neumí překládat."))

    sink_class, sink_input = _sink_for(client)  # type: ignore[misc]
    template = str(CONFIG.get("translate_comfy_template") or "") or DEFAULT_TEMPLATE
    instruction = template.format(text=text, source=source, target=target,
                                  source_name=lang_name(source), target_name=lang_name(target))

    loader_inputs: Dict[str, Any] = {"text_encoder": avail["encoder"]}
    checkpoint = _pick_checkpoint(client)
    if checkpoint:
        loader_inputs["ckpt_name"] = checkpoint
    if client.combo_options(TEXT_ENCODER_LOADER, "device"):
        loader_inputs["device"] = "default"

    gen_inputs: Dict[str, Any] = {
        "prompt": instruction,
        "clip": ["1", 0],
        # Překlad nemá být kreativní — co nabídne object_info, to nastavíme.
        "max_length": max(64, min(4096, int(CONFIG.get("translate_comfy_max_length") or 512))),
        "use_default_template": False,
        "thinking": False,
    }
    sampling = client.combo_options(TEXT_GENERATOR, "sampling_mode")
    if sampling:
        off = next((s for s in sampling if str(s).lower() in ("off", "false", "disable", "none")), None)
        gen_inputs["sampling_mode"] = off or sampling[0]
        if not off:
            # Sampling vypnout nejde, tak ho aspoň zkrotíme.
            gen_inputs["sampling_mode.temperature"] = 0.1
            gen_inputs["sampling_mode.top_p"] = 0.9
            gen_inputs["sampling_mode.seed"] = 1
    return {
        "1": {"class_type": TEXT_ENCODER_LOADER, "inputs": loader_inputs,
              "_meta": {"title": "Jazykový model"}},
        "2": {"class_type": TEXT_GENERATOR, "inputs": gen_inputs,
              "_meta": {"title": "Překlad"}},
        "3": {"class_type": sink_class, "inputs": {sink_input: ["2", 0]},
              "_meta": {"title": "Výsledek jako text"}},
    }


def _strings_from_history(history: dict) -> List[str]:
    """Vytáhne text z výstupů historie. Každý sink node to hlásí trochu jinak."""
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                found.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in ("text", "string", "value", "source", "result"):
                    walk(item)

    for out in (history.get("outputs") or {}).values():
        if isinstance(out, dict):
            walk(out)
    return found


PREAMBLE = re.compile(
    r"^\s*(?:here(?:'s| is)[^:]{0,40}:|translation\s*:|english\s*:|překlad\s*:|"
    r"sure[,!.]?|output\s*:)\s*", re.IGNORECASE)


def clean_translation(raw: str, original: str) -> str:
    """Z odpovědi modelu udělá čistý překlad.

    Model občas přidá „Here is the translation:", obalí to do uvozovek nebo
    zopakuje zadání. Tohle to osekne, ať se do promptu nedostane balast.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # Kdyby node vrátil i šablonu rozhovoru, vezmeme jen odpověď modelu.
    if "<start_of_turn>model" in text:
        text = text.split("<start_of_turn>model", 1)[1]
    for marker in ("<end_of_turn>", "<eos>", "<start_of_turn>"):
        text = text.split(marker, 1)[0]
    text = text.strip()
    # Když model zopakoval zadání, zahodíme tu část.
    original_line = str(original or "").strip()
    if original_line and text.startswith(original_line) and len(text) > len(original_line):
        text = text[len(original_line):].strip(" \n:-–—")
    text = PREAMBLE.sub("", text).strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'„“":
        text = text[1:-1].strip()
    # Vícekrát opakovaný překlad (model se občas zacyklí) — bereme první odstavec.
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if parts:
        text = parts[0]
    return " ".join(text.split())


def translate_via_comfy(text: str, source: str = "cs", target: str = "en",
                        client: Optional[ComfyClient] = None) -> Dict[str, Any]:
    """Přeloží text jazykovým modelem ve ComfyUI. Nikam mimo lokální síť nejde."""
    text = str(text or "").strip()
    if not text:
        return {"success": False, "error": "Prázdný text.", "provider": "comfy"}
    if str(source).lower()[:2] == str(target).lower()[:2]:
        return {"success": True, "translated": text, "provider": "comfy",
                "note": "Zdroj i cíl je stejný jazyk, nepřekládalo se."}

    client = client or ComfyClient()
    timeout = float(CONFIG.get("translate_comfy_timeout") or 180)
    workflow = build_translate_workflow(client, text, source, target)
    client_id = f"comfylocal-translate-{uuid.uuid4().hex[:8]}"
    prompt_id = client.submit(workflow, client_id)

    deadline = time.time() + timeout
    history: Optional[dict] = None
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            history = client.history(prompt_id, allow_empty=True)
        except Exception as e:
            log.debug("history při překladu: %s", e)
            history = None
        if history:
            break
    if not history:
        raise ComfyError(f"Překlad se nevrátil do {int(timeout)} s. "
                         f"Model se možná teprve načítá do paměti — zkus to znovu.")

    from .comfy_client import extract_history_error
    err = extract_history_error(history)
    if err:
        raise ComfyError("Překlad v ComfyUI spadl: " + err)

    candidates = _strings_from_history(history)
    if not candidates:
        raise ComfyError("ComfyUI překlad dokončil, ale nevrátil žádný text. "
                         "Zkontroluj, že má node PreviewAny.")
    best = ""
    for candidate in candidates:
        cleaned = clean_translation(candidate, text)
        # Odpověď, která je jen zopakované zadání, nám nepomůže.
        if cleaned and cleaned.lower() != text.lower() and len(cleaned) > len(best):
            best = cleaned
    if not best:
        raise ComfyError("Model vrátil jen původní text — překlad se nepovedl.")
    return {"success": True, "translated": best, "provider": "comfy",
            "model": workflow["1"]["inputs"]["text_encoder"]}


__all__ = ["translate_via_comfy", "availability", "build_translate_workflow",
           "clean_translation", "lang_name"]
