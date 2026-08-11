# -*- coding: utf-8 -*-
"""API kompatibilní s api.php.

Frontend je přenesený z app.php beze změny logiky, takže mluví na `api.php?action=…`
a čeká stejné tvary odpovědí. Tenhle modul je poskytuje nad lokální SQLite frontou
a ComfyUI na síti — bez workerů, tokenů, uživatelů a FTP.
"""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from . import control, db, projects as projects_mod, users as users_mod
from .comfy_client import IMAGE_SUFFIXES, ComfyClient, normalize_image_suffix
from .config import CONFIG
from .presets import camera_preset_text
from .runner import get_runner
from .translate import translate_text_online
from .workflow import (ENHANCE_TOKENS_MAX, list_workflows, load_workflow, ltx_safe_size,
                       workflow_is_photo_edit)

log = logging.getLogger("comfylocal.api")
router = APIRouter()

APP_VERSION = "1.0.0"
PIN_COOKIE = "comfylocal_pin"
MAX_BATCH = 40
SEED_MODES = ("increment_batch", "locked", "random_each")


# ── odpovědi ────────────────────────────────────────────────
def ok(data: Optional[Dict[str, Any]] = None) -> JSONResponse:
    payload = {"success": True}
    payload.update(data or {})
    return JSONResponse(payload)


def fail(message: str, code: int = 400, extra: Optional[Dict[str, Any]] = None) -> JSONResponse:
    payload = {"success": False, "error": message}
    payload.update(extra or {})
    return JSONResponse(payload, status_code=code)


# ── přístup ─────────────────────────────────────────────────
def users_enabled() -> bool:
    """Účty mají přednost před PINem — stejně jako přihlášení na webu."""
    try:
        return users_mod.has_users()
    except Exception:
        return False


def pin_required() -> bool:
    if users_enabled():
        return False
    return bool(str(CONFIG.get("access_pin") or "").strip())


def login_required() -> bool:
    return users_enabled() or pin_required()


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    """Přihlášený účet, nebo None. Bez účtů se chová jako dřív (admin)."""
    if users_enabled():
        return users_mod.user_for_token(request.cookies.get(users_mod.SESSION_COOKIE) or "")
    if pin_required() and request.cookies.get(PIN_COOKIE) != str(CONFIG.get("access_pin")).strip():
        return None
    return {"id": 0, "username": "ComfyLocal", "role": "admin", "is_admin": True, "active": 1}


def authenticated(request: Request) -> bool:
    return current_user(request) is not None


def is_admin(request: Request) -> bool:
    user = current_user(request)
    return bool(user and user.get("is_admin"))


# ── pomůcky ─────────────────────────────────────────────────
def clean_text(value: Any, max_len: int) -> str:
    text = str(value if value is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()[:max_len]


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


def clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def random_seed() -> int:
    return random.randint(1, 2147483647)


def snap_size(value: Any, default: int, settings: Dict[str, Any]) -> int:
    """Rozměr v mezích 256–4096; u LTX navíc srovnaný na jeho mřížku.

    Photo edit (Flux.2, FireRed) půlku latentu ani 2× upscaler nemá, takže
    by mu srovnávání jen bezdůvodně měnilo zadané rozměry.
    """
    size = clamp_int(value, 256, 4096, default)
    if str(settings.get("input_mode") or "").lower() == "photo_edit":
        return size
    return ltx_safe_size(size, default)


def build_comfy_prompt(prompt: str, preset: str, camera_motion: str) -> str:
    """Stejné složení jako na webu: prompt + pohyb kamery pro překlad."""
    motion = clean_text(camera_motion or camera_preset_text(preset), 1000)
    parts = [p for p in (clean_text(prompt, 6000), motion) if p]
    return ", ".join(parts)


def prepare_job_settings(settings: Dict[str, Any], preset: str, prompt: str,
                         negative: str) -> Tuple[Dict[str, Any], str, str]:
    """Port prepare_job_settings() z api.php — hranice hodnot a překlad promptu."""
    settings = dict(settings or {})
    settings["width"] = snap_size(settings.get("width"), 1280, settings)
    settings["height"] = snap_size(settings.get("height"), 720, settings)
    settings["fps"] = clamp_int(settings.get("fps"), 1, 60, 25)
    settings["duration"] = clamp_float(settings.get("duration"), 1, 60, 5)
    settings["frame_count"] = max(1, min(3600, round(settings["fps"] * settings["duration"])))
    settings["steps"] = clamp_int(settings.get("steps"), 1, 200, 30)
    settings["cfg"] = clamp_float(settings.get("cfg"), 0, 30, 3.5)
    settings["motion_strength"] = clamp_float(settings.get("motion_strength"), 0, 2, 0.75)
    settings["prompt_enhance"] = bool(settings.get("prompt_enhance"))
    settings["enhance_tokens"] = clamp_int(settings.get("enhance_tokens"), 64, ENHANCE_TOKENS_MAX, 512)
    seed_raw = settings.get("seed")
    settings["seed"] = clamp_int(seed_raw, 1, 2147483647, random_seed()) if str(seed_raw or "") != "" else random_seed()
    sm = str(settings.get("seed_mode") or "increment_batch")
    settings["seed_mode"] = sm if sm in SEED_MODES else "increment_batch"
    settings["camera_motion"] = clean_text(settings.get("camera_motion"), 1000) or clean_text(
        camera_preset_text(preset), 1000)
    settings["style"] = clean_text(settings.get("style"), 1000)

    original_prompt = clean_text(settings.get("original_prompt") or prompt, 6000)
    original_negative = clean_text(settings.get("original_negative_prompt") or negative, 4000)
    settings["original_prompt"] = original_prompt or prompt
    settings["original_negative_prompt"] = original_negative
    settings["translated"] = bool(settings.get("translated"))
    settings["translation_provider"] = clean_text(settings.get("translation_provider"), 80) or None

    translate_enabled = bool(settings.get("translate_prompt", CONFIG.get("translate_prompt", True)))
    if translate_enabled:
        source = str(CONFIG.get("translate_source_lang") or "cs")
        target = str(CONFIG.get("translate_target_lang") or "en")
        main = build_comfy_prompt(settings["original_prompt"], preset, settings["camera_motion"])
        tr = translate_text_online(main, source, target)
        if tr.get("success") and str(tr.get("translated") or "").strip():
            prompt = clean_text(tr["translated"], 6000)
            settings["translated"] = True
            settings["translation_provider"] = tr.get("provider") or "online"
        if settings["original_negative_prompt"]:
            neg_tr = translate_text_online(settings["original_negative_prompt"], source, target)
            if neg_tr.get("success") and str(neg_tr.get("translated") or "").strip():
                negative = clean_text(neg_tr["translated"], 4000)
    return settings, prompt, negative


async def save_upload(upload, prefix: str) -> str:
    """Uloží nahraný obrázek do data/uploads a vrátí relativní cestu."""
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = CONFIG.uploads_dir / f"{prefix}_{stamp}_{random.getrandbits(40):010x}{suffix}"
    with dst.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    if dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise ValueError(f"Soubor {upload.filename} je prázdný.")
    dst = normalize_image_suffix(dst)
    return dst.relative_to(CONFIG.base_dir).as_posix()


def resolve_project(project_id: Optional[int], settings: Dict[str, Any]) -> Optional[int]:
    """Když frontend projekt nepošle, doplníme výchozí podle režimu."""
    if project_id:
        return int(project_id)
    two_pict = str(settings.get("input_mode") or "").lower() in ("2pict", "flf2v")
    return projects_mod.default_project_id("flf2v" if two_pict else "i2v")


def workers_payload() -> Dict[str, Any]:
    """Náhrada za stats_workers.json: jediný „stroj" je ComfyUI na síti."""
    runner = get_runner()
    online = runner.client.online()
    error = runner.client.last_error
    stats = runner.client.system_stats() if online else {}
    devices = stats.get("devices") or []
    gpu = None
    if devices:
        d = devices[0]
        total = float(d.get("vram_total") or 0)
        free = float(d.get("vram_free") or 0)
        if total > 0:
            gpu = {
                "name": str(d.get("name") or "GPU"),
                "util_pct": None,   # ComfyUI /system_stats vytížení GPU nehlásí
                "mem_used_mb": round((total - free) / 1048576),
                "mem_total_mb": round(total / 1048576),
                "temp_c": None,
            }
    return {
        CONFIG.comfy_host or "ComfyUI": {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "worker": {"version": APP_VERSION, "active_job": runner.active_job_id or 0},
            "comfy": {"online": online, "state": "ready" if online else "offline",
                      "url": runner.client.base, "error": None if online else error},
            "gpu": gpu,
        }
    }


def viewer_scope(request: Request) -> Tuple[Optional[int], bool]:
    """(user_id, is_admin) pro filtrování jobů. user_id=None znamená „vidí vše".

    Bez účtů má přihlášený uživatel id 0 — to je jednouživatelský režim, kde není
    co skrývat, takže se chová jako None.
    """
    user = current_user(request) or {}
    uid = int(user.get("id") or 0)
    return (uid or None), bool(user.get("is_admin"))


def job_owner(request: Request) -> Tuple[Optional[int], str]:
    """(user_id, user_name) pro nově zakládaný job."""
    user = current_user(request) or {}
    uid = int(user.get("id") or 0)
    return (uid or None), str(user.get("username") or "")


def may_see_job(job: Optional[Dict[str, Any]], user_id: Optional[int], is_admin: bool) -> bool:
    if not job:
        return False
    if is_admin or user_id is None:
        return True
    owner = job.get("user_id")
    return owner is None or int(owner) == int(user_id)


def dashboard_payload(status: str = "", limit: int = 200, detail_id: int = 0,
                      user_id: Optional[int] = None, is_admin: bool = True) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "success": True,
        "jobs": db.list_jobs_for_user(user_id, is_admin, status, limit),
        "workers": workers_payload(),
        "queue_counts": db.queue_counts(),
        "jobs_ahead": db.jobs_ahead_of_user(user_id) if user_id else 0,
        "avg_job_seconds": db.average_job_seconds(),
        "eta_seconds": db.queue_eta_seconds(user_id) if user_id else None,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if detail_id > 0:
        job = db.get_job(detail_id)
        if job and not may_see_job(job, user_id, is_admin):
            out["detail_error"] = "Tenhle job patří jinému uživateli."
        elif job:
            out["detail"] = {"job": job, "events": db.job_events(detail_id)}
        else:
            out["detail_error"] = "Job nenalezen."
    return out


# ── router ──────────────────────────────────────────────────
@router.api_route("/api.php", methods=["GET", "POST"])
async def api_php(request: Request):
    action = str(request.query_params.get("action") or "").strip()
    method = request.method.upper()

    if action == "has_users":
        return ok({"has_users": users_enabled(), "pin_required": pin_required()})

    if action == "login":
        form = await request.form()
        username = clean_text(form.get("username"), 60)
        password = str(form.get("password") or form.get("pin") or "")

        # 1) Účty (jméno + heslo) — port přihlášení z webové verze.
        if users_enabled():
            if not username:
                return fail("Zadejte uživatelské jméno.", 401)
            if users_mod.throttled(username):
                return fail("Moc mnoho pokusů. Zkus to za pár minut.", 429)
            session = users_mod.login(username, password)
            if not session:
                users_mod.record_fail(username)
                time.sleep(1)
                return fail("Nesprávné jméno nebo heslo.", 401)
            users_mod.clear_fails(username)
            resp = ok({"user": session["user"], "role": session["user"]["role"],
                       "username": session["user"]["username"], "users_enabled": True})
            resp.set_cookie(users_mod.SESSION_COOKIE, session["token"], httponly=True,
                            samesite="lax", max_age=60 * 60 * 24 * users_mod.SESSION_DAYS)
            return resp

        # 2) Původní režim: PIN, nebo volný přístup.
        if not pin_required():
            return ok({"pin_required": False})
        if password.strip() != str(CONFIG.get("access_pin")).strip():
            time.sleep(1)
            return fail("Nesprávný PIN.", 401)
        resp = ok({"pin_required": True})
        resp.set_cookie(PIN_COOKIE, str(CONFIG.get("access_pin")).strip(), httponly=True,
                        samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp

    if action == "logout":
        users_mod.logout(request.cookies.get(users_mod.SESSION_COOKIE) or "")
        resp = ok()
        resp.delete_cookie(PIN_COOKIE)
        resp.delete_cookie(users_mod.SESSION_COOKIE)
        return resp

    if not authenticated(request):
        return fail("Nepřihlášeno.", 401, {"auth_expired": True})

    handler = HANDLERS.get(action)
    if handler is None:
        return fail(f"Neznámá akce: {action or '(prázdná)'}", 400)
    return await handler(request, method)


# ── jednotlivé akce ─────────────────────────────────────────
async def h_me(request: Request, method: str):
    user = current_user(request) or {}
    return ok({"user": {"username": user.get("username") or "ComfyLocal",
                        "is_admin": bool(user.get("is_admin")),
                        "role": user.get("role") or "user",
                        "user_id": user.get("id") or 0},
               "username": user.get("username") or "ComfyLocal",
               "role": user.get("role") or "user",
               "is_admin": bool(user.get("is_admin")),
               "authenticated": True,
               "users_enabled": users_enabled(),
               "version": APP_VERSION, "pin_required": pin_required()})


# ── účty (admin) ────────────────────────────────────────────
async def h_list_users(request: Request, method: str):
    if not is_admin(request):
        return fail("Jen pro správce.", 403)
    return ok({"users": users_mod.list_users()})


async def h_save_user(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    if not is_admin(request):
        return fail("Jen pro správce.", 403)
    body = await _json_body(request)
    try:
        user = users_mod.save_user(
            int(body.get("id") or 0) or None,
            clean_text(body.get("username"), 60),
            str(body.get("password") or ""),
            str(body.get("role") or "user"),
            bool(body.get("active", True)),
        )
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        msg = str(e)
        if "UNIQUE" in msg:
            return fail("Takové uživatelské jméno už existuje.")
        return fail(f"Účet se nepodařilo uložit: {msg}")
    return ok({"user": user, "users": users_mod.list_users()})


async def h_delete_user(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    if not is_admin(request):
        return fail("Jen pro správce.", 403)
    body = await _json_body(request)
    user_id = int(body.get("id") or 0)
    if not user_id:
        return fail("ID chybí.")
    me = current_user(request) or {}
    if int(me.get("id") or 0) == user_id:
        return fail("Vlastní účet smazat nejde.")
    admins = [u for u in users_mod.list_users() if u["is_admin"] and u["active"]]
    if len(admins) <= 1 and any(u["id"] == user_id for u in admins):
        return fail("Musí zůstat alespoň jeden aktivní správce.")
    users_mod.delete_user(user_id)
    return ok({"users": users_mod.list_users()})


# ── ovládání ComfyUI a render loopu ─────────────────────────
async def h_start_comfy(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    result = control.start_comfy()
    if not result.get("success"):
        return fail(str(result.get("error") or "Start ComfyUI selhal."), 409)
    return ok(result)


async def h_restart_worker(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    return ok(control.restart_runner())


async def h_control_status(request: Request, method: str):
    return ok({"control": control.status(), "workers": workers_payload()})


async def h_translate_prompt(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    body = await _json_body(request)
    text = clean_text(body.get("text"), 6000)
    source = clean_text(body.get("source") or CONFIG.get("translate_source_lang") or "cs", 12)
    target = clean_text(body.get("target") or CONFIG.get("translate_target_lang") or "en", 12)
    result = translate_text_online(text, source or "cs", target or "en")
    if not result.get("success") or not str(result.get("translated") or "").strip():
        # Frontend bere neúspěšný překlad jako fatální a job vůbec neodešle.
        # V interní síti bez výstupu do internetu by tím byla appka nepoužitelná,
        # takže vracíme původní text a jen označíme, že se nepřekládalo.
        log.info("Překlad nedostupný (%s) — posílám prompt v původním jazyce.",
                 result.get("error") or result.get("provider"))
        return JSONResponse({
            "success": True,
            "translated": text,
            "provider": "none",
            "fallback": True,
            "providers_tried": result.get("providers_tried") or [],
            "note": "Překlad není dostupný, prompt jde do ComfyUI v původním jazyce.",
        })
    return JSONResponse({**result, "success": True})


async def h_create_job(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    form = await request.form()
    image = form.get("image")
    if image is None or not getattr(image, "filename", ""):
        return fail("Chybí vstupní obrázek.")
    prompt = clean_text(form.get("prompt"), 6000)
    if not prompt:
        return fail("Prompt je prázdný.")
    negative = clean_text(form.get("negative_prompt"), 4000)
    preset = clean_text(form.get("preset") or "Statická kamera (stativ)", 80)
    try:
        settings = json.loads(str(form.get("settings_json") or "{}"))
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}

    settings, prompt, negative = prepare_job_settings(settings, preset, prompt, negative)
    try:
        rel = await save_upload(image, "input")
    except ValueError as e:
        return fail(str(e))

    image2 = form.get("image2")
    if image2 is not None and getattr(image2, "filename", ""):
        try:
            rel2 = await save_upload(image2, "input2")
        except ValueError as e:
            (CONFIG.base_dir / rel).unlink(missing_ok=True)
            return fail(str(e))
        settings["input_image_2"] = rel2
        settings["input_original_name_2"] = clean_text(image2.filename, 240)
        settings["input_mode"] = "2pict"
    else:
        settings["input_mode"] = settings.get("input_mode") or "1pict"

    project_id = resolve_project(_int(form.get("project_id")), settings)
    owner_id, owner_name = job_owner(request)
    job_id = db.create_job(prompt, negative, preset, rel,
                           clean_text(getattr(image, "filename", ""), 240), settings, project_id,
                           owner_id, owner_name)
    db.add_event(job_id, "create",
                 "Job vytvořen + prompt přeložen do EN" if settings.get("translated") else "Job vytvořen",
                 {"settings": settings})
    return ok({"id": job_id})


async def h_create_jobs_batch(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    form = await request.form()
    # Frontend posílá pole po PHP zvyku jako images[]; bereme oba názvy.
    images = [f for f in (form.getlist("images[]") + form.getlist("images"))
              if getattr(f, "filename", "")]
    if not images:
        return fail("Chybí vstupní obrázky.")
    if len(images) > MAX_BATCH:
        return fail(f"Jedna dávka může mít maximálně {MAX_BATCH} obrázků.", 413)
    prompt_base = clean_text(form.get("prompt"), 6000)
    if not prompt_base:
        return fail("Prompt je prázdný.")
    negative_base = clean_text(form.get("negative_prompt"), 4000)
    preset_base = clean_text(form.get("preset") or "Statická kamera (stativ)", 80)
    try:
        settings_list = json.loads(str(form.get("settings_jsons") or "[]"))
    except Exception:
        settings_list = []
    if not isinstance(settings_list, list):
        settings_list = []

    created: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for index, upload in enumerate(images):
        name = getattr(upload, "filename", f"image_{index + 1}")
        try:
            settings = settings_list[index] if index < len(settings_list) else {}
            if not isinstance(settings, dict):
                settings = {}
            settings, prompt, negative = prepare_job_settings(dict(settings), preset_base,
                                                             prompt_base, negative_base)
            settings["input_mode"] = settings.get("input_mode") or "1pict"
            rel = await save_upload(upload, "input")
            project_id = resolve_project(_int(form.get("project_id")), settings)
            job_id = db.create_job(prompt, negative, preset_base, rel, clean_text(name, 240),
                                   settings, project_id, *job_owner(request))
            db.add_event(job_id, "create",
                         "Job vytvořen v dávce + prompt přeložen do EN" if settings.get("translated")
                         else "Job vytvořen v dávce",
                         {"settings": settings, "batch_index": index})
            created.append({"id": job_id, "name": name})
        except Exception as e:
            log.warning("Job z dávky (%s) selhal: %s", name, e)
            failed.append({"name": name, "error": str(e)})

    return JSONResponse({
        "success": len(created) > 0,
        "created": created,
        "ids": [c["id"] for c in created],
        "failed": failed,
        "created_count": len(created),
        "failed_count": len(failed),
    })


async def h_rerun_job(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    body = await _json_body(request)
    new_seed = bool(body.get("new_seed", True))
    source_id = _int(body.get("id"))
    if not source_id:
        return fail("ID chybí.")
    src = db.get_job(source_id)
    if not src:
        return fail("Zdrojový job nenalezen.", 404)
    if not may_see_job(src, *viewer_scope(request)):
        return fail("Zopakovat jde jen vlastní job.", 403)
    src_rel = str(src.get("input_image") or "")
    src_path = CONFIG.base_dir / src_rel if src_rel else None
    if not src_path or not src_path.is_file():
        return fail("Původní obrázek už na disku není.")
    prompt = clean_text(src.get("prompt"), 6000)
    if not prompt:
        return fail("Původní prompt je prázdný.")

    settings = dict(src.get("settings") or {})
    settings["width"] = snap_size(settings.get("width"), 1280, settings)
    settings["height"] = snap_size(settings.get("height"), 720, settings)
    settings["fps"] = clamp_int(settings.get("fps"), 1, 60, 25)
    settings["duration"] = clamp_float(settings.get("duration"), 1, 60, 5)
    settings["frame_count"] = max(1, min(3600, round(settings["fps"] * settings["duration"])))
    settings["steps"] = clamp_int(settings.get("steps"), 1, 200, 30)
    settings["cfg"] = clamp_float(settings.get("cfg"), 0, 30, 3.5)
    settings["motion_strength"] = clamp_float(settings.get("motion_strength"), 0, 2, 0.75)
    settings["enhance_tokens"] = clamp_int(settings.get("enhance_tokens"), 64, ENHANCE_TOKENS_MAX, 512)
    settings["camera_motion"] = clean_text(settings.get("camera_motion"), 1000) or clean_text(
        camera_preset_text(src.get("preset") or ""), 1000)
    settings["style"] = clean_text(settings.get("style"), 1000)
    settings["seed"] = random_seed() if new_seed else clamp_int(settings.get("seed"), 1, 2147483647, random_seed())
    settings["seed_mode"] = "increment_batch" if new_seed else (
        settings.get("seed_mode") if settings.get("seed_mode") in SEED_MODES else "increment_batch")
    settings["rerun_from_job_id"] = source_id
    settings["rerun_new_seed"] = new_seed

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rel = f"data/uploads/input_{stamp}_{random.getrandbits(40):010x}{src_path.suffix or '.png'}"
    shutil.copy2(src_path, CONFIG.base_dir / rel)
    src2_rel = str(settings.get("input_image_2") or "")
    if src2_rel:
        src2_path = CONFIG.base_dir / src2_rel
        if src2_path.is_file():
            rel2 = f"data/uploads/input2_{stamp}_{random.getrandbits(40):010x}{src2_path.suffix or '.png'}"
            shutil.copy2(src2_path, CONFIG.base_dir / rel2)
            settings["input_image_2"] = rel2
            settings["input_mode"] = "2pict"
        else:
            settings.pop("input_image_2", None)
            settings["input_mode"] = "1pict"

    new_id = db.create_job(prompt, clean_text(src.get("negative_prompt"), 4000),
                           clean_text(src.get("preset"), 80), rel,
                           clean_text(src.get("input_original_name") or src_path.name, 240),
                           settings, src.get("project_id"), *job_owner(request))
    db.add_event(new_id, "rerun",
                 f"Job znovu zařazen z jobu #{source_id}" + (" s novým seedem" if new_seed else " se stejným seedem"),
                 {"source_job_id": source_id, "seed": settings["seed"], "new_seed": new_seed})
    return ok({"id": new_id, "seed": settings["seed"]})


async def h_update_pending_image(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    form = await request.form()
    job_id = _int(form.get("id"))
    image = form.get("image")
    if not job_id:
        return fail("ID chybí.")
    if image is None or not getattr(image, "filename", ""):
        return fail("Nový obrázek se nenahrál.")
    job = db.get_job(job_id)
    if not job:
        return fail("Job nenalezen.", 404)
    if not may_see_job(job, *viewer_scope(request)):
        return fail("Měnit jde jen vlastní job.", 403)
    if str(job.get("status")) != "pending":
        return fail("Fotku lze změnit jen u pending jobu.", 409)
    try:
        rel = await save_upload(image, "input_replace")
    except ValueError as e:
        return fail(str(e))
    old_rel = str(job.get("input_image") or "")
    db.update_job(job_id, input_image=rel,
                  input_original_name=clean_text(getattr(image, "filename", ""), 240))
    deleted_old = False
    if old_rel and old_rel != rel:
        deleted_old = db.cleanup_uploads() > 0
    db.add_event(job_id, "edit", "Vstupní fotka u pending jobu změněna",
                 {"old": old_rel, "new": rel, "deleted_old": deleted_old})
    return ok({"job": db.get_job(job_id), "deleted_old": deleted_old})


async def h_update_pending_job(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    body = await _json_body(request)
    job_id = _int(body.get("id"))
    if not job_id:
        return fail("ID chybí.")
    job = db.get_job(job_id)
    if not job:
        return fail("Job nenalezen.", 404)
    if not may_see_job(job, *viewer_scope(request)):
        return fail("Editovat jde jen vlastní job.", 403)
    if str(job.get("status")) != "pending":
        return fail("Editovat lze jen pending job.", 409)

    settings = dict(job.get("settings") or {})
    incoming = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    prompt_input = clean_text(body.get("prompt"), 6000)
    if not prompt_input:
        return fail("Prompt je prázdný.")
    negative_input = clean_text(body.get("negative_prompt"), 4000)
    preset = clean_text(body.get("preset") or job.get("preset") or "custom", 80)

    settings["width"] = snap_size(incoming.get("width", settings.get("width")), 1280, settings)
    settings["height"] = snap_size(incoming.get("height", settings.get("height")), 720, settings)
    settings["fps"] = clamp_int(incoming.get("fps", settings.get("fps")), 1, 60, 25)
    settings["duration"] = clamp_float(incoming.get("duration", settings.get("duration")), 1, 60, 5)
    settings["frame_count"] = max(1, min(3600, round(settings["fps"] * settings["duration"])))
    settings["steps"] = clamp_int(incoming.get("steps", settings.get("steps")), 1, 200, 30)
    settings["cfg"] = clamp_float(incoming.get("cfg", settings.get("cfg")), 0, 30, 3.5)
    settings["motion_strength"] = clamp_float(
        incoming.get("motion_strength", settings.get("motion_strength")), 0, 2, 0.75)
    settings["prompt_enhance"] = bool(incoming.get("prompt_enhance"))
    settings["enhance_tokens"] = clamp_int(
        incoming.get("enhance_tokens", settings.get("enhance_tokens")), 64, ENHANCE_TOKENS_MAX, 512)
    seed_in = incoming.get("seed")
    settings["seed"] = clamp_int(seed_in, 1, 2147483647, random_seed()) if str(seed_in or "") != "" else \
        clamp_int(settings.get("seed"), 1, 2147483647, random_seed())
    sm = str(incoming.get("seed_mode") or settings.get("seed_mode") or "increment_batch")
    settings["seed_mode"] = sm if sm in SEED_MODES else "increment_batch"
    settings["camera_motion"] = clean_text(
        incoming.get("camera_motion", settings.get("camera_motion")), 1000) or clean_text(
        camera_preset_text(preset), 1000)
    settings["style"] = clean_text(incoming.get("style", settings.get("style")), 1000)

    prompt = prompt_input
    negative = negative_input
    settings["original_prompt"] = prompt_input
    settings["original_negative_prompt"] = negative_input
    if clean_text(settings.get("input_language") or "en", 12) == "cs":
        source = str(CONFIG.get("translate_source_lang") or "cs")
        target = str(CONFIG.get("translate_target_lang") or "en")
        tr = translate_text_online(build_comfy_prompt(prompt_input, preset, settings["camera_motion"]),
                                   source, target)
        if tr.get("success") and str(tr.get("translated") or "").strip():
            prompt = clean_text(tr["translated"], 6000)
            settings["translated"] = True
            settings["translation_provider"] = tr.get("provider") or "online"
        else:
            settings["translated"] = False
            settings["translation_provider"] = "none"
        if negative_input:
            neg_tr = translate_text_online(negative_input, source, target)
            if neg_tr.get("success") and str(neg_tr.get("translated") or "").strip():
                negative = clean_text(neg_tr["translated"], 4000)

    fresh = db.get_job(job_id)
    if not fresh or str(fresh.get("status")) != "pending":
        return fail("Job už mezitím není pending.", 409)
    db.update_job(job_id, prompt=prompt, negative_prompt=negative, preset=preset, settings=settings)
    db.add_event(job_id, "edit", "Pending job upraven před renderem",
                 {"preset": preset, "settings": settings})
    return ok({"job": db.get_job(job_id)})


async def h_job_file(request: Request, method: str):
    job_id = _int(request.query_params.get("id"))
    kind = clean_text(request.query_params.get("kind") or "output", 20)
    if not job_id:
        return fail("ID chybí.")
    if kind not in ("input", "input2", "output"):
        return fail("Neplatný typ souboru.")
    job = db.get_job(job_id)
    if not job:
        return fail("Job nenalezen.", 404)
    uid, adm = viewer_scope(request)
    if not may_see_job(job, uid, adm):
        # Bez tohohle by stačilo hádat ID a stáhnout cizí video přímo z URL.
        return fail("Soubory cizího jobu nejsou dostupné.", 403)
    rel = {
        "input": job.get("input_image"),
        "input2": (job.get("settings") or {}).get("input_image_2"),
        "output": job.get("output_video"),
    }.get(kind)
    if not rel:
        return fail("Soubor u tohoto jobu není.", 404)
    path = (CONFIG.base_dir / str(rel)).resolve()
    allowed = (CONFIG.uploads_dir.resolve(), CONFIG.outputs_dir.resolve())
    if not any(str(path).startswith(str(root)) for root in allowed) or not path.is_file():
        return fail("Soubor neexistuje.", 404)
    inline = str(request.query_params.get("inline") or "") not in ("", "0", "false")
    if inline or kind != "output":
        return FileResponse(path)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


async def h_projects(request: Request, method: str):
    return ok({"projects": db.list_projects()})


async def h_default_workflow(request: Request, method: str):
    try:
        wf = load_workflow(str(CONFIG.get("default_workflow")))
    except Exception as e:
        return fail(str(e), 404)
    return JSONResponse(wf)


async def h_project_workflow(request: Request, method: str):
    project_id = _int(request.query_params.get("id"))
    if not project_id:
        return fail("ID chybí.")
    name = projects_mod.workflow_file_for_project(project_id)
    if not name:
        return fail("Workflow nenalezeno.", 404)
    try:
        return JSONResponse(load_workflow(name))
    except Exception as e:
        return fail(str(e), 404)


async def h_dashboard(request: Request, method: str):
    status = clean_text(request.query_params.get("status"), 40)
    limit = clamp_int(request.query_params.get("limit"), 1, 500, 200)
    detail_id = _int(request.query_params.get("detail_id"))
    uid, adm = viewer_scope(request)
    return JSONResponse(dashboard_payload(status, limit, detail_id, uid, adm))


async def h_jobs(request: Request, method: str):
    status = clean_text(request.query_params.get("status"), 40)
    limit = clamp_int(request.query_params.get("limit"), 1, 500, 200)
    uid, adm = viewer_scope(request)
    return ok({"jobs": db.list_jobs_for_user(uid, adm, status, limit),
               "queue_counts": db.queue_counts(),
               "jobs_ahead": db.jobs_ahead_of_user(uid) if uid else 0,
               "avg_job_seconds": db.average_job_seconds(),
               "eta_seconds": db.queue_eta_seconds(uid) if uid else None})


async def h_job_detail(request: Request, method: str):
    job_id = _int(request.query_params.get("id"))
    if not job_id:
        return fail("ID chybí.")
    job = db.get_job(job_id)
    if not job:
        return fail("Job nenalezen.", 404)
    uid, adm = viewer_scope(request)
    if not may_see_job(job, uid, adm):
        return fail("Tenhle job patří jinému uživateli.", 403)
    return ok({"job": job, "events": db.job_events(job_id)})


async def h_cancel_job(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    body = await _json_body(request)
    job_id = _int(body.get("id"))
    if not job_id:
        return fail("ID chybí.")
    uid, adm = viewer_scope(request)
    if not may_see_job(db.get_job(job_id), uid, adm):
        return fail("Zrušit jde jen vlastní job.", 403)
    done, message = db.request_cancel(job_id)
    if not done:
        return fail(message, 409)
    return ok({"message": message})


async def h_delete_job(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    body = await _json_body(request)
    job_id = _int(body.get("id"))
    if not job_id:
        return fail("ID chybí.")
    job = db.get_job(job_id)
    if not job:
        return fail("Job nenalezen.", 404)
    uid, adm = viewer_scope(request)
    if not may_see_job(job, uid, adm):
        return fail("Smazat jde jen vlastní job.", 403)
    if str(job.get("status")) not in db.FINISHED_STATUSES:
        db.request_cancel(job_id)
    deleted = db.delete_job(job_id)
    return ok({"deleted_files": deleted, "cleaned_uploads": db.cleanup_uploads()})


async def h_clear_finished(request: Request, method: str):
    if method != "POST":
        return fail("Method not allowed", 405)
    uid, adm = viewer_scope(request)
    count, files = db.clear_finished(None if adm else uid)
    return ok({"deleted": count, "deleted_files": files, "cleaned_uploads": db.cleanup_uploads()})


async def h_cleanup_uploads(request: Request, method: str):
    return ok({"cleaned_uploads": db.cleanup_uploads()})


async def h_stats(request: Request, method: str):
    uid, _ = viewer_scope(request)
    return ok({"data": None, "workers": workers_payload(), "queue_counts": db.queue_counts(),
               "jobs_ahead": db.jobs_ahead_of_user(uid) if uid else 0,
               "avg_job_seconds": db.average_job_seconds(),
               "eta_seconds": db.queue_eta_seconds(uid) if uid else None})


async def h_diagnostics(request: Request, method: str):
    checks: List[Dict[str, str]] = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    import sys
    add("Python", "ok", sys.version.split()[0])
    add("ComfyLocal", "ok", f"verze {APP_VERSION}")

    runner = get_runner()
    client = runner.client
    add("Render loop", "ok" if runner.is_alive() else "bad",
        "Běží." if runner.is_alive() else "Neběží — restartuj aplikaci.")

    online = client.online()
    add("ComfyUI", "ok" if online else "bad",
        f"{client.base} odpovídá." if online else
        f"{client.base} neodpovídá — {client.last_error or 'důvod se nepodařilo zjistit'}")
    add("API base", "ok" if online else "warn", client.base)
    add("Soubory (/view)", "ok" if online else "warn", client.files_base)

    from .tls import tls_mode
    mode = tls_mode()
    add("Ověřování TLS", "warn" if ("vypnuté" in mode or "certifi —" in mode) else "ok", mode)

    if not online:
        # Proxy nemusí směrovat všechny cesty stejně, tak ukážeme každou zvlášť
        # i s druhou bází (s/bez /api) — z toho je hned vidět, co přesměrovat.
        for probe in client.probe():
            add(f"sonda {probe['name']}", "ok" if probe["ok"] else "bad",
                f"{probe['url']} → {probe['detail']}")
        alt = ComfyClient(base=client.files_base if client.base != client.files_base
                          else client.base + "/api")
        alt_probe = alt.probe_endpoint("/system_stats")
        add("sonda druhá báze", "ok" if alt_probe["ok"] else "warn",
            f"{alt_probe['url']} → {alt_probe['detail']}")

    if online:
        ws = client.connect_ws("diagnostics")
        if ws is not None:
            add("WebSocket", "ok", "Průběh renderu přijde v reálném čase.")
            try:
                ws.close()
            except Exception:
                pass
        else:
            add("WebSocket", "warn", "WS nejde připojit — průběh se dopočítá pollingem /queue a /history.")
        stats = client.system_stats()
        devices = stats.get("devices") or []
        if devices:
            d = devices[0]
            total = float(d.get("vram_total") or 0) / 1073741824
            free = float(d.get("vram_free") or 0) / 1073741824
            add("GPU", "ok", f"{d.get('name') or 'GPU'} · VRAM {free:.1f}/{total:.1f} GB volné")
        else:
            add("GPU", "warn", "ComfyUI nehlásí žádné zařízení.")
        ckpts = client.combo_options("CheckpointLoaderSimple", "ckpt_name")
        ltx = [c for c in ckpts if "ltx" in c.lower()]
        if ckpts:
            add("Modely v ComfyUI", "ok" if ltx else "warn",
                f"{len(ckpts)} checkpointů, z toho LTX: {', '.join(ltx[:3]) or 'žádný'}")
        else:
            add("Modely v ComfyUI", "warn", "object_info nevrátil seznam checkpointů.")

    for name, path in (("data/uploads", CONFIG.uploads_dir), ("data/outputs", CONFIG.outputs_dir),
                       ("data/tmp", CONFIG.tmp_dir), ("workflows", CONFIG.workflows_dir)):
        exists = path.is_dir()
        writable = exists and _writable(path)
        add(name + "/", "ok" if writable else "bad",
            "Zapisovatelná složka." if writable else
            ("Složka není zapisovatelná." if exists else "Složka neexistuje."))

    try:
        db.connect().execute("SELECT 1")
        add("SQLite", "ok", f"Databáze dostupná ({CONFIG.db_path.name}).")
    except Exception as e:
        add("SQLite", "bad", str(e))

    for info in list_workflows():
        label = info["name"]
        if info.get("error"):
            add(label, "bad", str(info["error"]))
            continue
        add(label, "ok", f"API workflow OK · {info.get('nodes', 0)} nodů · typ {info.get('kind')}")
        try:
            wf = load_workflow(label)
        except Exception:
            continue
        classes = {str(n.get("class_type") or "") for n in wf.values() if isinstance(n, dict)}

        if online:
            # Chybějící soubor modelu/LoRA se pozná odsud; poškozený projde a spadne
            # až při renderu (typicky "shape ... is invalid for input of size ...").
            missing = client.missing_models(wf)
            if missing:
                for m in missing[:4]:
                    add(f"{label}: chybí model", "bad",
                        f"{m['node']}.{m['input']} = {m['value']} — ComfyUI ho nenabízí. "
                        f"Dostupné: {m['available'] or '(nic)'}")
            else:
                add(f"{label}: modely", "ok", "Všechny modely a LoRA ze šablony ComfyUI zná.")

        if not workflow_is_photo_edit(wf):
            has_tokens = "TextGenerateLTX2Prompt" in classes
            add(label + " tokens", "ok" if has_tokens else "warn",
                "TextGenerateLTX2Prompt nalezen." if has_tokens
                else "Token node v šabloně není, funkce se přeskočí.")
            has_enhance = any(
                str(n.get("class_type")) == "PrimitiveBoolean"
                and "enhance" in str((n.get("_meta") or {}).get("title") or "").lower()
                for n in wf.values() if isinstance(n, dict))
            add(label + " Prompt Enhance", "ok" if has_enhance else "warn",
                "Prompt Enhance boolean nalezen." if has_enhance
                else "Prompt Enhance node v šabloně není, funkce se přeskočí.")

    projects = db.list_projects()
    add("Projekty (workflow v DB)", "ok" if projects else "bad",
        f"{len(projects)} aktivních: " + ", ".join(p["name"] for p in projects) if projects
        else "Žádný projekt — chybí soubory ve workflows/.")

    tr = translate_text_online("kočka", "cs", "en")
    add("Překlad promptu CZ→EN", "ok" if tr.get("success") else "warn",
        f"Funguje přes {tr.get('provider')} (kočka → {tr.get('translated')})." if tr.get("success")
        else "Nedostupný (appka nemá výstup do internetu). Prompt se pošle nepřeložený.")

    workers = []
    for wid, wx in workers_payload().items():
        comfy = wx.get("comfy") or {}
        workers.append({"id": wid, "version": str((wx.get("worker") or {}).get("version") or ""),
                        "state": "online" if comfy.get("online") else "offline",
                        "comfy": "ready" if comfy.get("online") else "offline"})
    return ok({"expected_worker_version": APP_VERSION, "checks": checks, "workers": workers})


async def h_worker_unavailable(request: Request, method: str):
    return fail("ComfyLocal žádný worker nemá — ComfyUI běží mimo aplikaci, "
                "takže ho odsud nejde spustit ani restartovat.", 409)


def _writable(path: Path) -> bool:
    probe = path / ".comfylocal_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


HANDLERS = {
    "me": h_me,
    "translate_prompt": h_translate_prompt,
    "create_job": h_create_job,
    "create_jobs_batch": h_create_jobs_batch,
    "rerun_job": h_rerun_job,
    "update_pending_image": h_update_pending_image,
    "update_pending_job": h_update_pending_job,
    "job_file": h_job_file,
    "projects": h_projects,
    "default_workflow": h_default_workflow,
    "project_workflow": h_project_workflow,
    "dashboard": h_dashboard,
    "dashboard_cached": h_dashboard,
    "jobs": h_jobs,
    "job_detail": h_job_detail,
    "cancel_job": h_cancel_job,
    "delete_job": h_delete_job,
    "clear_finished": h_clear_finished,
    "cleanup_uploads": h_cleanup_uploads,
    "stats": h_stats,
    "diagnostics": h_diagnostics,
    "request_comfy_start": h_start_comfy,
    "request_comfyui_start": h_start_comfy,
    "start_comfy": h_start_comfy,
    "request_worker_restart": h_restart_worker,
    "control_status": h_control_status,
    "list_users": h_list_users,
    "save_user": h_save_user,
    "delete_user": h_delete_user,
}
