#!/usr/bin/env python3
"""Průvodce prvním nastavením ComfyNOVA pro Windows a Linux."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / "config.example.json"
CONFIG = ROOT / "config.json"


def ask(label: str, default: str) -> str:
    return input(f"{label} [{default}]: ").strip() or default


def main() -> int:
    parser = argparse.ArgumentParser(description="První nastavení ComfyNOVA")
    parser.add_argument("--comfy-url")
    parser.add_argument("--port", type=int)
    parser.add_argument("--admin")
    parser.add_argument("--password")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if CONFIG.exists() and not args.force:
        print("[INFO] config.json už existuje. Nastavení ponechávám beze změny.")
        return 0
    if not EXAMPLE.is_file():
        raise SystemExit("[CHYBA] Chybí config.example.json.")

    data = json.loads(EXAMPLE.read_text(encoding="utf-8-sig"))
    default_url = str(data.get("comfy_url") or "http://127.0.0.1:8000/")
    default_port = int(data.get("port") or 8770)

    if args.non_interactive:
        if not all((args.comfy_url, args.port, args.admin, args.password)):
            raise SystemExit("[CHYBA] Neinteraktivní instalace vyžaduje všechny parametry.")
        comfy_url, port = args.comfy_url, args.port
        username, password = args.admin.strip(), args.password
    else:
        print("\nComfyNOVA — první nastavení")
        print("Stejný počítač/server: obvykle http://127.0.0.1:8000/")
        print("Firemní proxy: například https://viz-proxy-dev.nova.group/comfy/\n")
        comfy_url = ask("Adresa ComfyUI", default_url)
        while True:
            try:
                port = int(ask("Port ComfyNOVA", str(default_port)))
                if 1 <= port <= 65535:
                    break
            except ValueError:
                pass
            print("Zadej port od 1 do 65535.")
        username = ask("Jméno prvního správce", "admin").strip()
        while True:
            password = getpass.getpass("Heslo prvního správce (alespoň 4 znaky): ")
            confirm = getpass.getpass("Heslo znovu: ")
            if len(password) < 4:
                print("Heslo musí mít alespoň 4 znaky.")
            elif password != confirm:
                print("Hesla se neshodují.")
            else:
                break

    parsed = urlparse(comfy_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SystemExit("[CHYBA] Adresa ComfyUI musí začínat http:// nebo https://.")
    if not username or len(password) < 4 or not 1 <= int(port) <= 65535:
        raise SystemExit("[CHYBA] Neplatné jméno, heslo nebo port.")

    data["comfy_url"] = comfy_url.rstrip("/") + "/"
    data["host"] = "0.0.0.0"
    data["port"] = int(port)
    data["bootstrap_admin"] = {"username": username, "password": password}
    data["translate_backend"] = "comfy"
    data["translate_allow_internet_fallback"] = False
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n[OK] Nastavení bylo uloženo do config.json.")
    print("[INFO] Po prvním startu se vytvoří správce a heslo se z config.json odstraní.")
    print(f"[INFO] Web bude dostupný na portu {port}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
