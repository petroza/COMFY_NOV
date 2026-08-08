# -*- coding: utf-8 -*-
"""HTTP server ComfyLocal — servíruje UI, statiku a API kompatibilní s api.php."""
from __future__ import annotations

import logging

from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, projects as projects_mod, users as users_mod
from .compat import (APP_VERSION, PIN_COOKIE, authenticated, is_admin, login_required,
                     pin_required, router as api_router, users_enabled)
from .config import CONFIG
from .logging_setup import configure_logging
from .runner import get_runner, start_runner
from .tls import setup_tls, tls_mode

log = logging.getLogger("comfylocal.server")

WEB_DIR = CONFIG.base_dir / "web"

app = FastAPI(title="ComfyLocal", docs_url=None, redoc_url=None)
app.include_router(api_router)

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#0e1116"/>'
    '<path d="M12 9l12 7-12 7z" fill="#4f9dff"/></svg>'
)


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(FAVICON, media_type="image/svg+xml")


@app.get("/")
async def index(request: Request) -> Response:
    """Jedna stránka: buď PIN, nebo celé UI (stejné jako app.php)."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    locked = not authenticated(request)
    html = html.replace("{{PIN_GATE_STYLE}}", "display:grid" if locked else "display:none")
    html = html.replace("{{APP_STYLE}}", "display:none" if locked else "")
    html = html.replace("{{PIN_REQUIRED}}", "true" if login_required() else "false")
    with_users = users_enabled()
    html = html.replace("{{USER_FIELD_STYLE}}", "" if with_users else "display:none")
    html = html.replace("{{LOGIN_SECRET_LABEL}}", "Heslo" if with_users else "PIN")
    html = html.replace("{{ADMIN_BTN_STYLE}}", "" if is_admin(request) else "display:none")
    html = html.replace("{{APP_VERSION}}", APP_VERSION)
    html = html.replace("{{COMFY_URL}}", CONFIG.comfy_base)
    return HTMLResponse(html)


@app.get("/app.php")
async def app_php_alias() -> Response:
    """Původní web běžel na app.php — ať funguje i starý bookmark."""
    return RedirectResponse("/")


@app.get("/admin")
async def admin_page(request: Request) -> Response:
    """Admin panel: účty, ovládání ComfyUI a přehled projektů."""
    if not authenticated(request):
        return RedirectResponse("/")
    if not is_admin(request):
        return HTMLResponse("<h1>403</h1><p>Admin panel je jen pro správce.</p>", status_code=403)
    html = (WEB_DIR / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("{{APP_VERSION}}", APP_VERSION))


@app.get("/setup")
async def setup_page(request: Request) -> Response:
    if not authenticated(request):
        return RedirectResponse("/")
    html = (WEB_DIR / "setup.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("{{APP_VERSION}}", APP_VERSION))


@app.get("/api/setup")
async def setup_data(request: Request):
    """Data pro stránku Setup — konfigurace, workflow, stav ComfyUI."""
    if not authenticated(request):
        return {"success": False, "error": "Nepřihlášeno."}
    from .workflow import list_workflows
    runner = get_runner()
    config = CONFIG.as_dict()
    config.pop("access_pin", None)
    config["comfy_headers"] = {k: "•••" for k in (config.get("comfy_headers") or {})}
    return {
        "success": True,
        "version": APP_VERSION,
        "comfy": {
            "url": CONFIG.comfy_base,
            "api_base": CONFIG.comfy_api_base,
            "ws_url": CONFIG.comfy_ws_url,
            "online": runner.client.online(),
            "resolved_files_base": runner.client.files_base,
            "resolved_api_base": runner.client.base,
        },
        "paths": {
            "config": "config.json",
            "data": str(CONFIG.data_dir),
            "uploads": str(CONFIG.uploads_dir),
            "outputs": str(CONFIG.outputs_dir),
            "workflows": str(CONFIG.workflows_dir),
            "database": str(CONFIG.db_path),
            "log": str(CONFIG.log_file),
        },
        "log": _log_info(),
        "pin_required": pin_required(),
        "config": config,
        "workflows": list_workflows(),
        "projects": db.list_projects(),
        "queue": db.queue_counts(),
    }


@app.post("/api/setup")
async def setup_save(request: Request):
    """Uloží adresu ComfyUI (a související volby) do config.json a použije ji hned."""
    if not authenticated(request):
        return {"success": False, "error": "Nepřihlášeno."}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return {"success": False, "error": "Neplatná data."}

    patch = {}
    url = str(body.get("comfy_url") or "").strip()
    if url:
        # Fragment (#workflow-id) ani query do adresy nepatří, uřízneme je hned.
        url = url.split("#", 1)[0].split("?", 1)[0].strip()
        if not url.startswith(("http://", "https://")):
            return {"success": False, "error": "Adresa musí začínat http:// nebo https://"}
        patch["comfy_url"] = url.rstrip("/") + "/"

    if "comfy_api_path" in body:
        patch["comfy_api_path"] = str(body.get("comfy_api_path") or "").strip().strip("/")
    if "comfy_verify_tls" in body:
        patch["comfy_verify_tls"] = bool(body.get("comfy_verify_tls"))
    if "comfy_timeout" in body:
        try:
            patch["comfy_timeout"] = max(5, min(600, int(float(body["comfy_timeout"]))))
        except (TypeError, ValueError):
            return {"success": False, "error": "Timeout musí být číslo."}
    if "translate_prompt" in body:
        patch["translate_prompt"] = bool(body.get("translate_prompt"))
    if not patch:
        return {"success": False, "error": "Není co uložit."}

    CONFIG.update_and_save(patch)
    client = get_runner().reload_client()
    online = client.online()
    return {
        "success": True,
        "saved": patch,
        "comfy": {
            "url": CONFIG.comfy_base,
            "api_base": CONFIG.comfy_api_base,
            "ws_url": CONFIG.comfy_ws_url,
            "online": online,
        },
        "message": ("Uloženo, ComfyUI odpovídá." if online else
                    "Uloženo, ale ComfyUI na téhle adrese neodpovídá."),
    }


@app.post("/api/setup/test")
async def setup_test(request: Request):
    """Vyzkouší zadanou adresu, aniž by se cokoliv ukládalo."""
    if not authenticated(request):
        return {"success": False, "error": "Nepřihlášeno."}
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = str((body or {}).get("comfy_url") or "").strip().split("#", 1)[0].split("?", 1)[0]
    api_path = str((body or {}).get("comfy_api_path") or "").strip().strip("/")
    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "Adresa musí začínat http:// nebo https://"}

    from .comfy_client import ComfyClient
    base = url.rstrip("/")
    probe = ComfyClient(base=f"{base}/{api_path}" if api_path else base)
    online = probe.online()
    result: Dict[str, Any] = {
        "success": online,
        "api_base": probe.base,
        "files_base": base,
        "checks": [],
    }
    # Když to neprojde, ukážeme každou sondu i s důvodem — offline bez vysvětlení
    # se nedá ladit. Proxy navíc nemusí směrovat všechny cesty stejně.
    for one in (probe.probe() if not online else [probe.probe_endpoint("/system_stats")]):
        result["checks"].append({
            "name": one.get("name") or "/system_stats",
            "ok": one["ok"],
            "detail": one["detail"] if one["ok"] else f"{one['url']} → {one['detail']}",
        })
    if not online:
        alt_base = f"{base}/api" if not api_path else base
        alt = ComfyClient(base=alt_base)
        alt_probe = alt.probe_endpoint("/system_stats")
        result["checks"].append({
            "name": "druhá báze (s/bez /api)", "ok": alt_probe["ok"],
            "detail": f"{alt_probe['url']} → {alt_probe['detail']}",
        })
        if alt_probe["ok"]:
            result["error"] = (f"Zadaná báze neodpovídá, ale {alt_base} ano — "
                               f"uprav předponu API a ulož.")
    if online:
        stats = probe.system_stats()
        devices = stats.get("devices") or []
        result["checks"].append({
            "name": "GPU", "ok": bool(devices),
            "detail": str((devices[0] or {}).get("name") or "?") if devices else "ComfyUI nehlásí zařízení",
        })
        ckpts = probe.combo_options("CheckpointLoaderSimple", "ckpt_name")
        result["checks"].append({"name": "modely (object_info)", "ok": bool(ckpts),
                                 "detail": f"{len(ckpts)} checkpointů" if ckpts else "seznam nepřišel"})
        ws = probe.connect_ws("setup-test")
        result["checks"].append({"name": "WebSocket", "ok": ws is not None,
                                 "detail": "připojeno" if ws is not None
                                 else "nejde připojit (průběh se dopočítá pollingem)"})
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    elif not result.get("error"):
        result["error"] = f"{probe.base} neodpovídá — {probe.last_error or 'důvod neznámý'}"
    return result


def _log_info() -> Dict[str, Any]:
    path = CONFIG.log_file
    exists = path.is_file()
    return {
        "enabled": bool(CONFIG.get("log_to_file", True)),
        "path": str(path),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else 0,
        "level": str(CONFIG.get("log_level") or "INFO").upper(),
    }


@app.get("/api/log")
async def log_tail(request: Request, lines: int = 400):
    """Konec logu do stránky Setup, ať se chyba dá přečíst bez hledání souboru."""
    if not authenticated(request):
        return PlainTextResponse("Nepřihlášeno.", status_code=401)
    path = CONFIG.log_file
    if not path.is_file():
        return PlainTextResponse(
            "Log soubor zatím neexistuje.\n"
            f"Očekávaná cesta: {path}\n"
            + ("" if CONFIG.get("log_to_file", True)
               else "Zapisování do souboru je vypnuté (log_to_file: false v config.json)."))
    lines = max(10, min(5000, int(lines)))
    # Log rotuje na 5 MB, takže se vejde do paměti celý; bereme jen konec.
    text = path.read_text(encoding="utf-8", errors="replace")
    return PlainTextResponse("".join(text.splitlines(keepends=True)[-lines:]))


@app.get("/api/log/download")
async def log_download(request: Request):
    if not authenticated(request):
        return PlainTextResponse("Nepřihlášeno.", status_code=401)
    path = CONFIG.log_file
    if not path.is_file():
        return PlainTextResponse("Log soubor zatím neexistuje.", status_code=404)
    return FileResponse(path, filename="comfylocal.log", media_type="text/plain")


@app.post("/logout")
async def logout(request: Request) -> Response:
    users_mod.logout(request.cookies.get(users_mod.SESSION_COOKIE) or "")
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(PIN_COOKIE)
    resp.delete_cookie(users_mod.SESSION_COOKIE)
    return resp


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.on_event("startup")
async def on_startup() -> None:
    # I když server nastartuje jinak než přes `python -m comfylocal`
    # (uvicorn, gunicorn, kontejner), logování i systémové úložiště
    # certifikátů musí být zapnuté.
    configure_logging()
    setup_tls()
    db.init()
    users_mod.ensure_schema()
    projects_mod.sync_projects()
    start_runner()
    log.info("ComfyLocal %s běží — ComfyUI: %s (API %s)",
             APP_VERSION, CONFIG.comfy_base, CONFIG.comfy_api_base)
