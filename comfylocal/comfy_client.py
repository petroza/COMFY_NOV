# -*- coding: utf-8 -*-
"""HTTP/WS klient pro ComfyUI běžící na lokální síti (klidně za reverse proxy).

Proti původnímu workeru tady není nic o FTP ani o vzdáleném API — appka mluví
přímo na ComfyUI a výstupy si stahuje přes /view do vlastní složky.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None

from .config import CONFIG

log = logging.getLogger("comfylocal.comfy")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".gif"}


class ComfyError(RuntimeError):
    pass


_TLS_WARNED = False


def _resolve_verify() -> Any:
    """Co předat requests jako `verify`.

    Firemní proxy mívá certifikát od interní autority, který systém nezná.
    Čistší než ověřování vypnout je dát cestu k CA balíčku (comfy_ca_bundle);
    pak spojení zůstane ověřené. Vypnutí je fallback a hlásí se do logu.
    """
    global _TLS_WARNED
    bundle = str(CONFIG.get("comfy_ca_bundle") or "").strip()
    if bundle:
        path = Path(bundle)
        if not path.is_absolute():
            path = CONFIG.base_dir / path
        if path.is_file():
            return str(path)
        log.warning("comfy_ca_bundle %s neexistuje — použiju běžné ověřování.", bundle)

    if bool(CONFIG.get("comfy_verify_tls", True)):
        return True

    if not _TLS_WARNED:
        _TLS_WARNED = True
        log.warning("Ověřování TLS certifikátu je vypnuté — spojení na ComfyUI není "
                    "chráněné proti podvržení. Čistší je comfy_ca_bundle s firemní CA.")
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    return False


class ComfyClient:
    """Endpointy jdou na API base (na viz-proxy-dev je to /comfy/api),
    soubory se stahují z file base (/comfy). Když jedna varianta vrátí 404,
    klient zkusí druhou — funguje tedy i pro ComfyUI spuštěné přímo na PC."""

    def __init__(self, base: Optional[str] = None) -> None:
        self.files_base = (base or CONFIG.comfy_base).rstrip("/")
        self.base = CONFIG.comfy_api_base.rstrip("/") if base is None else self.files_base
        self.timeout = float(CONFIG.get("comfy_timeout") or 60)
        self.verify = _resolve_verify()
        self.headers = dict(CONFIG.get("comfy_headers") or {})
        self._object_info: Optional[dict] = None
        # path -> báze, která na něj odpověděla (zjištěno při prvním 404 fallbacku)
        self._path_base: Dict[str, str] = {}
        # důvod, proč naposledy nešlo na ComfyUI (pro diagnostiku v UI)
        self.last_error: Optional[str] = None

    # ── low level ───────────────────────────────────────────
    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def alt_base(self) -> str:
        """Druhá varianta báze: buď bez /api, nebo s /api."""
        if self.base != self.files_base:
            return self.files_base
        return self.base + "/api"

    def alt_url(self, path: str) -> str:
        return f"{self.alt_base()}/{path.lstrip('/')}"

    def endpoint(self, path: str) -> str:
        """Vrátí URL endpointu; jednou zjištěná funkční báze se pamatuje."""
        key = path.split("?", 1)[0].rstrip("/")
        remembered = self._path_base.get(key)
        if remembered:
            return f"{remembered}/{path.lstrip('/')}"
        return self.url(path)

    def _remember(self, path: str, base: str) -> None:
        self._path_base[path.split("?", 1)[0].rstrip("/")] = base

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        r = requests.request(method, self.endpoint(path), headers=self.headers, verify=self.verify, **kw)
        if r.status_code in (404, 405) and "files" not in kw:
            alt_base = self.alt_base()
            alt_r = requests.request(method, self.alt_url(path), headers=self.headers,
                                     verify=self.verify, **kw)
            if alt_r.status_code < 400:
                self._remember(path, alt_base)
                log.info("Endpoint %s odpovídá na %s — používám to dál.", path, alt_base)
                return alt_r
        return r

    def _get(self, path: str, **kw) -> requests.Response:
        return self._request("GET", path, **kw)

    def _post(self, path: str, **kw) -> requests.Response:
        return self._request("POST", path, **kw)

    # ── stav ────────────────────────────────────────────────
    def online(self) -> bool:
        """True jen když endpoint opravdu vrátí ComfyUI JSON.

        Reverse proxy umí na nesměrovanou cestu vrátit 200 s přihlašovací
        stránkou nebo SPA indexem — to není ComfyUI a job by na tom spadl.
        Důvod posledního selhání zůstane v self.last_error pro diagnostiku.
        """
        result = self.probe_endpoint("/system_stats")
        self.last_error = None if result["ok"] else result["detail"]
        return bool(result["ok"])

    def probe_endpoint(self, path: str, timeout: float = 8) -> Dict[str, Any]:
        """Jeden ověřovací dotaz s čitelným důvodem, proč (ne)prošel."""
        url = self.endpoint(path)
        try:
            r = requests.get(url, headers=self.headers, verify=self.verify,
                             timeout=timeout, allow_redirects=False)
        except requests.exceptions.SSLError as e:
            return {"ok": False, "url": url, "status": None,
                    "detail": f"TLS certifikát neprošel ověřením: {str(e)[:200]}. "
                              f"U interního certifikátu vypni „Ověřovat TLS certifikát“ v Setupu."}
        except requests.exceptions.ConnectTimeout:
            return {"ok": False, "url": url, "status": None,
                    "detail": "Spojení se nepodařilo navázat (timeout). Zkontroluj síť/VPN."}
        except requests.exceptions.ProxyError as e:
            return {"ok": False, "url": url, "status": None,
                    "detail": f"Spojení jde přes HTTP proxy a ta ho odmítla: {str(e)[:170]}. "
                              f"Pro adresu v interní síti nastav výjimku v NO_PROXY."}
        except requests.exceptions.ConnectionError as e:
            reason = str(e)
            low = reason.lower()
            dns_markers = ("failed to resolve", "name or service not known", "getaddrinfo",
                           "nodename nor servname", "name resolution", "no address associated")
            if any(m in low for m in dns_markers):
                hint = "Jméno serveru se nepřeložilo (DNS) — jsi na firemní síti / VPN?"
            elif "refused" in low:
                hint = "Spojení odmítnuto — na téhle adrese a portu nic neposlouchá."
            elif "timed out" in low or "timeout" in low:
                hint = "Spojení vypršelo — adresa je nedosažitelná (firewall, jiná síť)."
            else:
                hint = "Spojení se nepovedlo — běží ComfyUI a je adresa správná?"
            return {"ok": False, "url": url, "status": None, "detail": f"{hint} ({reason[:150]})"}
        except Exception as e:
            return {"ok": False, "url": url, "status": None, "detail": f"{type(e).__name__}: {str(e)[:200]}"}

        status = r.status_code
        location = r.headers.get("Location") or ""
        ctype = (r.headers.get("Content-Type") or "").lower()
        body = (r.text or "")[:300].strip()

        if status in (301, 302, 303, 307, 308):
            return {"ok": False, "url": url, "status": status,
                    "detail": f"Proxy odpovídá přesměrováním {status} na {location or '?'} — "
                              f"nejspíš přihlašovací stránka nebo špatná cesta."}
        if status in (401, 403):
            return {"ok": False, "url": url, "status": status,
                    "detail": f"HTTP {status} — proxy chce autentizaci. Přihlašovací hlavičku "
                              f"(cookie / Basic auth) lze doplnit do comfy_headers v config.json."}
        if status == 404:
            return {"ok": False, "url": url, "status": status,
                    "detail": "HTTP 404 — proxy tuhle cestu nesměruje. Zkontroluj adresu a předponu API."}
        if status >= 400:
            return {"ok": False, "url": url, "status": status,
                    "detail": f"HTTP {status}: {body[:160] or '(prázdná odpověď)'}"}

        if "json" not in ctype:
            preview = body[:120].replace("\n", " ")
            return {"ok": False, "url": url, "status": status,
                    "detail": f"HTTP {status}, ale odpověď není JSON (Content-Type {ctype or '?'}). "
                              f"Takhle vypadá přihlašovací stránka nebo UI, ne ComfyUI API. "
                              f"Začátek: {preview!r}"}
        try:
            data = r.json()
        except Exception:
            return {"ok": False, "url": url, "status": status,
                    "detail": "Odpověď se tváří jako JSON, ale nejde přečíst."}
        return {"ok": True, "url": url, "status": status, "detail": "odpovídá JSONem", "data": data}

    def probe(self) -> List[Dict[str, Any]]:
        """Proklepne víc endpointů — proxy nemusí směrovat všechny stejně."""
        return [{"name": path, **self.probe_endpoint(path)}
                for path in ("/system_stats", "/queue", "/object_info")]

    def system_stats(self) -> dict:
        try:
            r = self._get("/system_stats", timeout=10)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.debug("system_stats selhalo: %s", e)
            return {}

    def queue(self) -> dict:
        try:
            r = self._get("/queue", timeout=15)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def prompt_in_queue(self, prompt_id: str, queue_data: Optional[dict] = None) -> tuple:
        """(running, pending, pending_count) — stejná logika jako v původním workeru."""
        q = queue_data if queue_data is not None else self.queue()
        running = False
        pending = False
        pending_count = 0

        def _pid(item: Any) -> str:
            if isinstance(item, (list, tuple)):
                return str(item[1] if len(item) > 1 else item[0])
            if isinstance(item, dict):
                return str(item.get("prompt_id") or item.get("id") or "")
            return ""

        try:
            for item in q.get("queue_running", []) or []:
                if _pid(item) == prompt_id:
                    running = True
            for item in q.get("queue_pending", []) or []:
                pending_count += 1
                if _pid(item) == prompt_id:
                    pending = True
        except Exception:
            pass
        return running, pending, pending_count

    # ── vstupní obrázky ─────────────────────────────────────
    def upload_image(self, path: Path) -> str:
        def _try(url: str) -> requests.Response:
            # Soubor se otevírá znovu pro každý pokus — použitý stream už nejde poslat.
            with path.open("rb") as f:
                return requests.post(
                    url,
                    files={"image": (path.name, f, "application/octet-stream")},
                    data={"overwrite": "true", "type": "input"},
                    headers=self.headers, verify=self.verify, timeout=300,
                )

        r = _try(self.endpoint("/upload/image"))
        if r.status_code in (404, 405):
            alt_base = self.alt_base()
            alt_r = _try(f"{alt_base}/upload/image")
            if alt_r.status_code < 400:
                self._remember("/upload/image", alt_base)
                r = alt_r
        if r.status_code >= 400:
            raise ComfyError(f"ComfyUI /upload/image {r.status_code}: {r.text[:500]}")
        try:
            return r.json().get("name") or path.name
        except Exception:
            return path.name

    # ── object_info / model autofix ─────────────────────────
    def object_info(self) -> dict:
        if self._object_info is not None:
            return self._object_info
        try:
            r = self._get("/object_info", timeout=30)
            r.raise_for_status()
            data = r.json()
            self._object_info = data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning("object_info nejde načíst, model autofix bude omezený: %s", e)
            self._object_info = {}
        return self._object_info

    def forget_object_info(self) -> None:
        self._object_info = None

    def combo_options(self, class_type: str, input_name: str) -> List[str]:
        try:
            info = self.object_info().get(class_type) or {}
            required = ((info.get("input") or {}).get("required") or {})
            optional = ((info.get("input") or {}).get("optional") or {})
            cfg = required.get(input_name, optional.get(input_name))
            if isinstance(cfg, (list, tuple)) and cfg:
                first = cfg[0]
                if isinstance(first, list):
                    return [str(x) for x in first]
            if isinstance(cfg, dict) and isinstance(cfg.get("options"), list):
                return [str(x) for x in cfg["options"]]
        except Exception:
            pass
        return []

    # Vstupy, které odkazují na soubory modelů — u nich se dá ověřit, že je ComfyUI zná.
    MODEL_INPUTS = ("ckpt_name", "lora_name", "text_encoder", "vae_name", "unet_name",
                    "clip_name", "clip_name1", "clip_name2", "model_name", "control_net_name",
                    "style_model_name", "upscale_model_name")

    def missing_models(self, workflow: dict) -> List[Dict[str, str]]:
        """Které modely/LoRA ze workflow ComfyUI vůbec nenabízí.

        Odliší „soubor chybí“ od „soubor je poškozený“ — poškozený projde tímhle
        testem a spadne až při renderu.
        """
        missing: List[Dict[str, str]] = []
        for node_id, node in (workflow or {}).items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            cls = str(node.get("class_type") or "")
            for key in self.MODEL_INPUTS:
                value = inputs.get(key)
                if not isinstance(value, str) or not value:
                    continue
                options = self.combo_options(cls, key)
                if not options:
                    continue  # ComfyUI ten node nezná, nebo nevrátil seznam — netvrdíme nic
                if not any(o.lower() == value.lower() for o in options):
                    missing.append({"node": f"{node_id}:{cls}", "input": key, "value": value,
                                    "available": ", ".join(options[:6])})
        return missing

    def resolve_combo_value(self, class_type: str, input_name: str, current: str,
                            exact_preferred: List[str], wildcard_preferred: List[str],
                            throw_if_no_match: bool = False) -> str:
        options = self.combo_options(class_type, input_name)
        if not options:
            return current
        for o in options:
            if o.lower() == str(current).lower():
                return o
        for pref in exact_preferred:
            for o in options:
                if o.lower() == str(pref).lower():
                    return o
        for pat in wildcard_preferred:
            for o in options:
                if fnmatch.fnmatchcase(o.lower(), pat.lower()):
                    return o
        if throw_if_no_match:
            raise ComfyError(
                f"Nenalezen vhodný model pro {class_type}.{input_name}. "
                f"Ve workflow je {current!r}, ale ComfyUI ho nemá. Dostupné: {', '.join(options[:30])}"
            )
        return current

    # ── render ──────────────────────────────────────────────
    def submit(self, workflow: dict, client_id: str) -> str:
        r = self._post("/prompt", json={"prompt": workflow, "client_id": client_id}, timeout=120)
        if r.status_code >= 400:
            raise ComfyError(f"ComfyUI /prompt chyba {r.status_code}: {r.text[:2000]}")
        data = r.json()
        pid = data.get("prompt_id")
        if not pid:
            raise ComfyError(f"ComfyUI nevrátil prompt_id: {data}")
        return pid

    def interrupt(self) -> None:
        try:
            self._post("/interrupt", timeout=10)
        except Exception:
            pass

    def history(self, prompt_id: str, allow_empty: bool = False) -> Optional[dict]:
        r = self._get(f"/history/{prompt_id}", timeout=60)
        r.raise_for_status()
        data = r.json()
        if prompt_id in data:
            return data[prompt_id]
        if allow_empty:
            return None
        raise ComfyError(f"History neobsahuje prompt_id {prompt_id}")

    def connect_ws(self, client_id: str):
        """Zkusí /api/ws, pak /ws. Když neprojde ani jedno (proxy bez WS),
        vrátí None a průběh se sleduje pollingem /queue a /history."""
        if websocket is None:
            log.warning("websocket-client není nainstalovaný — průběh jen pollingem.")
            return None
        sslopt = None
        if not self.verify:
            import ssl
            sslopt = {"cert_reqs": ssl.CERT_NONE}
        query = "?" + urlencode({"clientId": client_id})
        candidates = [CONFIG.comfy_ws_url + query, CONFIG.comfy_ws_fallback_url + query]
        for ws_url in dict.fromkeys(candidates):
            try:
                ws = websocket.create_connection(
                    ws_url,
                    timeout=8,
                    header=[f"{k}: {v}" for k, v in self.headers.items()] or None,
                    sslopt=sslopt,
                )
                ws.settimeout(1)
                log.info("WebSocket připojen: %s", ws_url.split("?", 1)[0])
                return ws
            except Exception as e:
                log.warning("WebSocket %s se nepřipojil (%s).", ws_url.split("?", 1)[0], e)
        log.warning("Žádný WebSocket — průběh sleduji pollingem.")
        return None

    def download_output(self, item: dict, dst_dir: Path) -> Path:
        params = {
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        }
        dst = dst_dir / Path(item["filename"]).name
        query = "?" + urlencode(params)
        # Soubory servíruje jak /comfy/view, tak /comfy/api/view — zkusíme obě.
        urls = [self.endpoint("/view") + query, self.alt_url("/view") + query]
        last_error: Optional[Exception] = None
        for url in dict.fromkeys(urls):
            try:
                with requests.get(url, headers=self.headers, verify=self.verify,
                                  stream=True, timeout=900) as r:
                    r.raise_for_status()
                    with dst.open("wb") as f:
                        for chunk in r.iter_content(1024 * 1024):
                            if chunk:
                                f.write(chunk)
                if url.startswith(self.alt_base()):
                    self._remember("/view", self.alt_base())
                return dst
            except Exception as e:
                last_error = e
                log.warning("Stažení výstupu z %s selhalo: %s", url.split("?", 1)[0], e)
        raise ComfyError(f"Výstup {item['filename']} se nepodařilo stáhnout: {last_error}")


# ── historie: chyby a výstupy ───────────────────────────────
def _short(v: Any, limit: int = 1200) -> str:
    txt = str(v or "")
    return txt if len(txt) <= limit else txt[:limit] + "…"


def extract_history_error(history: dict) -> str:
    status = history.get("status") if isinstance(history, dict) else None
    if not isinstance(status, dict):
        return ""
    status_str = str(status.get("status_str") or "").lower()
    completed = status.get("completed")
    messages = status.get("messages") or []
    parts: List[str] = []
    for msg in reversed(messages if isinstance(messages, list) else []):
        typ = ""
        data: Any = None
        if isinstance(msg, (list, tuple)) and len(msg) >= 2:
            typ, data = str(msg[0] or ""), msg[1]
        elif isinstance(msg, dict):
            typ, data = str(msg.get("type") or ""), msg.get("data") or msg
        if typ not in ("execution_error", "execution_interrupted", "error"):
            continue
        if isinstance(data, dict):
            node = data.get("node_id") or data.get("node")
            cls = data.get("class_type")
            exc = data.get("exception_message") or data.get("message") or data.get("exception_type") or ""
            tb = data.get("traceback") or data.get("traceback_message") or ""
            line = typ
            if node or cls:
                line += f" na node {node or '?'} {cls or ''}".rstrip()
            if exc:
                line += f": {exc}"
            elif tb:
                line += f": {tb}"
            parts.append(line)
        else:
            parts.append(f"{typ}: {data}")
        break
    if not parts and (completed is False or status_str in ("error", "failed", "interrupted")):
        parts.append(f"status={status_str or 'neznámý'}, completed={completed}")
    if not parts:
        return ""
    txt = _short(" | ".join(parts), 1800)
    low = txt.lower()
    if "modelmmap" in low and "get_file_handle" in low:
        txt += (" | Rychlá oprava: restartuj ComfyUI bez Dynamic VRAM "
                "(--disable-dynamic-vram --disable-mmap). Je to chyba načítání modelu, ne SaveVideo.")
    if "no space left on device" in low or "errno 28" in low:
        txt += (" | Na serveru s ComfyUI není místo na disku. Dokud se neuvolní, budou se soubory "
                "modelů dotahovat jen částečně a hlásit poškozené tenzory. Uvolni místo a nahraj "
                "dotčené modely znovu.")
    if "is invalid for input of size" in low and ("lora" in low or "loraloader" in low):
        txt += (" | Tohle je chyba souboru LoRA, ne promptu ani rozlišení: ComfyUI načte tenzor, "
                "který nemá očekávanou velikost. Obvykle je LoRA nedotažená/poškozená, nebo patří "
                "k jinému modelu. Nejčastější příčina je zaplněný disk serveru při stahování — "
                "uvolni místo a nahraj LoRA znovu, případně v šabloně sniž strength_model na 0.")
    elif "is invalid for input of size" in low:
        txt += (" | Nesouhlasí velikost tenzoru — obvykle nesedí model, LoRA nebo text encoder "
                "k sobě, nebo je některý soubor poškozený. Zkus stejné workflow spustit přímo "
                "v ComfyUI: když spadne i tam, chyba není v ComfyLocalu.")
    if "size mismatch" in low or "expected all tensors to be on the same device" in low:
        txt += (" | Zkontroluj, že checkpoint, LoRA a text encoder ve workflow patří k sobě.")
    txt += ltx_av_noise_hint(txt)
    return txt


def ltx_av_noise_hint(message: str) -> str:
    """Rozpozná známou chybu ComfyUI u LTX 2.3 s audiem.

    Signatura: „size of tensor a (X) must match … tensor b (Y)", kde platí
    Y = 128 × (X + počet audio latentů). Znamená to, že ComfyUI vyrobil šum
    jen pro video, ale latent obsahuje i audio — je to chyba ComfyUI
    (issue #13692 / #13887), ne chyba rozlišení ani šablony. Řeší ji jedině
    aktualizace ComfyUI na serveru.
    """
    m = re.search(r"size of tensor a \((\d+)\).{0,60}?tensor b \((\d+)\)", message, re.IGNORECASE)
    if not m:
        return ""
    a, b = int(m.group(1)), int(m.group(2))
    if b <= a or b % 128 != 0:
        return ""
    audio_latents = b // 128 - a
    if not 0 < audio_latents <= 10000:
        return ""
    return (f" | Tohle je známá chyba ComfyUI, ne chyba rozlišení: šum se vyrobil jen pro video "
            f"({a} tokenů), ale latent obsahuje i audio ({a} + {audio_latents} audio latentů = "
            f"{b}). Opraví to aktualizace ComfyUI na serveru (ComfyUI issue #13692 a #13887); "
            f"v appce to nastavit nejde.")


def raise_if_history_failed(history: dict) -> None:
    err = extract_history_error(history)
    if err:
        raise ComfyError("ComfyUI render spadl: " + err)


def find_output_files(history: dict) -> List[dict]:
    outputs = history.get("outputs") or {}
    found: List[dict] = []
    for node_id, out in outputs.items():
        for bucket in ("videos", "gifs", "images"):
            for item in out.get(bucket, []) or []:
                fn = item.get("filename") or ""
                ext = ("." + fn.rsplit(".", 1)[-1].lower()) if "." in fn else ""
                if bucket in ("videos", "gifs") or ext in VIDEO_SUFFIXES or (bucket == "images" and ext in IMAGE_SUFFIXES):
                    found.append({
                        "filename": fn,
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                        "bucket": bucket,
                        "node_id": node_id,
                    })
    return found


# ── pomůcky pro vstupní obrázky ─────────────────────────────
def sniff_image_suffix(path: Path) -> str:
    try:
        head = path.read_bytes()[:32]
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if head.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
            return ".gif"
        if head.startswith(b"BM"):
            return ".bmp"
    except Exception:
        pass
    return path.suffix.lower() if path.suffix.lower() in IMAGE_SUFFIXES else ".png"


def normalize_image_suffix(path: Path) -> Path:
    """ComfyUI potřebuje reálnou příponu podle obsahu, ne podle názvu z prohlížeče."""
    real = sniff_image_suffix(path)
    if path.suffix.lower() == real:
        return path
    new_path = path.with_suffix(real)
    if new_path.exists():
        new_path = path.with_name(path.stem + "_img" + real)
    try:
        path.rename(new_path)
        return new_path
    except Exception as e:
        log.warning("Přípona vstupního obrázku se nepodařila opravit (%s): %s", path.name, e)
        return path


__all__ = [
    "ComfyClient", "ComfyError", "extract_history_error", "raise_if_history_failed",
    "find_output_files", "normalize_image_suffix", "sniff_image_suffix",
    "IMAGE_SUFFIXES", "VIDEO_SUFFIXES",
]
