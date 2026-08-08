# -*- coding: utf-8 -*-
"""Důvěra k TLS certifikátům.

Typický případ ve firmě: proxy má certifikát od interní certifikační autority.
Windows/macOS ji znají (v prohlížeči adresa funguje), ale Python ne — má vlastní
seznam autorit z balíčku certifi, takže stejná adresa skončí na
CERTIFICATE_VERIFY_FAILED.

Balíček `truststore` tohle spraví: přepne Python na systémové úložiště
certifikátů, takže appka věří tomu samému, čemu věří prohlížeč — a ověřování
zůstane zapnuté. Když `truststore` není k dispozici, jde ještě dát cestu k CA
balíčku (`comfy_ca_bundle`), nebo ověřování vypnout (`comfy_verify_tls: false`).
"""
from __future__ import annotations

import logging
from typing import Optional

from .config import CONFIG

log = logging.getLogger("comfylocal.tls")

_STATE: Optional[str] = None


def setup_tls() -> str:
    """Zapne systémové úložiště certifikátů. Vrátí popis režimu pro diagnostiku."""
    global _STATE
    if _STATE is not None:
        return _STATE

    if not bool(CONFIG.get("use_system_trust_store", True)):
        _STATE = "systémové úložiště vypnuté v config.json (použije se certifi)"
        return _STATE

    try:
        import truststore
        truststore.inject_into_ssl()
        _STATE = "systémové úložiště certifikátů (stejné, jakému věří prohlížeč)"
        log.info("TLS: %s", _STATE)
    except ImportError:
        _STATE = ("certifi — balíček truststore není nainstalovaný, takže interní firemní "
                  "certifikát Python neuzná; doinstaluj ho příkazem pip install truststore")
        log.warning("TLS: %s", _STATE)
    except Exception as e:
        _STATE = f"certifi — systémové úložiště se nepodařilo zapnout ({e})"
        log.warning("TLS: %s", _STATE)
    return _STATE


def tls_mode() -> str:
    """Jak se právě ověřují certifikáty — pro výpis v Diagnostice a Setupu."""
    bundle = str(CONFIG.get("comfy_ca_bundle") or "").strip()
    if bundle:
        return f"vlastní CA balíček: {bundle}"
    if not bool(CONFIG.get("comfy_verify_tls", True)):
        return "ověřování vypnuté (comfy_verify_tls: false)"
    return _STATE or "zatím nenastaveno"
