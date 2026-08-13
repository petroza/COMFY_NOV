# -*- coding: utf-8 -*-
"""Trvalé dávkové generování obrázků přes ComfyUI.

Dávku řídí serverové vlákno, takže pokračuje i po zavření prohlížeče. Stav každé
položky je v SQLite; po restartu aplikace se rozpracovaná položka bezpečně vrátí
do fronty. V jednom okamžiku se do ComfyUI posílá jen jeden obrázek.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import db
from .comfy_client import ComfyClient, ComfyError, find_output_files, raise_if_history_failed
from .config import CONFIG

log = logging.getLogger("comfylocal.image_batch")

STYLE_DEFAULT = (
    "Soft polished digital children's picture-book illustration, contemporary European "
    "animated storybook style, subtle gouache and colored-pencil texture, oversized rounded "
    "heads, large glossy dark eyes with bright catchlights, rosy pink cheeks, tiny rounded "
    "noses, warm friendly expressions, soft clean dark-brown outlines, gentle airbrushed "
    "shading, tactile fluffy fur or soft hair, cheerful but controlled palette of coral red, "
    "burnt orange, mustard yellow, teal, sage green and warm brown, warm creamy paper background "
    "on a warm light neutral background, a few tiny flowers or grass strokes near the feet, centered "
    "readable silhouette, simple uncluttered square composition, generous breathing space, "
    "professionally finished illustration for children age 4-8. No text, no letters, no words, "
    "no title, no caption, no logo, no watermark, no photorealism, no 3D render, no anime, "
    "no harsh shadows."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS comfy_image_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_name TEXT,
    mode TEXT NOT NULL DEFAULT 'hybrid',
    style_prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'paused',
    total INTEGER NOT NULL DEFAULT 0,
    done_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    ocr_retry_count INTEGER NOT NULL DEFAULT 0,
    current_index INTEGER,
    latest_file TEXT,
    output_dir TEXT NOT NULL,
    user_id INTEGER,
    user_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS comfy_image_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    source_index INTEGER NOT NULL,
    item_ref TEXT,
    image_ref TEXT,
    subject TEXT NOT NULL,
    source_prompt TEXT,
    model TEXT NOT NULL,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    output_file TEXT,
    prompt_id TEXT,
    error TEXT,
    seconds REAL,
    priority INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    ocr_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(batch_id, source_index)
);
CREATE INDEX IF NOT EXISTS idx_image_batches_status ON comfy_image_batches(status);
CREATE INDEX IF NOT EXISTS idx_image_batch_items_next
ON comfy_image_batch_items(batch_id, status, model, source_index);
"""

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def init_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with db._LOCK:
            conn = db.connect()
            conn.executescript(SCHEMA)
            item_columns = {r["name"] for r in conn.execute(
                "PRAGMA table_info(comfy_image_batch_items)").fetchall()}
            if "priority" not in item_columns:
                conn.execute("ALTER TABLE comfy_image_batch_items "
                             "ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "retry_count" not in item_columns:
                conn.execute("ALTER TABLE comfy_image_batch_items "
                             "ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
            if "ocr_text" not in item_columns:
                conn.execute("ALTER TABLE comfy_image_batch_items ADD COLUMN ocr_text TEXT")
            if "source_prompt" not in item_columns:
                conn.execute("ALTER TABLE comfy_image_batch_items ADD COLUMN source_prompt TEXT")
            batch_columns = {r["name"] for r in conn.execute(
                "PRAGMA table_info(comfy_image_batches)").fetchall()}
            if "ocr_retry_count" not in batch_columns:
                conn.execute("ALTER TABLE comfy_image_batches "
                             "ADD COLUMN ocr_retry_count INTEGER NOT NULL DEFAULT 0")
            # Jen jednou při startu procesu: pád nesmí nechat položku navždy
            # ve stavu processing. Běžné obnovování stránky už na stav nesahá.
            conn.execute("UPDATE comfy_image_batch_items SET status='pending', prompt_id=NULL "
                         "WHERE status='processing'")
            conn.commit()
        BATCH_ROOT.mkdir(parents=True, exist_ok=True)
        _SCHEMA_READY = True


BATCH_ROOT = CONFIG.data_dir / "image_batches"
_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()
_OCR_WARNED = False


def ocr_available() -> bool:
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        return True
    except Exception:
        return False


def _accepted_ocr_lines(result: Any, min_score: float = 0.72) -> List[str]:
    accepted: List[str] = []
    for one in result or []:
        try:
            text = " ".join(str(one[1] or "").split())
            score = float(one[2])
        except (IndexError, TypeError, ValueError):
            continue
        # Krátké náhodné tvary v očích, srsti a květinách OCR občas považuje
        # za písmeno. Za text bereme až čitelný úsek alespoň o čtyřech znacích.
        meaningful = "".join(ch for ch in text if ch.isalnum())
        if score >= min_score and len(meaningful) >= 4:
            accepted.append(text)
    return accepted


def detect_text(path: Path) -> str:
    """Vrátí čitelný text nalezený v obrázku, jinak prázdný řetězec."""
    global _OCR_ENGINE, _OCR_WARNED
    try:
        with _OCR_LOCK:
            if _OCR_ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR
                _OCR_ENGINE = RapidOCR()
            result, _elapsed = _OCR_ENGINE(str(path))
        return " | ".join(_accepted_ocr_lines(result))
    except Exception as exc:
        if not _OCR_WARNED:
            log.exception("OCR kontrola není dostupná: %s", exc)
            _OCR_WARNED = True
        return ""


def _row(row) -> Dict[str, Any]:
    return dict(row) if row else {}


def _safe(value: object, fallback: str = "") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-_")
    return cleaned[:80] or fallback


def extract_subject(prompt: str) -> str:
    text = " ".join(str(prompt or "").split())
    match = re.search(r"Close-up of (.*?), drawn large", text, re.IGNORECASE)
    if match:
        return match.group(1).strip().rstrip(".")
    text = re.sub(r"^Children's storybook illustration.*?Close-up of\s*", "", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\s*From the fairy tale.*$", "", text, flags=re.IGNORECASE)
    return text.strip().rstrip(".") or "a friendly fairy-tale character"


def choose_model(subject: str, mode: str) -> str:
    if mode in ("zimage", "qwen", "flux1", "flux2"):
        return mode
    low = subject.strip().lower()
    starters = ("he ", "she ", "they ", "it ", "we ", "you ", "his ", "her ",
                "the ", "never ", "because ", "so ", "to ", "when ", "where ",
                "why ", "how ", "cats ", "foxes ", "goddesses ")
    # Z-Image je velmi rychlý a výborný na konkrétní objekty ("a mouse", "red
    # apple"), ale větu nebo poučku občas vykreslí jako titulek plakátu. Proto je
    # hybrid záměrně konzervativní: delší/abstraktní fráze a slovesa jdou do
    # Qwenu, který je převede na čistě obrazovou scénu.
    abstract_words = {
        "always", "never", "should", "must", "could", "would", "because", "why",
        "reason", "excuse", "permission", "advice", "help", "truth", "lie", "fair",
        "argue", "argues", "find", "finds", "found", "seem", "seemed", "think",
        "thought", "took", "take", "gave", "give", "shared", "share", "wanted",
        "want", "decided", "learned", "lesson", "promise", "refused", "asked",
    }
    words = re.findall(r"[a-z']+", low)
    sentence_like = low.startswith(starters) or len(words) > 4 or any(w in abstract_words for w in words)
    return "qwen" if sentence_like else "zimage"


def story_seed(item_ref: str, source_index: int) -> int:
    """Stabilní seed sdílený všemi obrázky jedné pohádky.

    Pole ``item`` v importovaném JSONu označuje příběh. Stejný seed drží
    barevnost, kresbu a celkový rukopis blíž u sebe; chybějící item se
    bezpečně chová jako samostatný obrázek.
    """
    key = str(item_ref or f"image-{int(source_index)}").encode("utf-8")
    return 20_260_813_000_000 + int.from_bytes(hashlib.sha256(key).digest()[:6], "big")


def story_models(records: List[dict], mode: str) -> Dict[int, str]:
    """Vybere jediný model pro každý příběh, i v automatickém režimu."""
    result: Dict[int, str] = {}
    grouped: Dict[str, List[Tuple[int, str]]] = {}
    for record in records:
        index = int(record.get("source_index") or record.get("index") or 0)
        ref = str(record.get("item_ref") or record.get("item") or f"__image_{index}")
        grouped.setdefault(ref, []).append((index, str(record.get("subject") or "")))
    for members in grouped.values():
        if mode == "hybrid":
            selected = ("qwen" if any(choose_model(subject, mode) == "qwen"
                                      for _, subject in members) else "zimage")
        else:
            selected = choose_model(members[0][1], mode)
        for index, _ in members:
            result[index] = selected
    return result


def _workflow_zimage(prompt: str, seed: int) -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 800, "height": 800, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["6", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["7", 0], "seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "PreviewImage", "inputs": {"images": ["9", 0]}},
    }


def _workflow_qwen(prompt: str, seed: int) -> dict:
    negative = ("text, letters, words, sentence, typography, title, caption, speech bubble, "
                "watermark, logo, photorealism, 3D render, horror, low quality, distorted face")
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2512_fp8_e4m3fn.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors", "strength_model": 1.0}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "5": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["2", 0], "shift": 3.1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["3", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["3", 0]}},
        "8": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 800, "height": 800, "batch_size": 1}},
        "9": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["8", 0], "seed": seed, "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["4", 0]}},
        "11": {"class_type": "PreviewImage", "inputs": {"images": ["10", 0]}},
    }


def _workflow_flux1(prompt: str, seed: int) -> dict:
    """Oficiální single-file FLUX.1 Dev FP8 workflow pro ComfyUI."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": "flux1-dev-fp8.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["2", 0], "guidance": 3.5}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": 800, "height": 800, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["3", 0],
            "latent_image": ["5", 0], "seed": seed, "steps": 20, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "PreviewImage", "inputs": {"images": ["7", 0]}},
    }


def _workflow_flux2(prompt: str, seed: int) -> dict:
    """FLUX.2 Dev s nainstalovanou Turbo LoRA, text-to-image 800x800."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "flux2_dev_fp8mixed.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0], "lora_name": "Flux_2-Turbo-LoRA_comfyui.safetensors",
            "strength_model": 1.0}},
        "3": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "mistral_3_small_flux2_fp8.safetensors", "type": "flux2",
            "device": "default"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["3", 0]}},
        "5": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["4", 0], "guidance": 4.0}},
        "6": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["5", 0]}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "Flux2Scheduler", "inputs": {"steps": 8, "width": 800, "height": 800}},
        "10": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": 800, "height": 800, "batch_size": 1}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["7", 0], "guider": ["6", 0], "sampler": ["8", 0],
            "sigmas": ["9", 0], "latent_image": ["10", 0]}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["12", 0]}},
        "14": {"class_type": "PreviewImage", "inputs": {"images": ["13", 0]}},
    }


def create_batch(jobs: List[dict], source_name: str, mode: str, style: str,
                 limit: int = 0, user: Optional[dict] = None) -> Dict[str, Any]:
    init_schema()
    mode = mode if mode in ("hybrid", "zimage", "qwen", "flux1", "flux2") else "hybrid"
    selected = jobs[:max(0, limit)] if limit > 0 else jobs
    if not selected:
        raise ValueError("JSON neobsahuje žádné položky.")
    if len(selected) > 100000:
        raise ValueError("Jedna dávka může obsahovat nejvýše 100 000 položek.")
    style = str(style or STYLE_DEFAULT).strip()[:8000]
    user = user or {}
    with db._LOCK:
        conn = db.connect()
        cur = conn.execute(
            "INSERT INTO comfy_image_batches(name,source_name,mode,style_prompt,status,total,output_dir,user_id,user_name) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (Path(source_name).stem or "Dávka obrázků", source_name, mode, style, "paused",
             len(selected), "", user.get("id"), user.get("username")))
        batch_id = int(cur.lastrowid)
        output_dir = BATCH_ROOT / f"batch_{batch_id:06d}"
        conn.execute("UPDATE comfy_image_batches SET output_dir=? WHERE id=?",
                     (str(output_dir), batch_id))
        rows = []
        prepared = []
        for index, raw in enumerate(selected, 1):
            one = raw if isinstance(raw, dict) else {}
            subject = extract_subject(one.get("prompt", ""))
            item_ref = str(one.get("item") or "")
            prepared.append({"index": index, "item": item_ref, "subject": subject})
        models = story_models(prepared, mode)
        for index, raw in enumerate(selected, 1):
            one = raw if isinstance(raw, dict) else {}
            subject = prepared[index - 1]["subject"]
            item_ref = prepared[index - 1]["item"]
            rows.append((batch_id, index, item_ref,
                         str(one.get("image") or ""), subject, str(one.get("prompt") or ""),
                         models[index],
                         story_seed(item_ref, index)))
        conn.executemany(
            "INSERT INTO comfy_image_batch_items(batch_id,source_index,item_ref,image_ref,subject,"
            "source_prompt,model,seed) VALUES(?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    output_dir.mkdir(parents=True, exist_ok=True)
    return get_batch(batch_id)


def get_batch(batch_id: int) -> Dict[str, Any]:
    init_schema()
    with db._LOCK:
        conn = db.connect()
        batch = _row(conn.execute("SELECT * FROM comfy_image_batches WHERE id=?", (int(batch_id),)).fetchone())
        if not batch:
            return {}
        recent = [dict(r) for r in conn.execute(
            "SELECT source_index,image_ref,subject,model,status,output_file,error,seconds,"
            "retry_count,ocr_text "
            "FROM comfy_image_batch_items WHERE batch_id=? AND status IN ('done','error') "
            "ORDER BY updated_at DESC,id DESC LIMIT 12", (int(batch_id),)).fetchall()]
    batch["recent"] = recent
    batch["ocr_available"] = ocr_available()
    batch["progress"] = round(100 * (int(batch.get("done_count") or 0) + int(batch.get("error_count") or 0)) /
                              max(1, int(batch.get("total") or 0)), 1)
    return batch


def list_batches(user: Optional[dict] = None, limit: int = 30) -> List[Dict[str, Any]]:
    init_schema()
    user = user or {}
    sql = "SELECT * FROM comfy_image_batches"
    params: List[Any] = []
    if user.get("id") and user.get("role") != "admin":
        sql += " WHERE user_id=?"
        params.append(int(user["id"]))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(200, int(limit))))
    with db._LOCK:
        rows = [dict(r) for r in db.connect().execute(sql, params).fetchall()]
    for row in rows:
        row["progress"] = round(100 * (int(row.get("done_count") or 0) + int(row.get("error_count") or 0)) /
                                max(1, int(row.get("total") or 0)), 1)
    return rows


def control_batch(batch_id: int, action: str) -> Dict[str, Any]:
    action = str(action or "").lower()
    if action == "retry-zimage-done":
        return retry_done_zimage(batch_id)
    if action == "scan-ocr":
        return scan_done_ocr(batch_id)
    if action == "restart-clean":
        return restart_clean(batch_id)
    states = {"start": "running", "resume": "running", "pause": "paused", "stop": "stopped"}
    if action not in states:
        raise ValueError("Neznámá akce dávky.")
    with db._LOCK:
        conn = db.connect()
        row = conn.execute("SELECT status FROM comfy_image_batches WHERE id=?", (int(batch_id),)).fetchone()
        if not row:
            raise ValueError("Dávka neexistuje.")
        if row["status"] == "done" and action in ("start", "resume"):
            raise ValueError("Dokončenou dávku není třeba znovu spouštět.")
        # Existující pozastavená dávka mohla vzniknout se starší verzí pravidel.
        # Před pokračováním proto přepočítáme model u dosud nehotových položek.
        if action in ("start", "resume"):
            mode_row = conn.execute("SELECT mode FROM comfy_image_batches WHERE id=?",
                                    (int(batch_id),)).fetchone()
            mode = str(mode_row["mode"] if mode_row else "hybrid")
            all_items = [dict(one) for one in conn.execute(
                "SELECT id,subject,item_ref,source_index,status FROM comfy_image_batch_items WHERE batch_id=?",
                (int(batch_id),)).fetchall()]
            models = story_models(all_items, mode)
            pending = [one for one in all_items if one["status"] == "pending"]
            conn.executemany("UPDATE comfy_image_batch_items SET model=?,seed=? WHERE id=?",
                             [(models[int(one["source_index"])],
                               story_seed(str(one["item_ref"] or ""), int(one["source_index"])),
                               int(one["id"]))
                              for one in pending])
        conn.execute("UPDATE comfy_image_batches SET status=?,updated_at=datetime('now'),"
                     "started_at=COALESCE(started_at,datetime('now')) WHERE id=?",
                     (states[action], int(batch_id)))
        conn.commit()
    return get_batch(batch_id)


def retry_done_zimage(batch_id: int) -> Dict[str, Any]:
    """Zálohuje hotové Z-Image výstupy a vrátí je do fronty přes Qwen.

    Je to opravná akce pro dávku, ve které Z-Image vepsal prompt do obrázku.
    Nic se nemaže: původní PNG zůstanou v ``_rejected_text``.
    """
    with db._LOCK:
        conn = db.connect()
        batch_row = conn.execute("SELECT * FROM comfy_image_batches WHERE id=?",
                                 (int(batch_id),)).fetchone()
        if not batch_row:
            raise ValueError("Dávka neexistuje.")
        batch = dict(batch_row)
        if batch["status"] == "running":
            raise ValueError("Nejdřív dávku pozastav.")
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM comfy_image_batch_items WHERE batch_id=? AND status='done' AND model='zimage'",
            (int(batch_id),)).fetchall()]
        if not items:
            raise ValueError("Žádné dokončené Z-Image položky k opravě nebyly nalezeny.")
        root = Path(batch["output_dir"]).resolve()
        rejected = root / "_rejected_text"
        for item in items:
            rel = str(item.get("output_file") or "")
            source = (root / rel).resolve()
            try:
                source.relative_to(root)
            except ValueError:
                continue
            if source.is_file():
                target = rejected / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(target.stem + f"_{int(time.time())}" + target.suffix)
                source.replace(target)
        ids = [int(one["id"]) for one in items]
        conn.executemany(
            "UPDATE comfy_image_batch_items SET status='pending',model='qwen',priority=100,"
            "output_file=NULL,prompt_id=NULL,error=NULL,seconds=NULL,updated_at=datetime('now') WHERE id=?",
            [(one,) for one in ids])
        conn.execute("UPDATE comfy_image_batches SET done_count=MAX(0,done_count-?),latest_file=NULL,"
                     "status='paused',finished_at=NULL,updated_at=datetime('now') WHERE id=?",
                     (len(ids), int(batch_id)))
        conn.commit()
    return get_batch(batch_id)


def scan_done_ocr(batch_id: int) -> Dict[str, Any]:
    """Prověří OCR i obrázky dokončené před instalací automatické kontroly."""
    batch = get_batch(batch_id)
    if not batch:
        raise ValueError("Dávka neexistuje.")
    if batch["status"] == "running":
        raise ValueError("Nejdřív dávku pozastav.")
    if not ocr_available():
        raise ValueError("Lokální OCR není nainstalované.")
    with db._LOCK:
        rows = [dict(r) for r in db.connect().execute(
            "SELECT * FROM comfy_image_batch_items WHERE batch_id=? AND status='done' "
            "AND output_file IS NOT NULL ORDER BY source_index", (int(batch_id),)).fetchall()]
    root = Path(batch["output_dir"]).resolve()
    bad: List[Tuple[int, str]] = []
    for item in rows:
        path = (root / str(item.get("output_file") or "")).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file():
            continue
        text = detect_text(path)
        if not text:
            continue
        rejected = root / "_ocr_rejected" / "precheck" / path.relative_to(root)
        rejected.parent.mkdir(parents=True, exist_ok=True)
        if rejected.exists():
            rejected = rejected.with_name(rejected.stem + f"_{int(time.time())}" + rejected.suffix)
        path.replace(rejected)
        bad.append((int(item["id"]), text))
    if bad:
        with db._LOCK:
            conn = db.connect()
            conn.executemany(
                "UPDATE comfy_image_batch_items SET status='pending',output_file=NULL,prompt_id=NULL,"
                "seed=seed+1000003,priority=500,retry_count=retry_count+1,ocr_text=?,"
                "updated_at=datetime('now') WHERE id=?", [(text, item_id) for item_id, text in bad])
            conn.execute("UPDATE comfy_image_batches SET done_count=MAX(0,done_count-?),"
                         "ocr_retry_count=ocr_retry_count+?,latest_file=NULL,finished_at=NULL,"
                         "updated_at=datetime('now') WHERE id=?", (len(bad), len(bad), int(batch_id)))
            conn.commit()
    result = get_batch(batch_id)
    result["scan_found"] = len(bad)
    return result


def restart_clean(batch_id: int) -> Dict[str, Any]:
    """Trvale smaže všechny výstupy dávky, vynuluje ji a rovnou spustí od začátku."""
    batch = get_batch(batch_id)
    if not batch:
        raise ValueError("Dávka neexistuje.")
    if batch["status"] == "running":
        raise ValueError("Nejdřív dávku pozastav.")
    with db._LOCK:
        conn = db.connect()
        processing = int(conn.execute(
            "SELECT COUNT(*) FROM comfy_image_batch_items WHERE batch_id=? AND status='processing'",
            (int(batch_id),)).fetchone()[0])
    if processing:
        raise ValueError("Právě běžící obrázek ještě dobíhá. Opakuj akci za chvíli.")

    root = BATCH_ROOT.resolve()
    output = Path(batch["output_dir"]).resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ValueError("Výstupní složka neleží v bezpečném adresáři image_batches.") from exc
    if output == root:
        raise ValueError("Nelze čistit kořenovou složku všech dávek.")
    if output.exists():
        for child in output.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    output.mkdir(parents=True, exist_ok=True)

    mode = str(batch.get("mode") or "hybrid")
    with db._LOCK:
        conn = db.connect()
        rows = [dict(one) for one in conn.execute(
            "SELECT id,subject,source_index,item_ref FROM comfy_image_batch_items WHERE batch_id=?",
            (int(batch_id),)).fetchall()]
        models = story_models(rows, mode)
        conn.executemany(
            "UPDATE comfy_image_batch_items SET model=?,seed=?,status='pending',output_file=NULL,"
            "prompt_id=NULL,error=NULL,seconds=NULL,priority=0,retry_count=0,ocr_text=NULL,"
            "updated_at=datetime('now') WHERE id=?",
            [(models[int(one["source_index"])],
              story_seed(str(one["item_ref"] or ""), int(one["source_index"])),
              int(one["id"])) for one in rows])
        conn.execute(
            "UPDATE comfy_image_batches SET status='running',done_count=0,error_count=0,"
            "ocr_retry_count=0,current_index=NULL,latest_file=NULL,started_at=datetime('now'),"
            "finished_at=NULL,updated_at=datetime('now') WHERE id=?", (int(batch_id),))
        conn.commit()
    log.warning("Dávka %s byla včetně všech výstupů vyčištěna a spuštěna od začátku.", batch_id)
    return get_batch(batch_id)


def latest_output_path(batch_id: int) -> Optional[Path]:
    batch = get_batch(batch_id)
    if not batch or not batch.get("latest_file"):
        return None
    root = Path(batch["output_dir"]).resolve()
    target = (root / str(batch["latest_file"])).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.is_file() else None


def _claim() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    with db._LOCK:
        conn = db.connect()
        batch_row = conn.execute(
            "SELECT * FROM comfy_image_batches WHERE status='running' ORDER BY id LIMIT 1").fetchone()
        if not batch_row:
            return {}, {}
        batch = dict(batch_row)
        item_row = conn.execute(
            "SELECT * FROM comfy_image_batch_items WHERE batch_id=? AND status='pending' "
            "ORDER BY priority DESC, CASE model WHEN 'zimage' THEN 0 ELSE 1 END, source_index LIMIT 1",
            (batch["id"],)).fetchone()
        if not item_row:
            processing = conn.execute(
                "SELECT COUNT(*) FROM comfy_image_batch_items WHERE batch_id=? AND status='processing'",
                (batch["id"],)).fetchone()[0]
            if not processing:
                conn.execute("UPDATE comfy_image_batches SET status='done',finished_at=datetime('now'),"
                             "updated_at=datetime('now') WHERE id=?", (batch["id"],))
                conn.commit()
            return {}, {}
        item = dict(item_row)
        conn.execute("UPDATE comfy_image_batch_items SET status='processing',updated_at=datetime('now') WHERE id=?",
                     (item["id"],))
        conn.execute("UPDATE comfy_image_batches SET current_index=?,updated_at=datetime('now') WHERE id=?",
                     (item["source_index"], batch["id"]))
        conn.commit()
        return batch, item


def _final_prompt(item: dict, batch: dict) -> str:
    # Původní JSON zůstává autoritativní pro obsah scény. Za něj ale
    # připojíme jediný společný výtvarný klíč dávky, aby celá kniha
    # vypadala jako práce jednoho ilustrátora.
    source_prompt = str(item.get("source_prompt") or "").strip()
    if source_prompt:
        style = str(batch.get("style_prompt") or "").strip()
        return source_prompt + ((" Consistent visual style for the entire story: " + style)
                                if style else "")
    if item["model"] == "qwen":
        return ("Create a purely visual illustration with absolutely no writing. Convert the meaning "
                "into a concrete action scene with characters and objects. The concept to visualize is: "
                + item["subject"] + ". Treat those words only as private scene directions. Never display, "
                "quote, spell, label or caption any of them inside the image. This is an illustration, "
                "not a poster, card, sign or book cover. " + batch["style_prompt"])
    return "Depict clearly: " + item["subject"] + ". " + batch["style_prompt"]


def _write_manifest(batch: dict, item: dict, relative: str, seconds: float) -> None:
    path = Path(batch["output_dir"]) / "manifest.csv"
    fresh = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        if fresh:
            writer.writerow(["index", "item", "image", "file", "model", "seed", "seconds",
                             "ocr_retries", "subject"])
        writer.writerow([item["source_index"], item["item_ref"], item["image_ref"], relative,
                         item["model"], item["seed"], seconds, item.get("retry_count", 0),
                         item["subject"]])


def _convert_to_webp(source: Path, target: Path) -> None:
    """Převede do skutečného WebP (pouhé přejmenování PNG by nestačilo)."""
    from PIL import Image
    with Image.open(source) as image:
        rgb = image.convert("RGB")
        temporary = target.with_suffix(target.suffix + ".part")
        rgb.save(temporary, format="WEBP", quality=92, method=6)
    temporary.replace(target)
    if source.resolve() != target.resolve() and source.exists():
        source.unlink()


class ImageBatchRunner(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="comfylocal-image-batch")
        self.stop_event = threading.Event()
        self.client = ComfyClient()

    def run(self) -> None:
        init_schema()
        log.info("Dávkový render obrázků běží, ComfyUI = %s", self.client.base)
        while not self.stop_event.is_set():
            try:
                batch, item = _claim()
                if not item:
                    self.stop_event.wait(1.5)
                    continue
                self._render(batch, item)
            except Exception:
                log.exception("Dávkový render: neočekávaná chyba")
                self.stop_event.wait(2)

    def _render(self, batch: dict, item: dict) -> None:
        started = time.time()
        error = ""
        relative = ""
        actual_model = str(item["model"])
        ocr_text = ""
        retry_count = int(item.get("retry_count") or 0)
        try:
            folder_name = f"batch_{((int(item['source_index']) - 1) // 1000) + 1:04d}"
            folder = Path(batch["output_dir"]) / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            image_ref = _safe(item.get("image_ref"), f"image-{int(item['source_index']):06d}")
            target = folder / f"{int(item['source_index']):06d}_{image_ref}.webp"

            # OCR může změnit seed, ale nikdy model. Přepnutí modelu uprostřed
            # pohádky by porušilo její výtvarnou kontinuitu.
            max_attempts = 3
            for attempt in range(max_attempts):
                actual_model = str(item["model"])
                attempt_item = dict(item, model=actual_model)
                prompt = _final_prompt(attempt_item, batch)
                builder = (_workflow_qwen if actual_model == "qwen" else
                           _workflow_flux1 if actual_model == "flux1" else
                           _workflow_flux2 if actual_model == "flux2" else _workflow_zimage)
                seed = int(item["seed"]) + attempt * 1_000_003
                prompt_id = self.client.submit(builder(prompt, seed), str(uuid.uuid4()))
                with db._LOCK:
                    conn = db.connect()
                    conn.execute("UPDATE comfy_image_batch_items SET prompt_id=? WHERE id=?",
                                 (prompt_id, item["id"]))
                    conn.commit()
                history = None
                deadline = time.time() + 1200
                while time.time() < deadline and not self.stop_event.is_set():
                    history = self.client.history(prompt_id, allow_empty=True)
                    if history is not None:
                        break
                    time.sleep(1.5)
                if history is None:
                    raise ComfyError("ComfyUI nedokončilo obrázek do 20 minut.")
                raise_if_history_failed(history)
                outputs = find_output_files(history)
                images = [one for one in outputs if one.get("bucket") == "images"]
                if not images:
                    raise ComfyError("ComfyUI nevrátilo žádný obrázek.")
                downloaded = self.client.download_output(images[0], folder)
                if target.exists():
                    target.unlink()
                _convert_to_webp(downloaded, target)

                ocr_text = detect_text(target)
                if not ocr_text:
                    break

                retry_count += 1
                relative_attempt = str(target.relative_to(Path(batch["output_dir"])))
                rejected = (Path(batch["output_dir"]) / "_ocr_rejected" /
                            f"attempt_{attempt + 1}" / relative_attempt)
                rejected.parent.mkdir(parents=True, exist_ok=True)
                if rejected.exists():
                    rejected = rejected.with_name(
                        rejected.stem + f"_{int(time.time())}" + rejected.suffix)
                target.replace(rejected)
                with db._LOCK:
                    conn = db.connect()
                    conn.execute("UPDATE comfy_image_batch_items SET retry_count=?,ocr_text=?,"
                                 "updated_at=datetime('now') WHERE id=?",
                                 (retry_count, ocr_text, item["id"]))
                    conn.execute("UPDATE comfy_image_batches SET ocr_retry_count=ocr_retry_count+1,"
                                 "updated_at=datetime('now') WHERE id=?", (batch["id"],))
                    conn.commit()
                log.warning("Dávka %s, položka %s: OCR našlo %r, nový seed (pokus %s/%s)",
                            batch["id"], item["source_index"], ocr_text, attempt + 1, max_attempts)
                if attempt == max_attempts - 1:
                    raise ComfyError(
                        f"OCR našlo text i po {max_attempts} pokusech: {ocr_text}")

            relative = str(target.relative_to(Path(batch["output_dir"]))).replace("\\", "/")
        except Exception as exc:
            error = str(exc)[:3000]

        seconds = round(time.time() - started, 1)
        with db._LOCK:
            conn = db.connect()
            if error:
                conn.execute("UPDATE comfy_image_batch_items SET status='error',error=?,seconds=?,"
                             "updated_at=datetime('now') WHERE id=?", (error, seconds, item["id"]))
                conn.execute("UPDATE comfy_image_batches SET error_count=error_count+1,"
                             "updated_at=datetime('now') WHERE id=?", (batch["id"],))
            else:
                conn.execute("UPDATE comfy_image_batch_items SET status='done',output_file=?,seconds=?,"
                             "model=?,retry_count=?,ocr_text=?,updated_at=datetime('now') WHERE id=?",
                             (relative, seconds, actual_model, retry_count, ocr_text or None, item["id"]))
                conn.execute("UPDATE comfy_image_batches SET done_count=done_count+1,latest_file=?,"
                             "updated_at=datetime('now') WHERE id=?", (relative, batch["id"]))
            conn.commit()
        if error:
            log.error("Dávka %s, položka %s selhala: %s", batch["id"], item["source_index"], error)
        else:
            item["model"] = actual_model
            item["retry_count"] = retry_count
            _write_manifest(batch, item, relative, seconds)
            log.info("Dávka %s: položka %s hotová za %.1f s (OCR opakování %s)",
                     batch["id"], item["source_index"], seconds, retry_count)


_RUNNER: Optional[ImageBatchRunner] = None
_RUNNER_LOCK = threading.Lock()


def start_runner() -> ImageBatchRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None or not _RUNNER.is_alive():
            _RUNNER = ImageBatchRunner()
            _RUNNER.start()
        return _RUNNER
