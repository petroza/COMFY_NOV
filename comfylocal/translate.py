# -*- coding: utf-8 -*-
"""Překlad promptu CZ → EN. Port translate_text_online() z api.php.

Zkouší Google GTX, Google clients5 a MyMemory. Když appka nemá výstup do
internetu (což v interní síti klidně může být), překlad tiše selže a
odešle se prompt tak, jak ho uživatel napsal.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from .config import CONFIG

log = logging.getLogger("comfylocal.translate")

USER_AGENT = "ComfyLocal/1.0"


def _http_get_text(url: str, timeout: float) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if 200 <= r.status_code < 300:
            return r.text
    except Exception as e:
        log.debug("Překladač %s selhal: %s", url.split("?", 1)[0], e)
    return None


def _clean_lang(code: str, fallback: str) -> str:
    code = re.sub(r"[^a-zA-Z\-]", "", str(code or ""))
    return code or fallback


def translate_text(text: str, source: str = "cs", target: str = "en") -> Dict[str, object]:
    """Přeloží text podle `translate_backend` z config.json.

    - `comfy` (výchozí) — jazykovým modelem, který už běží ve ComfyUI. Appka
      pak nepotřebuje výstup do internetu vůbec.
    - `online` — původní cesta přes Google / MyMemory.
    - `off` — nepřekládat.
    """
    text = str(text or "").strip()
    if not text:
        return {"success": True, "translated": "", "provider": "none"}
    if not bool(CONFIG.get("translate_prompt", True)):
        return {"success": False, "translated": "", "provider": "disabled",
                "error": "Překlad je v config.json vypnutý."}

    backend = str(CONFIG.get("translate_backend") or "comfy").strip().lower()
    if backend in ("off", "none", "disabled"):
        return {"success": False, "translated": "", "provider": "disabled",
                "error": "Překlad je vypnutý (translate_backend=off)."}

    if backend != "online":
        from .translate_comfy import translate_via_comfy
        try:
            return translate_via_comfy(text, _clean_lang(source, "cs"), _clean_lang(target, "en"))
        except Exception as e:
            log.warning("Překlad přes ComfyUI nevyšel: %s", e)
            if not bool(CONFIG.get("translate_allow_internet_fallback", False)):
                # Výchozí stav: appka nechodí mimo lokální síť ani na záskok.
                return {"success": False, "translated": "", "provider": "comfy",
                        "error": f"Překlad přes ComfyUI nevyšel: {e}"}
            log.info("Zkouším záložní překlad po internetu (translate_allow_internet_fallback=true).")

    return translate_text_online(text, source, target)


def translate_text_online(text: str, source: str = "cs", target: str = "en") -> Dict[str, object]:
    """Překlad po internetu: Google GTX → Google clients5 → MyMemory."""
    text = str(text or "").strip()
    if not text:
        return {"success": True, "translated": "", "provider": "none"}

    source = _clean_lang(source, "auto")
    target = _clean_lang(target, "en")
    timeout = float(CONFIG.get("translate_timeout") or 12)
    tried: List[str] = []

    # 1) Google GTX
    tried.append("google_gtx")
    raw = _http_get_text(
        "https://translate.googleapis.com/translate_a/single?client=gtx"
        f"&sl={quote(source)}&tl={quote(target)}&dt=t&q={quote(text)}", timeout)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and isinstance(data[0], list):
                translated = "".join(str(part[0]) for part in data[0] if part and part[0]).strip()
                if translated:
                    return {"success": True, "translated": translated,
                            "provider": "google_gtx", "providers_tried": tried}
        except Exception:
            pass

    # 2) Google clients5
    tried.append("google_clients5")
    raw = _http_get_text(
        "https://clients5.google.com/translate_a/t?client=dict-chrome-ex"
        f"&sl={quote(source)}&tl={quote(target)}&q={quote(text)}", timeout)
    if raw:
        try:
            data = json.loads(raw)
            translated = ""
            if isinstance(data, dict):
                sentences = data.get("sentences") or []
                if sentences and isinstance(sentences[0], dict):
                    translated = str(sentences[0].get("trans") or "").strip()
            elif isinstance(data, list) and data:
                first = data[0]
                translated = str(first[0] if isinstance(first, list) and first else first).strip()
            if translated:
                return {"success": True, "translated": translated,
                        "provider": "google_clients5", "providers_tried": tried}
        except Exception:
            pass

    # 3) MyMemory
    tried.append("mymemory")
    raw = _http_get_text(
        f"https://api.mymemory.translated.net/get?q={quote(text)}"
        f"&langpair={quote(source + '|' + target)}", timeout)
    if raw:
        try:
            data = json.loads(raw)
            translated = str(((data or {}).get("responseData") or {}).get("translatedText") or "").strip()
            if translated:
                return {"success": True, "translated": translated,
                        "provider": "mymemory", "providers_tried": tried}
        except Exception:
            pass

    return {"success": False, "translated": "", "provider": "none", "providers_tried": tried,
            "error": "Překlad se nepovedl (Google GTX, Google fallback ani MyMemory neodpověděly). "
                     "Prompt se odešle v původním jazyce."}
