# Předání balíčku — co je nové proti COMFY_PC_FTP_WORKER

## Proč vznikl nový balíček

Původní řešení ([COMFY_PC_FTP_WORKER](https://github.com/petroza/COMFY_PC_FTP_WORKER)) bylo postavené na tom,
že ComfyUI běží doma na PC bez veřejné IP:

- PHP aplikace na sdíleném hostingu držela frontu (SQLite) a soubory,
- `worker_comfy.py` na PC s GPU si joby stahoval přes REST API s worker tokenem,
- workflow JSONy a nasazení se řešily přes FTP, hotová videa se nahrávala zpátky na web.

ComfyUI je teď dostupné na síti (`https://viz-proxy-dev.nova.group/comfy/`), takže celé to prostřední
patro zmizelo. ComfyLocal je jediný proces, který mluví přímo na ComfyUI.

## Co se změnilo

| Původně | Teď |
|---|---|
| PHP web na hostingu + Python worker na PC | Jedna Python appka (FastAPI + SQLite + UI) |
| Nasazení a workflow přes FTP | Lokální složka `workflows/`, nic se nikam nenahrává |
| Worker tokeny, pepper, HMAC, expirace, revokace | Nic z toho; volitelný PIN pro síť |
| Login, bcrypt hash, CSRF, rate limit, `install.php` | Volitelný PIN (`access_pin`) |
| Worker se ptal webu (`X-API-Token`) a nahrával výsledek na web | Render loop ve vlákně, výstupy v `data/outputs/` |
| `COMFY_BASE=http://127.0.0.1:8000` na PC s ComfyUI | `comfy_url` + `comfy_api_path` na proxy |
| Auto-start ComfyUI z workeru, GPU statistiky přes `nvidia-smi`, watchdog, restart workeru | Vypuštěno — ComfyUI běží mimo naše ruce; stav a VRAM se čtou z `/system_stats` |
| Admin panel, uživatelé, projekty v DB | Projekty se dopočítají ze složky `workflows/`, uživatelé nejsou |

## Co zůstalo zachované 1:1

Render logika z `worker_comfy.py` je portovaná do `comfylocal/workflow.py` včetně:

- **Autofixu názvů modelů** podle `object_info` — ale jen když ComfyUI název ze šablony vůbec nenabízí;
  jinak se nechá tak, jak je v projektu.
- **Patchování vstupů bez pevných node ID** — prompt (i `PrimitiveStringMultiline.value` s titulkem *Prompt*), negative (přepíše se jen když ho vyplníš), rozměry, seed, steps, cfg, fps, délka včetně linkovaných `PrimitiveInt` nodů.
- **Skládání promptu** v pořadí `děj → pohyb kamery → styl → technická kvalita` (LTX váží začátek promptu víc) a dedup částí.
- **FLF2V (2 PICT)** — nody `31` / `39`, fallback na první dva `LoadImage`, kontrola že se oba obrázky do workflow dostaly.
- **Prompt Enhance** (`TextGenerateLTX2Prompt.max_length`, boolean přepínač podle titulku).
- Presety pohybu kamery a stylu (stejné texty jako v `app.php`), formáty rozlišení, hlášky o fázi renderu.
- Diagnostika chyb z ComfyUI history včetně nápovědy pro `ModelMMAP/get_file_handle` (restart bez Dynamic VRAM).

## Co bylo záměrně vypuštěno

Není to potřeba pro provoz na jednom stroji v interní síti. Kdyby to někdo chtěl zpátky, je to v historii starého repa:

- uživatelé a role, admin panel, projekty a jejich workflow v DB,
- generování worker ZIPu s tokenem, `security.php`, `.htaccess` ochrany,
- automatický překlad promptu CZ→EN (Google GTX) — prompt se posílá tak, jak ho napíšeš,
- vzdálený start/restart ComfyUI a workeru, `nvidia-smi` statistiky.

## Adresy ComfyUI

Podstatné pro tuhle proxy: **endpointy jsou pod `/comfy/api/`**, soubory pod `/comfy/`.

```
POST  https://viz-proxy-dev.nova.group/comfy/api/prompt
GET   https://viz-proxy-dev.nova.group/comfy/api/queue
GET   https://viz-proxy-dev.nova.group/comfy/api/history/<prompt_id>
POST  https://viz-proxy-dev.nova.group/comfy/api/upload/image
WS    wss://viz-proxy-dev.nova.group/comfy/api/ws?clientId=<uuid>
GET   https://viz-proxy-dev.nova.group/comfy/view?filename=…   (výstupy)
```

Klient (`comfylocal/comfy_client.py`) drží dvě báze — API a file — a když jedna vrátí 404/405, zkusí druhou
a zapamatuje si, která funguje. Stejný balíček proto funguje i proti ComfyUI spuštěnému přímo
(`"comfy_url": "http://127.0.0.1:8188"`). WebSocket se zkouší na `/api/ws` i `/ws`; když proxy WS nepustí,
průběh se dopočítává pollingem `/queue` a `/history` (job tím netrpí, jen je progress hrubší).

## Provozní poznámky

- Restart appky nic neztratí: rozjeté joby se vrátí do stavu `pending` a spustí se znovu.
- Fronta je sériová (jeden job po druhém) — ComfyUI si stejně řadí prompty za sebe.
- `data/` roste s výstupy. Buď mazat v UI, nebo nastavit `purge_finished_after_hours`.
- Když chip v UI hlásí *ComfyUI offline*: zkontrolovat VPN/síť, `comfy_url`, u interního certifikátu
  `"comfy_verify_tls": false`, a jestli proxy nechce autentizaci (pak `comfy_headers`).


## Frontend je přenesený 1:1

`web/index.html`, `web/app.css` a `web/app.js` jsou vytažené z `app.php` (tam byl frontend prakticky
statický — jen 6 PHP vsuvek). Zůstal levý sidebar s frontou a hromadnými nástroji, akordeon
**01 Základ … 06 Odeslání**, pravý **Detail jobu** s akcemi, přepínač CZ/EN, světlý/tmavý režim,
kontextové menu nad jobem, dávkový upload i editace pending jobu.

Aby to fungovalo bez přepisování, backend nabízí **API kompatibilní s `api.php`** (`comfylocal/compat.py`,
endpoint `/api.php?action=…`) nad stejným schématem databáze (`comfy_jobs`, `comfy_events`, `comfy_projects`).
Rozdíly proti webu jsou jen tam, kde původní funkce neměla co obsluhovat:

| Prvek v UI | Původně | Teď |
|---|---|---|
| Tlačítko **Worker** | stažení worker ZIPu s tokenem | **ComfyUI** — otevře ComfyUI na síti |
| Tlačítko **Setup** | `security.php` (worker tokeny) | stránka Setup: adresa ComfyUI, API předpona, TLS, timeout, přehled workflow a cest |
| **Diagnostika** | PHP, SQLite, složky, worker signál | ComfyUI (API i file báze), WebSocket, GPU/VRAM, modely z `object_info`, workflow, složky, DB, překladač |
| Sekce **06 Odeslání** | FTP worker režim + výběr cílového PC | karta *ComfyUI na síti* + jediný cíl renderu se stavem a VRAM |
| Chip s workerem | `DESKTOP-…: offline`, verze workeru | host ComfyUI + `ComfyUI ready` + VRAM |
| Login (uživatel + heslo) | bcrypt, CSRF, rate limit | volitelný PIN (`access_pin`), stejný vzhled přihlašovací obrazovky |

Dvě věci se chovají jinak záměrně:

- **Překlad promptu.** Frontend překládá prompt před odesláním a neúspěch bral jako fatální — job se
  vůbec nezařadil. V interní síti bez výstupu do internetu by to appku zablokovalo, takže
  `translate_prompt` při nedostupném překladači vrátí původní text s `provider: none`. Prompt jde do
  ComfyUI česky a Diagnostika to hlásí.
- **Nastavení adresy.** Setup umí adresu ComfyUI přepsat za běhu (`POST /api/setup`), zapíše ji do
  `config.json` a znovu postaví klienta — bez restartu.

## Poznámka k adresám na proxy

Do `comfy_url` patří **`/comfy/`**, ne `/comfy/api/`. Když se do prohlížeče zadá `…/comfy/api/`,
ComfyUI si sáhne na relativní `assets/…` vůči `/comfy/api/`, kde nic není — proto ta rozbitá CSS/JS.
ComfyLocal si adresu skládá sám: endpointy `…/comfy/api/*`, soubory `…/comfy/view`, a když jedna báze
vrátí 404, zkusí druhou a zapamatuje si funkční variantu.


## Co se změnilo v „jen LTX 2.3, dva režimy"

Appka teď umí jediný model a dva režimy — 1 PICT (i2v) a 2 PICT (první + poslední frejm).
Šablony ve `workflows/` jsou export (API) přesně těch projektů, které jsou v `docs/comfyui_projects/`.

Vypuštěno:

- photo edit workflow (Flux.2, FireRed) a všechna větvení kolem nich v backendu i ve frontendu,
- projekty ve formátu UI ve `workflows/` — ComfyUI je přes `/prompt` nepřijme, teď se na to hlásí chyba,
- posuvníky **Kroky výpočtu**, **Držení promptu (cfg)** a **Síla pohybu**. U LTX 2.3 neřídily nic
  (šablona má `ManualSigmas` a `cfg = 1`) a přepis `cfg` hodnotou z UI kazil výsledek.

Opraveno proti šablonám, které appka posílala dřív:

| Co | Bylo | Je (podle projektu) |
|---|---|---|
| FLF2V checkpoint (3 nody) | `ltx-2.3-22b-dev-fp8` | `ltx-2.3-22b-distilled-fp8` |
| i2v samplery (`320:280`, `320:291`) | `euler_cfg_pp`, `euler_ancestral_cfg_pp` | `euler` |
| i2v `320:296` strength | přepisováno na 0.85 + tvrdá kontrola | 0.7 ze šablony, kontrola zrušena |
| Prompt Enhance `max_length` | strop 512 | 2048 |
| Rozlišení | libovolný násobek 8 z auto formátu | srovnané na LTX mřížku (`ltx_safe_size`) |

Poslední řádek je ta nejčastější příčina padání: auto formát dopočítával rozměry na osmičky,
takže třeba 1920×1080 nebo 1440×1080 rozešlo latent a vodicí obrázek ve druhém průchodu i2v
šablony a sampler skončil na `must match the size of tensor`.
