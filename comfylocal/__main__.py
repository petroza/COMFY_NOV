# -*- coding: utf-8 -*-
"""Spuštění ComfyLocal: python -m comfylocal"""
from __future__ import annotations

import socket
import threading
import webbrowser

import uvicorn

from .config import CONFIG
from .logging_setup import configure_logging
from .tls import setup_tls


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# Podle čeho poznat, kterou z našich pojmenovaných šablon uživatel importuje.
# Klíč = značky, které musí být v názvu souboru; hledá se v podobě bez oddělovačů,
# takže „video_ltx2_5_flf2v" i „ltx25-flf2v" dopadnou stejně.
TEMPLATE_NAMES = (
    (("ltx25", "flf2v"), "ltx25_flf2v_template.json"),
    (("ltx25", "i2v"), "ltx25_i2v_template.json"),
    (("minimaxh3", "r2v"), "minimax_h3_ref2v_template.json"),
    (("minimaxh3", "ref2v"), "minimax_h3_ref2v_template.json"),
    (("minimaxh3", "i2v"), "minimax_h3_i2v_template.json"),
)


def guess_template_name(source: str) -> str:
    """Odhadne jméno šablony podle názvu vstupního souboru."""
    flat = "".join(c for c in source.lower() if c.isalnum())
    for needles, name in TEMPLATE_NAMES:
        if all(n in flat for n in needles):
            return name
    stem = "".join(c if c.isalnum() else "_" for c in source.lower())
    return (stem.strip("_") or "imported") + "_template.json"


def import_workflow_cli(args) -> int:
    """`python -m comfylocal import-workflow soubor.json` — UI export → API šablona.

    Jména parametrů se berou ze živého ComfyUI (object_info): v UI exportu jsou
    hodnoty uložené bez jmen a hádat se nesmí, protože špatně přiřazený parametr
    znamená spadlý nebo tiše špatný render.
    """
    import json
    from pathlib import Path

    from .comfy_client import ComfyClient
    from .import_workflow import (ImportError_, convert_ui_workflow,
                                  known_names_from_templates, model_files_in)

    src = Path(args.source)
    if not src.is_file():
        print(f"[CHYBA] Soubor {src} neexistuje.")
        return 1
    try:
        ui = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[CHYBA] {src.name} není platný JSON: {e}")
        return 1

    client = ComfyClient()
    print(f"Ptám se ComfyUI na seznam nodů: {client.base}")
    object_info = client.object_info()
    if object_info:
        print(f"  ComfyUI zná {len(object_info)} typů nodů — jména parametrů budou přesná.")
    else:
        print("  ComfyUI neodpovědělo. Zkusím to z našich šablon, ale nemusí to stačit.")

    try:
        api = convert_ui_workflow(ui, object_info=object_info,
                                  known_names=known_names_from_templates(CONFIG.workflows_dir))
    except ImportError_ as e:
        print(f"\n[NEPŘEVEDENO] {e}")
        return 2

    target = CONFIG.workflows_dir / (args.name or guess_template_name(src.stem))
    if target.exists() and not args.force:
        print(f"[CHYBA] {target.name} už existuje. Přepiš ho pomocí --force.")
        return 1

    models = model_files_in(api)
    missing = [m["value"] for m in client.missing_models(api)] if object_info else []

    target.write_text(json.dumps(api, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n[OK] {target.name} — {len(api)} nodů, {len(models)} modelů.")
    for m in models:
        print(f"   [{'CHYBÍ' if m in missing else 'ok':5s}] {m}")
    if missing:
        print("\nPozor: modely označené CHYBÍ na ComfyUI nejsou, render s nimi spadne.")
    print("\nProjekt se v appce objeví po jejím restartu.")
    return 0


def main() -> None:
    import argparse
    import sys

    # Bez podpříkazu se appka chová jako dřív a rovnou nastartuje.
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        parser = argparse.ArgumentParser(prog="python -m comfylocal")
        sub = parser.add_subparsers(dest="command", required=True)
        imp = sub.add_parser("import-workflow",
                             help="převede UI export z ComfyUI na API šablonu do workflows/")
        imp.add_argument("source", help="soubor uložený z ComfyUI (Workflow → Export)")
        imp.add_argument("--name", help="jak se má šablona jmenovat ve workflows/")
        imp.add_argument("--force", action="store_true", help="přepsat existující šablonu")
        args = parser.parse_args()
        configure_logging()
        setup_tls()
        raise SystemExit(import_workflow_cli(args))
    run_server()


def run_server() -> None:
    log_path = configure_logging()
    tls_state = setup_tls()
    host = str(CONFIG.get("host") or "0.0.0.0")
    port = int(CONFIG.get("port") or 8770)
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    print("=" * 66)
    print(" ComfyLocal — ComfyUI na lokální síti, bez FTP a bez workeru")
    print(f" Web:      http://{shown_host}:{port}")
    if host in ("0.0.0.0", "::"):
        print(f" V síti:   http://{_local_ip()}:{port}")
    print(f" ComfyUI:  {CONFIG.comfy_base}")
    print(f" API:      {CONFIG.comfy_api_base}")
    print(f" Data:     {CONFIG.data_dir}")
    print(f" Log:      {log_path if log_path else 'jen do konzole (log_to_file: false)'}")
    print(f" TLS:      {tls_state}")
    print(f" PIN:      {'ano' if str(CONFIG.get('access_pin') or '').strip() else 'ne (otevřeno v síti)'}")
    print("=" * 66)

    if CONFIG.get("open_browser"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{shown_host}:{port}")).start()

    uvicorn.run("comfylocal.server:app", host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
