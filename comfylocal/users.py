# -*- coding: utf-8 -*-
"""Uživatelské účty a přihlašovací relace pro ComfyLocal.

Port účtů z webové (FTP) verze — jen bez PHP a bez internetu: tabulky
`comfy_users` a `comfy_sessions` leží ve stejné SQLite databázi jako fronta.
Hesla se ukládají jako PBKDF2-SHA256 (stdlib, žádná externí závislost).

Když v databázi není žádný aktivní účet, appka zůstane v původním režimu
(volný přístup, případně PIN z config.json) — nic se tím nerozbije.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from . import db

SESSION_COOKIE = "comfylocal_session"
SESSION_DAYS = 30
PBKDF2_ROUNDS = 120_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS comfy_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login    TEXT
);
CREATE TABLE IF NOT EXISTS comfy_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen  TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_username ON comfy_users(username);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON comfy_sessions(user_id);
"""

_LOCK = threading.RLock()
# Jednoduchý brzdič hádání hesel (jméno → [časy neúspěchů]); stejný smysl
# jako login throttle na webu, jen v paměti procesu.
_FAILS: Dict[str, List[float]] = {}
THROTTLE_WINDOW = 300.0
THROTTLE_MAX = 8


def ensure_schema() -> None:
    with _LOCK:
        conn = db.connect()
        conn.executescript(SCHEMA)
        conn.commit()


# ── hesla ───────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = str(stored).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ── throttle ────────────────────────────────────────────────
def throttled(username: str) -> bool:
    with _LOCK:
        hits = [t for t in _FAILS.get(username, []) if time.time() - t < THROTTLE_WINDOW]
        _FAILS[username] = hits
        return len(hits) >= THROTTLE_MAX


def record_fail(username: str) -> None:
    with _LOCK:
        _FAILS.setdefault(username, []).append(time.time())


def clear_fails(username: str) -> None:
    with _LOCK:
        _FAILS.pop(username, None)


# ── účty ────────────────────────────────────────────────────
def _row_to_public(row: Any) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "role": row["role"] or "user",
        "active": int(row["active"] or 0),
        "created_at": row["created_at"],
        "last_login": row["last_login"],
        "is_admin": (row["role"] or "user") == "admin",
    }


def has_users() -> bool:
    ensure_schema()
    with _LOCK:
        cnt = db.connect().execute("SELECT COUNT(*) FROM comfy_users WHERE active=1").fetchone()[0]
        return int(cnt) > 0


def list_users() -> List[Dict[str, Any]]:
    ensure_schema()
    with _LOCK:
        rows = db.connect().execute(
            "SELECT * FROM comfy_users ORDER BY username COLLATE NOCASE").fetchall()
        return [_row_to_public(r) for r in rows]


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    ensure_schema()
    with _LOCK:
        row = db.connect().execute("SELECT * FROM comfy_users WHERE id=?", (int(user_id),)).fetchone()
        return _row_to_public(row) if row else None


def save_user(user_id: Optional[int], username: str, password: str,
              role: str, active: bool) -> Dict[str, Any]:
    """Vytvoří nebo upraví účet. Prázdné heslo u editace = nemění se."""
    ensure_schema()
    username = str(username or "").strip()
    if not username:
        raise ValueError("Uživatelské jméno je povinné.")
    if len(username) > 60:
        raise ValueError("Uživatelské jméno je moc dlouhé (max 60 znaků).")
    role = "admin" if str(role) == "admin" else "user"
    with _LOCK:
        conn = db.connect()
        if user_id:
            if password:
                if len(password) < 4:
                    raise ValueError("Heslo musí mít alespoň 4 znaky.")
                conn.execute(
                    "UPDATE comfy_users SET username=?, role=?, active=?, password_hash=? WHERE id=?",
                    (username, role, 1 if active else 0, hash_password(password), int(user_id)))
            else:
                conn.execute("UPDATE comfy_users SET username=?, role=?, active=? WHERE id=?",
                             (username, role, 1 if active else 0, int(user_id)))
            new_id = int(user_id)
        else:
            if len(password or "") < 4:
                raise ValueError("Heslo musí mít alespoň 4 znaky.")
            cur = conn.execute(
                "INSERT INTO comfy_users (username, password_hash, role, active) VALUES (?,?,?,?)",
                (username, hash_password(password), role, 1 if active else 0))
            new_id = int(cur.lastrowid)
        conn.commit()
    return get_user(new_id) or {}


def bootstrap_from_config() -> Optional[str]:
    """Založí účet z `bootstrap_admin` v config.json a heslo z configu smaže.

    Heslo se tak nikdy nedostane do gitu (config.json je v .gitignore) a nezůstane
    ležet v souboru ani po prvním startu. Vrátí jméno vytvořeného účtu, nebo None.
    """
    from .config import CONFIG

    spec = CONFIG.get("bootstrap_admin")
    if not isinstance(spec, dict):
        return None
    username = str(spec.get("username") or "").strip()
    password = str(spec.get("password") or "")
    if not username or not password:
        return None

    ensure_schema()
    with _LOCK:
        row = db.connect().execute("SELECT id FROM comfy_users WHERE username=?", (username,)).fetchone()
    if row:
        # Účet už existuje — heslo z configu jen zahodíme, ať tam neleží.
        CONFIG.update_and_save({"bootstrap_admin": {"username": username, "password": ""}})
        return None

    save_user(None, username, password, "admin", True)
    CONFIG.update_and_save({"bootstrap_admin": {"username": username, "password": ""}})
    return username


def delete_user(user_id: int) -> None:
    ensure_schema()
    with _LOCK:
        conn = db.connect()
        conn.execute("DELETE FROM comfy_sessions WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM comfy_users WHERE id=?", (int(user_id),))
        conn.commit()


# ── relace ──────────────────────────────────────────────────
def login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Ověří jméno a heslo; při úspěchu vrátí {'token', 'user'}."""
    ensure_schema()
    username = str(username or "").strip()
    if not username:
        return None
    with _LOCK:
        row = db.connect().execute(
            "SELECT * FROM comfy_users WHERE username=? AND active=1", (username,)).fetchone()
    if not row or not verify_password(str(password or ""), row["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    with _LOCK:
        conn = db.connect()
        conn.execute("INSERT INTO comfy_sessions (token, user_id, created_at, last_seen)"
                     " VALUES (?,?,datetime('now'),datetime('now'))", (token, int(row["id"])))
        conn.execute("UPDATE comfy_users SET last_login=datetime('now') WHERE id=?", (int(row["id"]),))
        conn.commit()
    return {"token": token, "user": _row_to_public(row)}


def user_for_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    ensure_schema()
    with _LOCK:
        row = db.connect().execute(
            "SELECT u.* FROM comfy_sessions s JOIN comfy_users u ON u.id = s.user_id"
            " WHERE s.token=? AND u.active=1"
            " AND s.created_at > datetime('now', ?)",
            (str(token), f"-{SESSION_DAYS} days")).fetchone()
        if not row:
            return None
        db.connect().execute("UPDATE comfy_sessions SET last_seen=datetime('now') WHERE token=?",
                            (str(token),))
        db.connect().commit()
        return _row_to_public(row)


def logout(token: str) -> None:
    if not token:
        return
    ensure_schema()
    with _LOCK:
        conn = db.connect()
        conn.execute("DELETE FROM comfy_sessions WHERE token=?", (str(token),))
        conn.commit()
