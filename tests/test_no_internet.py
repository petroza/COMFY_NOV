# -*- coding: utf-8 -*-
"""Appka nesmí ve výchozím nastavení sáhnout na internet.

Tenhle test je pojistka proti tomu, aby se do appky nevrátila skrytá závislost
na internetu. Odchytává `requests` a hlásí každý cizí host, na který se appka
pokusí jít — ComfyUI a localhost jsou v pořádku, cokoliv jiného ne.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOCAL_HINTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


@pytest.fixture()
def net_watch(monkeypatch):
    """Zablokuje síť a zapíše, na jaké cizí hosty se appka pokusila jít."""
    attempts: list = []

    def guard(url, *a, **kw):
        text = str(url)
        host = text.split("/")[2] if "://" in text else text
        if not any(h in host for h in LOCAL_HINTS):
            attempts.append(host)
        raise requests.exceptions.ConnectionError("síť je v testu zakázaná")

    monkeypatch.setattr(requests, "get", guard)
    monkeypatch.setattr(requests, "post", guard)
    monkeypatch.setattr(requests, "request", lambda method, url, *a, **kw: guard(url))
    return attempts


@pytest.fixture()
def cfg():
    """Sahá na tu instanci CONFIG, kterou vidí modul `translate`.

    Jiné testy si `comfylocal.config` reloadují, takže `from comfylocal.config
    import CONFIG` může vrátit jinou instanci, než jakou si `translate` zapamatoval
    při importu. Nastavení by pak nemělo žádný efekt a test by lhal.
    """
    import comfylocal.translate as translate_module
    config = translate_module.CONFIG
    original = dict(config._data)
    config._data["translate_prompt"] = True
    yield config
    config._data.clear()
    config._data.update(original)


def test_default_translation_never_touches_the_internet(net_watch, cfg):
    """Výchozí backend „comfy" smí mluvit jen s ComfyUI."""
    from comfylocal.translate import translate_text
    cfg._data["translate_backend"] = "comfy"
    cfg._data["translate_allow_internet_fallback"] = False

    translate_text("kočka sedí na okně", "cs", "en")
    assert net_watch == [], f"appka šla na internet: {sorted(set(net_watch))}"


def test_disabled_translation_never_touches_the_internet(net_watch, cfg):
    from comfylocal.translate import translate_text
    cfg._data["translate_backend"] = "off"

    translate_text("kočka", "cs", "en")
    assert net_watch == []


def test_internet_fallback_stays_off_unless_explicitly_allowed(net_watch, cfg):
    """I když překlad přes ComfyUI selže, bez povolení se na internet nejde."""
    from comfylocal.translate import translate_text
    cfg._data["translate_backend"] = "comfy"
    cfg._data["translate_allow_internet_fallback"] = False

    result = translate_text("kočka", "cs", "en")
    assert result["success"] is False
    assert result["provider"] == "comfy"
    assert net_watch == [], "po selhání ComfyUI se nesmí tajně zkoušet Google"


def test_guard_itself_works(net_watch, cfg):
    """Kontrola testu: s backend=online se na internet jít MUSÍ.

    Bez tohohle by test výše mohl „projít" i kdyby odchytávání sítě nefungovalo.
    """
    from comfylocal.translate import translate_text
    cfg._data["translate_backend"] = "online"

    translate_text("kočka", "cs", "en")
    assert net_watch, "odchytávání sítě nefunguje — ostatní testy by byly bezcenné"


# ── statická kontrola zdrojáků ──────────────────────────────
EXTERNAL_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost)([a-zA-Z0-9.-]+)")
# Adresa ComfyUI v konfiguraci a odkazy v dokumentaci/komentářích jsou v pořádku.
ALLOWED_HOSTS = {"viz-proxy-dev.nova.group", "www.python.org", "github.com",
                 "raw.githubusercontent.com", "huggingface.co", "www.w3.org"}


def test_frontend_loads_no_external_resources():
    """CSS ani HTML si nesmí tahat fonty nebo skripty z internetu.

    Dřív tu byl `@import` fontů z Google Fonts — prohlížeč tím při každém
    otevření appky volal ven. Fonty jsou teď v `web/fonts/`.
    """
    offenders = []
    for path in sorted((ROOT / "web").glob("*.*")):
        if path.suffix.lower() not in (".css", ".html", ".js"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'(?:@import\s+url\(|src=["\']|href=["\'])\s*(https?://[^)"\']+)', text):
            offenders.append(f"{path.name}: {match.group(1)[:70]}")
    assert not offenders, "frontend tahá věci z internetu: " + "; ".join(offenders)


def test_bundled_fonts_are_present():
    """Když se fonty nezabalí, appka bude bez internetu vypadat jinak."""
    fonts = list((ROOT / "web" / "fonts").glob("*.woff2"))
    assert fonts, "chybí web/fonts/*.woff2"
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for font in fonts:
        assert font.name in css, f"{font.name} leží ve fonts/, ale CSS ho nepoužívá"


def test_only_translate_module_talks_to_the_internet():
    """Odchozí adresy smí být jen v translate.py (záložní online překlad)."""
    offenders = []
    for path in sorted((ROOT / "comfylocal").glob("*.py")):
        if path.name == "translate.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for host in EXTERNAL_URL.findall(line):
                if host not in ALLOWED_HOSTS:
                    offenders.append(f"{path.name}: {host}")
    assert not offenders, "nový odchozí provoz: " + "; ".join(offenders)
