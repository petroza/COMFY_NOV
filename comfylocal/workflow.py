# -*- coding: utf-8 -*-
"""Sestavení a patchování ComfyUI API workflow.

Režimy, které appka umí:

- **LTX 2.3 / 1 PICT** (`ltx23_i2v_template.json`) — video z jedné fotky,
- **LTX 2.3 / 2 PICT** (`ltx23_flf2v_template.json`) — první a poslední frejm,
- **photo edit** (`flux2_edit_template.json`, `firered_edit_template.json`) —
  na vstupu fotka, na výstupu upravená fotka.

Obě LTX šablony jsou export (formát API) projektů z ComfyUI, které jsou uložené
v `docs/comfyui_projects/`. U LTX se proto nesahá na `cfg`, `sampler_name`, sigmy
ani na strength vodicích obrázků — šablona má `cfg = 1` a pevný rozpis sigem
(`ManualSigmas`) a přepis hodnotou z formuláře z videa dělal šum. U photo edit
naopak kroky výpočtu i cfg smysl dávají, takže se tam patchují dál.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .comfy_client import ComfyClient, ComfyError
from .config import CONFIG
from .presets import camera_preset_text

log = logging.getLogger("comfylocal.workflow")

TECH_QUALITY = ("smooth motion, stable footage, sharp details, high quality, "
                "natural motion blur, 180-degree shutter")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

# LTX 2.3 VAE komprimuje obraz 32×. Šablona i2v navíc počítá první průchod
# v polovičním rozlišení (`a/2`) a pak ho zvedne 2× spatial upscalerem, takže
# musí platit (W//2)//32 * 2 == W//32 — jinak sampler ve druhém průchodu
# dostane latent a vodicí obrázek v jiné velikosti a render spadne na
# „The size of tensor a (…) must match the size of tensor b (…)“.
LTX_LATENT_BLOCK = 32

# Horní mez „délky vylepšeného promptu" (TextGenerateLTX2Prompt.max_length).
# Stejná hodnota jako v projektu z ComfyUI.
ENHANCE_TOKENS_MAX = 2048


# ── načtení šablon ──────────────────────────────────────────
def list_workflows() -> List[dict]:
    out: List[dict] = []
    for p in sorted(CONFIG.workflows_dir.glob("*.json")):
        info = {"name": p.name, "label": p.stem, "kind": "unknown"}
        try:
            wf = sanitize_workflow(json.loads(p.read_text(encoding="utf-8")), p.name)
            info["kind"] = "photo_edit" if workflow_is_photo_edit(wf) else (
                "flf2v" if workflow_is_flf2v(wf) else "i2v")
            info["nodes"] = len(wf)
        except Exception as e:
            info["error"] = str(e)
        out.append(info)
    return out


def sanitize_workflow(wf: Any, source: str) -> dict:
    if isinstance(wf, dict) and wf.get("_template_marker") == "REPLACE_WITH_EXPORTED_COMFYUI_API_WORKFLOW":
        raise ComfyError(
            f"Workflow je jen instalační šablona: {source}. Do složky workflows/ dej "
            "reálný export z ComfyUI ve formátu API (Workflow → Export (API))."
        )
    # Projekt uložený z ComfyUI (Workflow → Export) vypadá úplně jinak než export
    # pro API a ComfyUI ho přes /prompt nepřijme. Bez téhle hlášky by job spadl
    # až na serveru s nesrozumitelnou chybou.
    if isinstance(wf, dict) and isinstance(wf.get("nodes"), list):
        raise ComfyError(
            f"{source} je projekt z ComfyUI (formát UI), ne API workflow. V ComfyUI otevři "
            "projekt a ulož ho přes Workflow → Export (API); teprve ten JSON patří do workflows/."
        )
    if isinstance(wf, dict):
        wf = {k: v for k, v in wf.items() if not str(k).startswith("_")}
    if not isinstance(wf, dict) or not wf:
        raise ComfyError(f"Workflow JSON je prázdný nebo neplatný: {source}")
    return wf


def ltx_safe_size(value: Any, default: int = 1280) -> int:
    """Nejbližší rozměr, který LTX 2.3 šablona spočítá bez nesouhlasu tenzorů.

    Šablona počítá první průchod v polovičním rozlišení a pak ho zdvojnásobí
    upscalerem, takže reálný výstup je `2 * ((rozměr / 2) // 32) * 32`.
    Aby se výstup rovnal zadání, musí být rozměr **násobek 64** — násobek 32
    nestačí.

    Dřív se kontrolovalo jen `(rozměr // 32) % 2 == 0`, což 720 pustilo dál,
    ale ComfyUI z něj stejně udělal 704 (720/2 = 360, 360 // 32 = 11 → 352,
    ×2 = 704). Appka tedy hlásila 720 a doručila 704.
    """
    try:
        size = int(round(float(value)))
    except (TypeError, ValueError):
        size = int(default)
    size = max(256, min(4096, size))
    block = LTX_LATENT_BLOCK * 2  # 64 = 32 (latent) × 2 (upscaler mezi průchody)
    snapped = int(round(size / block)) * block
    return max(256, min(4096, snapped))


def ltx_delivered_size(size: Any) -> int:
    """Kolik pixelů z daného rozměru reálně vyleze ze šablony (pro diagnostiku)."""
    try:
        value = int(round(float(size)))
    except (TypeError, ValueError):
        return 0
    return 2 * ((value // 2) // LTX_LATENT_BLOCK) * LTX_LATENT_BLOCK


def ltx_geometry_note(width: int, height: int, fps: int, duration: float) -> str:
    """Předpočítané velikosti tenzorů, aby se chyba z ComfyUI dala rozklíčovat.

    Když render spadne na „size of tensor a (X) must match tensor b (Y)",
    dá se z tohohle zápisu poznat, jestli jde o geometrii (pak X/Y odpovídají
    spočítaným číslům) nebo o chybu ComfyUI (pak neodpovídají) — bez toho se
    to hádá z ničeho.

    Vzorce plynou z LTX 2.3: video latent je [B,128,T,H/32,W/32], první průchod
    jede v polovičním rozlišení, audio latent má 128 hodnot na jeden krok
    a 25 kroků na sekundu.
    """
    frames = max(1, int(round(float(fps) * float(duration))) + 1)
    t = (frames - 1) // 8 + 1
    h_lat = (int(height) // 2) // LTX_LATENT_BLOCK
    w_lat = (int(width) // 2) // LTX_LATENT_BLOCK
    tokens = t * h_lat * w_lat
    audio_latents = int(round(frames * 25 / max(1, int(fps))))
    video_frames_out = (t - 1) * 8 + 1
    return (f"{width}×{height} (1. průchod latent {w_lat}×{h_lat}×{t}) · frames={frames} "
            f"→ video {video_frames_out}, audio {audio_latents} · "
            f"noise tokens={tokens}, AV pack={128 * (tokens + audio_latents)}")


def load_workflow(name: Optional[str] = None) -> dict:
    fname = (name or CONFIG.get("default_workflow") or "").strip()
    if not fname:
        raise ComfyError("Není vybrané workflow.")
    if Path(fname).name != fname:
        raise ComfyError(f"Neplatný název workflow: {fname}")
    path = CONFIG.workflows_dir / fname
    if not path.exists():
        raise ComfyError(f"Workflow {fname} není ve složce {CONFIG.workflows_dir}.")
    wf = json.loads(path.read_text(encoding="utf-8"))
    return sanitize_workflow(wf, str(path))


def workflow_is_photo_edit(wf: dict) -> bool:
    """Photo-edit = ukládá obrázek (SaveImage) a nemá žádné video nody
    (Flux.2 edit, FireRed/Qwen edit a podobné)."""
    classes = {str(n.get("class_type") or "") for n in wf.values() if isinstance(n, dict)}
    has_image_out = "SaveImage" in classes
    has_video = any(("Video" in c) or c.startswith("LTXV") or c.startswith("LTXA") for c in classes)
    return has_image_out and not has_video


def workflow_is_flf2v(wf: dict) -> bool:
    """2 PICT = šablona má dva LoadImage nody (první a poslední frejm)."""
    loaders = [nid for nid, n in wf.items()
               if isinstance(n, dict) and str(n.get("class_type") or "").lower() == "loadimage"]
    return len(loaders) >= 2 and not workflow_is_photo_edit(wf)


# ── obecné pomůcky nad API workflow ─────────────────────────
def _get_node(wf: dict, node_id: Any) -> Optional[dict]:
    if isinstance(node_id, (list, tuple)) and node_id:
        node_id = node_id[0]
    return wf.get(str(node_id)) or wf.get(node_id)


def set_node_input(wf: dict, node_id: str, input_name: str, value: Any) -> bool:
    node = _get_node(wf, node_id)
    if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
        node["inputs"][input_name] = value
        return True
    return False


def get_node_input(wf: dict, node_id: str, input_name: str) -> Any:
    node = _get_node(wf, node_id)
    if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
        return node["inputs"].get(input_name)
    return None


def _node_title(node: dict) -> str:
    meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    return str(meta.get("title") or node.get("title") or "")


def deep_replace(obj: Any, repl: Dict[str, Any]) -> Any:
    if isinstance(obj, dict):
        return {k: deep_replace(v, repl) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_replace(v, repl) for v in obj]
    if isinstance(obj, str):
        if obj in repl:
            return repl[obj]
        s = obj
        for key, val in repl.items():
            if isinstance(val, (str, int, float)):
                s = s.replace(key, str(val))
        return s
    return obj


def workflow_contains_value(obj: Any, needle: str) -> bool:
    if isinstance(obj, dict):
        return any(workflow_contains_value(v, needle) for v in obj.values())
    if isinstance(obj, list):
        return any(workflow_contains_value(v, needle) for v in obj)
    if isinstance(obj, str):
        return obj == needle or needle in obj
    return False


def _set_linked_numeric(wf: dict, link_value: Any, value: Any, label: str,
                        patched: List[str], kind: str) -> bool:
    """Když je input napojený přes Primitive node, přepiš zdrojový node.
    U LTX je délka videa často linkovaná: EmptyLTXVLatentVideo.length -> PrimitiveInt."""
    if not isinstance(link_value, (list, tuple)) or not link_value:
        return False
    src = _get_node(wf, link_value[0])
    if not isinstance(src, dict):
        return False
    src_inputs = src.get("inputs")
    if not isinstance(src_inputs, dict):
        return False
    preferred = ("value", "int", "integer", "number", "float",
                 "frame_count", "frames_number", "num_frames", "length", "video_length",
                 "fps", "frame_rate", "duration", "seconds")
    numeric_keys = [k for k in preferred if k in src_inputs and isinstance(src_inputs.get(k), (int, float))]
    if not numeric_keys:
        numeric_keys = [k for k, v in src_inputs.items() if isinstance(v, (int, float))]
    if not numeric_keys:
        return False
    key = numeric_keys[0]
    old = src_inputs[key]
    src_inputs[key] = int(value) if kind in ("frames", "fps", "width", "height", "seed", "steps") else value
    patched.append(f"{kind} linked {label} -> {link_value[0]}:{src.get('class_type','')}.{key}: {old} -> {src_inputs[key]}")
    return True


# ── autopatch vstupů ────────────────────────────────────────
def auto_patch_workflow_nodes(wf: dict, values: Dict[str, Any],
                              allow_sampling_params: bool = False) -> List[str]:
    """Automaticky přepíše ty vstupy API workflow, které se dají měnit z UI.

    `allow_sampling_params` řídí, jestli se smí sáhnout na `cfg` a `steps`.
    U photo edit (Flux.2, FireRed) je to správně — posuvníky v UI ty hodnoty
    opravdu řídí. U LTX 2.3 **ne**: šablona jede na `cfg = 1` s ručním rozpisem
    sigem (`ManualSigmas`), počet kroků je dán délkou toho seznamu, a přepis
    `cfg` hodnotou z posuvníku dělal z videa šum. Na `sampler_name`, sigmy ani
    na `strength` vodicích obrázků se nesahá nikdy.

    Ostatní LTX specifika:
    - délka videa bývá jako linkovaný PrimitiveInt, ne přímá hodnota v node,
    - negative prompt se nepřepisuje, když je v UI prázdný (nechá se default šablony).
    """
    patched: List[str] = []
    new_image = str(values.get("image") or "")
    prompt = str(values.get("positive_prompt") or "")
    negative = str(values.get("negative_prompt") or "").strip()
    width = int(values.get("width") or 0)
    height = int(values.get("height") or 0)
    seed = int(values.get("seed") or 0)
    fps = int(values.get("fps") or 0)
    duration = int(values.get("duration") or 0)
    frame_count = int(values.get("frame_count") or 0)
    steps = int(values.get("steps") or 0) if allow_sampling_params else 0
    cfg = float(values.get("cfg") or 0) if allow_sampling_params else 0.0

    text_candidates: List[tuple] = []
    positive_patched = False
    negative_patched = False

    for node_id, node in list(wf.items()):
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        class_type = str(node.get("class_type") or "")
        cls = class_type.lower()
        title = _node_title(node).lower()
        label = f"{node_id}:{class_type}"

        # 1) VSTUPNÍ OBRÁZEK
        is_image_loader = (
            ("load" in cls and "image" in cls) or
            ("image" in cls and any(x in cls for x in ("input", "file", "path"))) or
            "load image" in title or "input image" in title or "image input" in title or
            title.strip() in ("image", "input", "start image", "source image")
        )
        if new_image:
            for key in ("image", "image_path", "filename", "file", "path"):
                if key in inputs and isinstance(inputs.get(key), str):
                    old = inputs.get(key)
                    if is_image_loader or str(old).lower().endswith(IMAGE_EXTS):
                        if old != new_image:
                            inputs[key] = new_image
                            patched.append(f"image {label}.{key}: {old} -> {new_image}")
            # Pojistka: jakýkoliv image soubor v inputech se přepíše na aktuální upload.
            for key, val in list(inputs.items()):
                if isinstance(val, str) and val.lower().endswith(IMAGE_EXTS) and val != new_image:
                    inputs[key] = new_image
                    patched.append(f"image global {label}.{key}: {val} -> {new_image}")

        # 2) PROMPT / NEGATIVE PROMPT
        # U LTX 2.3 exportů bývá hlavní prompt jako PrimitiveStringMultiline.inputs.value
        # s titulkem "Prompt", ne jako CLIPTextEncode.inputs.text.
        text_keys = [k for k in ("text", "prompt", "caption", "positive", "negative")
                     if k in inputs and isinstance(inputs.get(k), str)]
        if "value" in inputs and isinstance(inputs.get("value"), str):
            value_is_prompt_text = (
                ("primitive" in cls and "string" in cls) or "string" in cls or
                any(x in title for x in ("prompt", "positive", "negative", "caption", "text"))
            )
            if value_is_prompt_text and "value" not in text_keys:
                if any(x in title for x in ("prompt", "caption", "positive", "negative")):
                    text_keys.insert(0, "value")
                else:
                    text_keys.append("value")
        if text_keys:
            key = text_keys[0]
            current_text = str(inputs.get(key) or "")
            is_text_node = (
                any(x in cls for x in ("text", "prompt", "encode", "gemma", "clip", "string")) or
                any(x in title for x in ("prompt", "text", "caption", "positive", "negative"))
            )
            if is_text_node:
                negative_hint = (
                    "negative" in title or "negative" in cls or key == "negative" or
                    any(x in current_text.lower() for x in
                        ("low quality", "ugly", "deformed", "blur", "flicker", "watermark", "cartoon"))
                )
                positive_hint = (not negative_hint) and (
                    "positive" in title or key == "positive" or "prompt" in title or "caption" in title or
                    ("primitive" in cls and "string" in cls and key == "value")
                )
                text_candidates.append((node_id, node, key, label, negative_hint, positive_hint))
                if negative_hint:
                    if negative:
                        old = str(inputs[key])
                        inputs[key] = negative
                        patched.append(f"negative {label}.{key}: {old[:40]!r} -> custom")
                        negative_patched = True
                elif positive_hint and prompt:
                    old = str(inputs[key])
                    inputs[key] = prompt
                    patched.append(f"positive {label}.{key}: {old[:40]!r} -> UI prompt")
                    positive_patched = True

        # 3) ROZMĚRY / SEED / STEPS / CFG / FPS / DÉLKA
        def set_num(keys, value, kind, cast_int=False):
            if not value:
                return False
            for k in keys:
                if k in inputs:
                    v = inputs.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        old = v
                        inputs[k] = int(value) if cast_int else value
                        patched.append(f"{kind} {label}.{k}: {old} -> {inputs[k]}")
                        return True
                    if _set_linked_numeric(wf, v, int(value) if cast_int else value, f"{label}.{k}", patched, kind):
                        return True
            return False

        set_num(("width", "W", "w"), width, "width", True)
        set_num(("height", "H", "h"), height, "height", True)
        set_num(("seed", "noise_seed", "random_seed"), seed, "seed", True)
        if seed and "sampling_mode.seed" in inputs and isinstance(inputs.get("sampling_mode.seed"), (int, float)):
            old_seed = inputs.get("sampling_mode.seed")
            inputs["sampling_mode.seed"] = int(seed)
            patched.append(f"seed {label}.sampling_mode.seed: {old_seed} -> {seed}")
        set_num(("steps",), steps, "steps", True)
        set_num(("cfg", "guidance", "guidance_scale"), cfg, "cfg", False)
        set_num(("fps", "frame_rate"), fps, "fps", True)
        set_num(("duration", "seconds", "sec", "length_seconds", "video_duration"), duration, "duration", True)
        set_num(("frame_count", "frames", "frames_number", "num_frames", "length", "video_length"),
                frame_count, "frames", True)

        # LTX používá PrimitiveInt s titulkem "Duration", "Frame Rate", "Width", "Height" —
        # ty nemají key duration/fps, jen inputs.value, takže patch podle titulku.
        if "value" in inputs and isinstance(inputs.get("value"), (int, float)) and not isinstance(inputs.get("value"), bool):
            primitive_value = inputs.get("value")
            if width and (title in ("width", "w") or ("width" in title and "height" not in title)):
                inputs["value"] = int(width)
                patched.append(f"width primitive {label}.value: {primitive_value} -> {inputs['value']}")
            elif height and (title in ("height", "h") or ("height" in title and "width" not in title)):
                inputs["value"] = int(height)
                patched.append(f"height primitive {label}.value: {primitive_value} -> {inputs['value']}")
            elif fps and any(x in title for x in ("frame rate", "framerate", "fps")):
                inputs["value"] = int(fps)
                patched.append(f"fps primitive {label}.value: {primitive_value} -> {inputs['value']}")
            elif duration and "duration" in title:
                # Node je PrimitiveInt — sekundy sem musí jít jako celé číslo.
                inputs["value"] = int(duration)
                patched.append(f"duration primitive {label}.value: {primitive_value} -> {inputs['value']}")
            elif frame_count and any(x in title for x in ("frame count", "frames", "num frames", "length")) and "rate" not in title:
                inputs["value"] = int(frame_count)
                patched.append(f"frames primitive {label}.value: {primitive_value} -> {inputs['value']}")

    # 4) Fallback pro pozitivní prompt, když export nemá _meta/title.
    if prompt and not positive_patched and text_candidates:
        candidates = [c for c in text_candidates if not c[4]] or text_candidates
        _nid, node, key, label, _neg, _pos = candidates[0]
        old = node["inputs"][key]
        node["inputs"][key] = prompt
        patched.append(f"positive fallback {label}.{key}: {str(old)[:40]!r} -> UI prompt")
        positive_patched = True

    # 5) Fallback pro custom negative, jen když ho uživatel vyplnil.
    if negative and not negative_patched:
        candidates = [c for c in text_candidates if c[4]]
        if candidates:
            _nid, node, key, label, _neg, _pos = candidates[0]
            old = node["inputs"][key]
            node["inputs"][key] = negative
            patched.append(f"negative fallback {label}.{key}: {str(old)[:40]!r} -> custom")
            negative_patched = True

    # 6) Diagnostika: zbylé cizí image soubory ve workflow.
    if new_image:
        leftovers = []
        for node_id, node in wf.items():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                continue
            for key, val in node["inputs"].items():
                if isinstance(val, str) and val.lower().endswith(IMAGE_EXTS) and val != new_image:
                    leftovers.append(f"{node_id}:{node.get('class_type','')}.{key}={val}")
        if leftovers:
            log.warning("Ve workflow zůstaly jiné image soubory: %s%s",
                        "; ".join(leftovers[:8]), " …" if len(leftovers) > 8 else "")

    if not positive_patched:
        log.warning("Nepodařilo se najít positive prompt node; zkontroluj API workflow "
                    "nebo přidej placeholder __POSITIVE_PROMPT__.")
    if negative and not negative_patched:
        log.warning("Custom negative prompt byl zadán, ale negative node se nenašel; "
                    "přidej placeholder __NEGATIVE_PROMPT__ nebo node s titulkem 'Negative'.")
    return patched


# ── LTX ochrany a autofixy ──────────────────────────────────
def assert_input_image_present(wf: dict, *images: str) -> None:
    """Pojistka, že se nahraná fotka opravdu dostala do LoadImage nodu.

    Dřív se tady navíc hlídalo, že `LTXVImgToVideoInplace` má přesně
    strength 1.0 / 0.85. To byla hodnota z jiné, starší verze šablony —
    proti projektu, který appka posílá teď (0.7 v prvním průchodu), to
    padalo na „LTX ochrana: image-hold strength byl přepsán". Hodnoty ze
    šablony se proto nechávají tak, jak je uživatel uložil v ComfyUI.
    """
    for image in images:
        if image and not workflow_contains_value(wf, image):
            raise ComfyError(
                f"Vstupní obrázek se nedostal do workflow (ComfyUI image={image}). "
                "Zkontroluj, že šablona ve workflows/ je export ve formátu API a má LoadImage node."
            )


def repair_ltx_model_names(wf: dict, client: ComfyClient) -> None:
    """Autofix názvů modelů — jen když ComfyUI ten ze šablony vůbec nenabízí.

    Šablony jsou vyexportované z projektů, které na serveru běží, takže názvy
    v nich jsou správné: i2v jede na `…22b-dev-fp8` + distilled LoRA, FLF2V na
    `…22b-distilled-fp8`. Dřív tady byl pevný žebříček, který `distilled`
    checkpoint u FLF2V přepisoval na `dev` — tím se posílal jiný model, než na
    jaký je šablona (sigmy, cfg 1) stavěná. Teď se sahá jen na to, co na serveru
    reálně chybí, a hledá se nejpodobnější varianta ze stejné rodiny.
    """
    for node_id, node in list(wf.items()):
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        cls = str(node.get("class_type") or "")
        for key in ("ckpt_name", "text_encoder", "lora_name", "model_name"):
            cur = inputs.get(key)
            if not isinstance(cur, str) or not cur.strip():
                continue
            options = client.combo_options(cls, key)
            if not options or any(o.lower() == cur.lower() for o in options):
                continue  # ComfyUI název zná (nebo seznam nepřišel) — nesaháme na to
            new = _closest_model_name(cur, options)
            if new and new != cur:
                log.warning("Model autofix: node %s %s.%s — %s ComfyUI nenabízí, beru %s",
                            node_id, cls, key, cur, new)
                inputs[key] = new
            else:
                log.error("Model autofix: node %s %s.%s = %s ComfyUI nenabízí a náhrada se nenašla. "
                          "Dostupné: %s", node_id, cls, key, cur, ", ".join(options[:6]) or "(nic)")


def _closest_model_name(current: str, options: List[str]) -> Optional[str]:
    """Nejbližší dostupný název modelu ke jménu ze šablony.

    Rozhoduje se podle slov v názvu (`ltx`, `2.3`, `22b`, `distilled`/`dev`,
    `fp8`…), takže `ltx-2.3-22b-distilled-fp8` si vybere jinou distilled
    variantu dřív než dev, i když by dev byl v seznamu první.
    """
    def tokens(name: str) -> set:
        return set(re.split(r"[^a-z0-9.]+", name.lower())) - {""}

    wanted = tokens(current)
    scored = [(len(wanted & tokens(opt)), -len(tokens(opt) - wanted), opt) for opt in options]
    scored.sort(reverse=True)
    best = scored[0] if scored else None
    # Aspoň dvě společná slova, ať se z „ltx-2.3…" nestane náhodou úplně jiný model.
    return best[2] if best and best[0] >= 2 else None


def patch_ltx_prompt_enhance(wf: dict, enable: bool, tokens: int, seed: int = 0) -> List[str]:
    """Patchuje LTX Prompt Enhance bez pevného node ID. Kde node není, tiše přeskočí."""
    patched: List[str] = []
    # Projekt v ComfyUI má max_length 2048; strop 512 by vylepšený prompt uřízl.
    tokens = max(64, min(ENHANCE_TOKENS_MAX, int(tokens or 512)))
    enable = bool(enable)
    for node_id, node in wf.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        cls = str(node.get("class_type") or "")
        title = _node_title(node).lower()
        inputs = node["inputs"]
        label = f"{node_id}:{cls}"
        if cls == "TextGenerateLTX2Prompt":
            old = inputs.get("max_length")
            inputs["max_length"] = tokens
            patched.append(f"prompt tokens {label}.max_length: {old} -> {tokens}")
            if seed and isinstance(inputs.get("sampling_mode.seed"), (int, float)):
                old_seed = inputs.get("sampling_mode.seed")
                inputs["sampling_mode.seed"] = int(seed)
                patched.append(f"prompt enhance seed {label}: {old_seed} -> {seed}")
        if cls == "PrimitiveBoolean" and "value" in inputs:
            if ("prompt enhance" in title or "enable prompt enhance" in title
                    or ("enhance" in title and "prompt" in title)):
                old = inputs.get("value")
                inputs["value"] = enable
                patched.append(f"prompt enhance {label}.value: {old} -> {enable}")
    return patched


def patch_photo_edit(wf: dict, steps: int) -> List[str]:
    """PHOTO EDIT: kroky výpočtu + turbo/lightning LoRA přepínač.
    Turbo se zapne při steps <= 10 (turbo LoRA jsou dělané na 8 kroků).
    Funguje pro Flux.2 edit i FireRed. Mimo photo-edit workflow nic nedělá."""
    if not workflow_is_photo_edit(wf):
        return []
    patched: List[str] = []
    turbo = int(steps) <= 10
    for nid, node in wf.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        cls = str(node.get("class_type") or "")
        title = _node_title(node).lower()
        if cls == "PrimitiveBoolean" and any(x in title for x in ("lora", "lightning", "turbo")):
            old = node["inputs"].get("value")
            node["inputs"]["value"] = bool(turbo)
            patched.append(f"turbo {nid}: {old} -> {turbo}")
        if cls == "PrimitiveInt" and "steps" in title:
            old = node["inputs"].get("value")
            node["inputs"]["value"] = int(steps)
            patched.append(f"steps {nid}: {old} -> {steps}")
    return patched


def align_ltx_guide_resize(wf: dict, width: int, height: int) -> List[str]:
    """Sladí vodicí obrázek s cílovým rozlišením.

    LTX 2.3 šablona vede vstupní fotku přes `ResizeImagesByLongerEdge` s pevným
    limitem (1536). Latent se přitom staví z Width/Height, které nastavuje UI.
    Jakmile si uživatel vybere víc než ten limit (třeba FHD 1920×1080), vodicí
    obrázek se zmenší na 1536×864, kdežto latent zůstane 1920×1080 — a sampler
    pak spadne na „The size of tensor a (…) must match the size of tensor b (…)“.

    Limit proto zvedneme na delší stranu požadovaného rozlišení. Menší rozlišení
    se nechají být, tam se nic nezmenšuje a chování šablony zůstává původní.
    """
    patched: List[str] = []
    target = max(int(width or 0), int(height or 0))
    if target <= 0:
        return patched
    for node_id, node in wf.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        if str(node.get("class_type") or "") != "ResizeImagesByLongerEdge":
            continue
        current = node["inputs"].get("longer_edge")
        if isinstance(current, (int, float)) and int(current) < target:
            node["inputs"]["longer_edge"] = target
            patched.append(f"guide resize {node_id}.longer_edge: {int(current)} -> {target}")
    return patched


def template_native_resolution(workflow_name: Optional[str]) -> Optional[tuple]:
    """Rozlišení, se kterým je šablona vyexportovaná (nody Width / Height).

    Používá se jako záchranná brzda: když render v požadovaném rozlišení spadne
    na nesouhlasu tenzorů, zkusí se ještě jednou v tomhle, protože v něm šablona
    z ComfyUI prokazatelně prošla.
    """
    try:
        wf = load_workflow(workflow_name)
    except Exception:
        return None
    width = height = None
    for node in wf.values():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        if str(node.get("class_type") or "") != "PrimitiveInt":
            continue
        title = _node_title(node).strip().lower()
        value = node["inputs"].get("value")
        if not isinstance(value, (int, float)):
            continue
        if title in ("width", "w"):
            width = int(value)
        elif title in ("height", "h"):
            height = int(value)
    if width and height:
        return width, height
    return None


def is_tensor_size_mismatch(message: str) -> bool:
    low = str(message or "").lower()
    return ("must match the size of tensor" in low
            or "sizes of tensors must match" in low
            or "shape mismatch" in low)


def apply_lora_override(wf: dict, override: str) -> List[str]:
    """Přepíše nebo vypne LoRA ve workflow podle config.json.

    Když je LoRA ze šablony na serveru poškozená nebo chybí, jde ji vyměnit
    (`ltx_lora_override: "nazev.safetensors"`) nebo vypnout (`"off"`) bez
    editace JSONu.
    """
    override = str(override or "").strip()
    if not override:
        return []
    patched: List[str] = []
    disable = override.lower() in ("off", "none", "0", "false")
    for node_id, node in wf.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        if "lora" not in str(node.get("class_type") or "").lower():
            continue
        inputs = node["inputs"]
        if disable:
            for key in ("strength_model", "strength_clip"):
                if key in inputs:
                    old = inputs[key]
                    inputs[key] = 0.0
                    patched.append(f"lora off {node_id}.{key}: {old} -> 0.0")
        elif isinstance(inputs.get("lora_name"), str):
            old = inputs["lora_name"]
            inputs["lora_name"] = override
            patched.append(f"lora {node_id}.lora_name: {old} -> {override}")
    return patched


def set_flf2v_images(wf: dict, first_image: str, last_image: str) -> None:
    # LTX 2.3 first-last-frame šablona: 31 = první frejm, 39 = poslední frejm.
    if _get_node(wf, "31") and _get_node(wf, "39"):
        set_node_input(wf, "31", "image", first_image)
        set_node_input(wf, "39", "image", last_image)
        return
    load_nodes = [str(nid) for nid, node in wf.items()
                  if isinstance(node, dict) and str(node.get("class_type") or "").lower() == "loadimage"]
    if len(load_nodes) >= 2:
        set_node_input(wf, load_nodes[0], "image", first_image)
        set_node_input(wf, load_nodes[1], "image", last_image)
        return
    raise ComfyError("2 PICT workflow nemá dva LoadImage nody pro první a poslední frejm.")


# ── popisky fází pro UI ─────────────────────────────────────
def node_stage_label(wf: Optional[dict], node_id: str) -> str:
    node = _get_node(wf or {}, node_id) if wf else None
    cls = str((node or {}).get("class_type") or "")
    title = _node_title(node or {})
    if not node_id:
        return "Čekám na ComfyUI"
    if cls == "SaveVideo":
        return "Ukládám video"
    if cls == "CreateVideo":
        return "Skládám video"
    if cls == "SaveImage":
        return "Ukládám obrázek"
    rules = [
        ("VAEDecode|AudioVAEDecode|SeparateAVLatent|CropGuides|LatentUpsampler", "Dekóduji výstup"),
        ("SamplerCustomAdvanced|KSampler|SamplerEuler|ManualSigmas|RandomNoise|CFGGuider", "Generuji snímky"),
        ("ImgToVideo|AddGuide|EmptyLatent|EmptyLTXV|ConcatAVLatent|LTXVConditioning", "Připravuji latent"),
        ("CLIPTextEncode|TextGenerate|PrimitiveString|ComfySwitch", "Kóduji prompt"),
        ("Preprocess|Resize|GetImageSize|LoadImage", "Zpracovávám obrázek"),
        ("Checkpoint|TextEncoder|AudioVAELoader|LoraLoader|ModelLoader", "Načítám model"),
    ]
    for pattern, label in rules:
        if re.search(pattern, cls or ""):
            return label
    return title or cls or str(node_id)


# ── skládání promptu ────────────────────────────────────────
def _norm_prompt_part(text: str) -> str:
    return " ".join(str(text or "").lower().replace(";", ",").split())


def join_prompt_parts_once(*parts: str) -> str:
    out: List[str] = []
    for part in parts:
        p = str(part or "").strip().strip(",")
        if not p:
            continue
        if _norm_prompt_part(p) in _norm_prompt_part(", ".join(out)):
            continue
        out.append(p)
    return ", ".join(out)


def build_prompt(job: dict, is_photo_edit: bool = False) -> tuple:
    settings = job.get("settings") or {}
    user_prompt = str(job.get("prompt") or "").strip()
    style = str(settings.get("style") or "").strip()
    camera_motion = str(settings.get("camera_motion") or "").strip()
    if not camera_motion:
        camera_motion = camera_preset_text(job.get("preset") or "Statická kamera (stativ)")
    if is_photo_edit:
        # Kamerové ani video tech-texty by editační instrukci jen kazily.
        return join_prompt_parts_once(user_prompt, style), ""
    # LTX 2.3 dává vyšší váhu začátku promptu: děj -> kamera -> styl -> technika.
    return join_prompt_parts_once(user_prompt, camera_motion, style, TECH_QUALITY), camera_motion


# ── hlavní build ────────────────────────────────────────────
def build_workflow(job: dict, comfy_image_name: str, comfy_image_name_2: Optional[str],
                   client: ComfyClient, workflow_name: Optional[str] = None) -> dict:
    settings = dict(job.get("settings") or {})
    fps = int(settings.get("fps") or 25)
    duration = max(1, int(round(float(settings.get("duration") or 5))))
    frame_count = int(settings.get("frame_count") or (fps * duration))
    raw_width = int(settings.get("width") or 1280)
    raw_height = int(settings.get("height") or 720)
    seed = int(settings.get("seed") or 1)
    steps = int(settings.get("steps") or 30)
    cfg = float(settings.get("cfg") or 3.5)
    prompt_enhance = bool(settings.get("prompt_enhance"))
    enhance_tokens = max(64, min(ENHANCE_TOKENS_MAX, int(settings.get("enhance_tokens") or 512)))

    wf = load_workflow(workflow_name or job.get("workflow"))
    is_photo_edit = workflow_is_photo_edit(wf)
    prompt, camera_motion = build_prompt(job, is_photo_edit)
    negative = str(job.get("negative_prompt") or "").strip()

    # Mřížka rozlišení platí jen pro LTX — photo edit tu půlku latentu a 2×
    # upscaler nemá, takže by mu srovnávání jen zbytečně měnilo rozměry.
    if is_photo_edit:
        width, height = raw_width, raw_height
    else:
        width = ltx_safe_size(raw_width, 1280)
        height = ltx_safe_size(raw_height, 720)
        if (width, height) != (raw_width, raw_height):
            log.info("Job #%s rozlišení srovnáno na LTX mřížku: %s×%s -> %s×%s",
                     job.get("id"), raw_width, raw_height, width, height)
    log.info("Job #%s prompt: %s%s", job.get("id"), prompt[:200], " …" if len(prompt) > 200 else "")
    log.info("Job #%s timing: duration=%ss fps=%s frames=%s, negative=%s",
             job.get("id"), duration, fps, frame_count, "custom" if negative else "workflow-default")
    if not is_photo_edit:
        log.info("Job #%s LTX geometrie: %s", job.get("id"), ltx_geometry_note(width, height, fps, duration))

    values = {
        "positive_prompt": prompt,
        "negative_prompt": negative,
        "image": comfy_image_name,
        "image2": comfy_image_name_2 or "",
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
        "frame_count": frame_count,
        "seed": seed,
        "steps": steps,
        "cfg": cfg,
        "camera_motion": camera_motion,
        "output_prefix": f"comfylocal_job_{job.get('id')}",
    }
    repl = {
        "__POSITIVE_PROMPT__": prompt,
        "__IMAGE_FILENAME__": comfy_image_name,
        "__IMAGE2_FILENAME__": comfy_image_name_2 or "",
        "__WIDTH__": width,
        "__HEIGHT__": height,
        "__FPS__": fps,
        "__DURATION__": duration,
        "__FRAME_COUNT__": frame_count,
        "__SEED__": seed,
        "__STEPS__": steps,
        "__CFG__": cfg,
        "__GUIDANCE__": cfg,
        "__CAMERA_MOTION__": camera_motion,
        "__OUTPUT_PREFIX__": f"comfylocal_job_{job.get('id')}",
    }
    if negative:
        repl["__NEGATIVE_PROMPT__"] = negative
    wf = deep_replace(wf, repl)

    is_two_pict = bool(comfy_image_name_2) or str(settings.get("input_mode") or "").lower() in ("2pict", "flf2v")
    patch_values = dict(values)
    if is_two_pict:
        # U 2 PICT se nesmí globálně přepsat všechny LoadImage nody na první obrázek.
        patch_values["image"] = ""
    patched = auto_patch_workflow_nodes(wf, patch_values, allow_sampling_params=is_photo_edit)

    if is_two_pict:
        if not comfy_image_name_2:
            raise ComfyError("Režim 2 PICT potřebuje druhý obrázek / poslední frejm.")
        set_flf2v_images(wf, comfy_image_name, comfy_image_name_2)
        assert_input_image_present(wf, comfy_image_name, comfy_image_name_2)
    else:
        assert_input_image_present(wf, comfy_image_name)

    enhance_patched = patch_ltx_prompt_enhance(wf, prompt_enhance, enhance_tokens, seed)
    repair_ltx_model_names(wf, client)
    photo_patched = patch_photo_edit(wf, steps)

    guide_patched: List[str] = [] if is_photo_edit else align_ltx_guide_resize(wf, width, height)
    lora_patched = apply_lora_override(wf, CONFIG.get("ltx_lora_override"))

    if patched:
        log.info("Workflow auto-patch: %s%s", "; ".join(patched[:12]), " …" if len(patched) > 12 else "")
    if enhance_patched:
        log.info("Prompt Enhance patch: %s", "; ".join(enhance_patched[:8]))
    if photo_patched:
        log.info("PHOTO EDIT patch: %s", "; ".join(photo_patched[:8]))
    if guide_patched:
        log.info("Sladění vodicího obrázku s rozlišením: %s", "; ".join(guide_patched))
    if lora_patched:
        log.info("LoRA override z config.json: %s", "; ".join(lora_patched[:6]))
    return wf
