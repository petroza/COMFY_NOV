# -*- coding: utf-8 -*-
"""Konfigurace ComfyLocal.

Priorita: proměnné prostředí > config.json > výchozí hodnoty.
Žádné tokeny, žádné FTP — jen adresa ComfyUI na síti.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("COMFYLOCAL_CONFIG") or (BASE_DIR / "config.json"))

DEFAULTS: Dict[str, Any] = {
    # ComfyUI na lokální síti (klidně i za reverse proxy). Fragment #... z adresního
    # řádku ComfyUI se sem nepíše — je to jen ID workflow v jeho vlastním UI.
    # Z téhle adresy se servírují soubory (/view).
    "comfy_url": "https://viz-proxy-dev.nova.group/comfy/",
    # Předpona API endpointů vůči comfy_url. Na viz-proxy-dev jsou endpointy pod
    # /comfy/api/ (tj. /comfy/api/prompt, /comfy/api/queue, /comfy/api/ws…).
    # U ComfyUI spuštěného přímo (http://127.0.0.1:8188) funguje i prázdná hodnota,
    # ale "api" je správně v obou případech — moderní ComfyUI má oba prefixy.
    "comfy_api_path": "api",
    # TLS: proxy s certifikátem od interní firemní autority projde v prohlížeči,
    # ale Python má vlastní seznam autorit (certifi) a takový certifikát neuzná.
    # Proto se ve výchozím stavu použije systémové úložiště certifikátů —
    # appka pak věří tomu samému, čemu věří prohlížeč, a ověřování zůstane zapnuté.
    "use_system_trust_store": True,
    # Alternativa: cesta k .pem/.crt s firemní CA (absolutní, nebo vůči složce appky).
    "comfy_ca_bundle": "",
    # Poslední možnost, když nic z výše uvedeného nejde: ověřování vypnout.
    "comfy_verify_tls": True,
    # Volitelné hlavičky pro proxy (např. {"Authorization": "Basic ..."}).
    "comfy_headers": {},
    "comfy_timeout": 60,

    "host": "0.0.0.0",
    "port": 8770,
    "open_browser": True,

    # Střídání uživatelů ve frontě. Když je vypnuté, jede se striktně podle
    # pořadí vytvoření — a dávka 40 obrázků od jednoho člověka pak zablokuje
    # všechny ostatní, dokud nedoběhne celá.
    "fair_queue": True,

    # Volitelný PIN pro přístup z ostatních strojů v síti. Prázdné = bez přihlášení.
    # Když v databázi existuje aspoň jeden aktivní účet, má přednost přihlášení
    # jménem a heslem (Admin → Uživatelé) a PIN se přeskočí.
    "access_pin": "",

    # První správcovský účet. Vyplň jméno a heslo, spusť appku — účet se založí
    # a heslo se odsud hned smaže (v databázi zůstane jen PBKDF2 hash).
    # Heslo sem piš jen v config.json, který je v .gitignore — nikdy ne do kódu.
    "bootstrap_admin": {"username": "", "password": ""},

    # Spuštění ComfyUI z UI (tlačítko „Spustit ComfyUI"). Příkaz se pouští
    # na tomhle PC ve složce comfy_dir. Prázdné = tlačítko jen napoví.
    "comfy_start_cmd": "",
    "comfy_dir": "",

    # Výchozí video šablony. Zdrojové projekty z ComfyUI, ze kterých jsou
    # vyexportované, leží v docs/comfyui_projects/. Photo edit šablony
    # (Flux.2, FireRed) se nabízejí podle obsahu složky workflows/.
    "default_workflow": "ltx23_i2v_template.json",   # 1 PICT / image-to-video
    "flf2v_workflow": "ltx23_flf2v_template.json",   # 2 PICT / první + poslední frejm

    # Náhrada LoRA ve video šablonách, aniž by se editoval JSON. Hodí se, když
    # LoRA ze šablony na serveru chybí nebo je poškozená a chceš zkusit jinou
    # (názvy, které ComfyUI nabízí, ukáže Diagnostika).
    # Prázdné = nechat, co je ve workflow. "off" = LoRA vypnout (strength 0).
    "ltx_lora_override": "",

    # Srovná délku tak, aby audio nebylo delší než obraz. Šablona počítá
    # fps×duration+1 (u 25 fps a 5 s = 126), ale video se dekóduje jen na 121
    # frejmů — audio pak přečnívá o ~0,2 s. Srovnává se dolů na násobek 8 plus
    # jedna, což velikost video latentu nemění, takže obraz vyjde stejně jako dřív.
    "ltx_align_av_length": True,

    # Když render spadne na nesouhlasu tenzorů (šablona nesnese zvolené rozlišení),
    # zkusí se ještě jednou v rozlišení, se kterým je šablona vyexportovaná.
    "ltx_retry_native_resolution": True,

    # Výchozí parametry renderu (uživatel je v UI mění). Kroky výpočtu a cfg
    # řídí jen photo edit — LTX 2.3 šablony mají pevný rozpis sigem a cfg = 1.
    "defaults": {
        "fps": 25,
        "duration": 5,
        "width": 1280,
        "height": 720,
        "steps": 30,
        "cfg": 3.5,
        "motion_strength": 0.75,
        "prompt_enhance": False,
        "enhance_tokens": 512,
        "preset": "Statická kamera (stativ)",
        "style": "None",
    },

    # Automatický překlad promptu CZ → EN před odesláním do ComfyUI (jako na webu).
    # Vyžaduje výstup do internetu; bez něj se prompt pošle nepřeložený.
    "translate_prompt": True,
    "translate_source_lang": "cs",
    "translate_target_lang": "en",
    "translate_timeout": 12,

    # Čím se překládá:
    #   "comfy"  — jazykovým modelem, který už běží ve ComfyUI (Gemma z LTX
    #              šablony). Appka pak nepotřebuje výstup do internetu vůbec.
    #   "online" — původní Google / MyMemory (vyžaduje internet).
    #   "off"    — nepřekládat.
    "translate_backend": "comfy",
    # Když překlad přes ComfyUI nevyjde, smí appka zkusit internet? Výchozí ne,
    # aby „nesahá na internet" platilo doopravdy.
    "translate_allow_internet_fallback": False,
    # Model pro překlad. Prázdné = appka si vybere sama (dá přednost Gemmě).
    "translate_comfy_encoder": "",
    "translate_comfy_checkpoint": "",
    "translate_comfy_max_length": 512,
    # Načtení modelu do paměti může trvat, proto je limit vyšší než u Googlu.
    "translate_comfy_timeout": 180,
    # Instrukce pro model. Prázdné = výchozí šablona pro Gemma 3.
    "translate_comfy_template": "",

    # Hotové joby starší než X hodin se automaticky uklidí (0 = nikdy).
    "purge_finished_after_hours": 0,
    "poll_interval": 2.0,

    # Appka na Windows běží v okně START_WINDOWS.bat, které se po pádu hned
    # zavře — bez souboru by chyba nešla dohledat. Log jde i do konzole,
    # tohle jen navíc ukládá to samé do data/logs/comfylocal.log s rotací.
    "log_to_file": True,
    "log_level": "INFO",
    "log_max_bytes": 5 * 1024 * 1024,
    "log_backup_count": 5,
}

_ENV_MAP = {
    "COMFY_URL": ("comfy_url", str),
    "COMFYLOCAL_HOST": ("host", str),
    "COMFYLOCAL_PORT": ("port", int),
    "COMFYLOCAL_PIN": ("access_pin", str),
    "COMFYLOCAL_VERIFY_TLS": ("comfy_verify_tls", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")),
    "COMFYLOCAL_OPEN_BROWSER": ("open_browser", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")),
}


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, val in (extra or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


class Config:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def as_dict(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def update_and_save(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Uloží změny do config.json a promítne je do běžící aplikace.

        Používá to stránka Setup, aby se dala přepsat adresa ComfyUI bez restartu
        a bez ručního editování souboru.
        """
        self._data = _deep_merge(self._data, patch or {})
        stored: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    stored = loaded
            except Exception:
                stored = {}
        stored = _deep_merge(stored, patch or {})
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(CONFIG_PATH)
        return self.as_dict()

    # ── cesty ────────────────────────────────────────────────
    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    @property
    def data_dir(self) -> Path:
        return self._ensure(BASE_DIR / "data")

    @property
    def uploads_dir(self) -> Path:
        return self._ensure(self.data_dir / "uploads")

    @property
    def outputs_dir(self) -> Path:
        return self._ensure(self.data_dir / "outputs")

    @property
    def tmp_dir(self) -> Path:
        return self._ensure(self.data_dir / "tmp")

    @property
    def logs_dir(self) -> Path:
        return self._ensure(self.data_dir / "logs")

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "comfylocal.log"

    @property
    def workflows_dir(self) -> Path:
        return self._ensure(BASE_DIR / "workflows")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "comfylocal.sqlite"

    @staticmethod
    def _ensure(p: Path) -> Path:
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ── ComfyUI ─────────────────────────────────────────────
    @property
    def comfy_base(self) -> str:
        """Adresa ComfyUI (odsud se servírují soubory) bez lomítka a bez fragmentu."""
        raw = str(self.get("comfy_url") or "").strip()
        raw = raw.split("#", 1)[0].split("?", 1)[0]
        return raw.rstrip("/")

    @property
    def comfy_api_base(self) -> str:
        """Základ API endpointů, např. https://viz-proxy-dev.nova.group/comfy/api"""
        suffix = str(self.get("comfy_api_path") or "").strip().strip("/")
        return f"{self.comfy_base}/{suffix}" if suffix else self.comfy_base

    @staticmethod
    def _to_ws(url: str) -> str:
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url

    @property
    def comfy_ws_url(self) -> str:
        return self._to_ws(self.comfy_api_base) + "/ws"

    @property
    def comfy_ws_fallback_url(self) -> str:
        """Když proxy nepustí /api/ws, zkusíme /ws přímo pod comfy_url."""
        return self._to_ws(self.comfy_base) + "/ws"

    @property
    def comfy_host(self) -> str:
        return urlparse(self.comfy_base).netloc or self.comfy_base


def load_config() -> Config:
    data = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            file_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(file_data, dict):
                data = _deep_merge(data, file_data)
        except Exception as e:  # pragma: no cover - konfigurace se čte jen při startu
            raise SystemExit(f"config.json se nepodařilo přečíst: {e}")
    for env_key, (cfg_key, cast) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            data[cfg_key] = cast(raw)
        except Exception:
            raise SystemExit(f"Neplatná hodnota {env_key}={raw!r}")
    return Config(data)


CONFIG = load_config()
