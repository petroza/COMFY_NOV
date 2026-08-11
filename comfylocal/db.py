# -*- coding: utf-8 -*-
"""SQLite databáze ComfyLocal.

Schéma je záměrně stejné jako v api.php (comfy_jobs / comfy_events / comfy_projects),
takže původní frontend z app.php funguje bez přepisování. Rozdíl je jen v tom, že
tady není nic o uživatelích, workerech ani tokenech.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None

ACTIVE_STATUSES = ("pending", "processing", "queued", "generating", "uploading", "downloading")
FINISHED_STATUSES = ("done", "error", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS comfy_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt              TEXT NOT NULL,
    negative_prompt     TEXT,
    preset              TEXT,
    input_image         TEXT NOT NULL,
    input_original_name TEXT,
    output_video        TEXT,
    output_files        TEXT,
    settings_json       TEXT,
    comfy_prompt_id     TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    progress            INTEGER NOT NULL DEFAULT 0,
    current_node        TEXT,
    error               TEXT,
    project_id          INTEGER,
    user_id             INTEGER,
    user_name           TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    started_at          TEXT,
    finished_at         TEXT,
    duration_seconds    REAL
);
CREATE TABLE IF NOT EXISTS comfy_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    type       TEXT NOT NULL DEFAULT 'info',
    message    TEXT,
    data_json  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS comfy_projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    description   TEXT,
    workflow_file TEXT,
    input_type    TEXT NOT NULL DEFAULT 'image',
    settings_json TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_comfy_jobs_status ON comfy_jobs(status);
CREATE INDEX IF NOT EXISTS idx_comfy_jobs_created ON comfy_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_comfy_events_job ON comfy_events(job_id);
CREATE INDEX IF NOT EXISTS idx_projects_active ON comfy_projects(active);
"""


def now_sql() -> str:
    """Stejný formát jako SQLite datetime('now') — UTC bez zóny."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(CONFIG.db_path, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
            _CONN.commit()
        return _CONN


def _migrate_job_owner() -> None:
    """Doplní sloupce vlastníka do databáze vytvořené starší verzí.

    Joby z doby před účty zůstanou bez vlastníka (NULL) — ty vidí každý, aby se
    po updatu nikomu neztratila rozjetá fronta.
    """
    with _LOCK:
        conn = connect()
        have = {r["name"] for r in conn.execute("PRAGMA table_info(comfy_jobs)").fetchall()}
        if "user_id" not in have:
            conn.execute("ALTER TABLE comfy_jobs ADD COLUMN user_id INTEGER")
        if "user_name" not in have:
            conn.execute("ALTER TABLE comfy_jobs ADD COLUMN user_name TEXT")
        conn.commit()


def init() -> None:
    connect()
    _migrate_job_owner()
    with _LOCK:
        conn = connect()
        rows = conn.execute(
            "SELECT id FROM comfy_jobs WHERE status IN"
            " ('processing','uploading','queued','generating','downloading')"
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE comfy_jobs SET status='pending', progress=0, current_node=NULL,"
                " comfy_prompt_id=NULL, updated_at=? WHERE status IN"
                " ('processing','uploading','queued','generating','downloading')",
                (now_sql(),),
            )
            conn.commit()
    for r in rows:
        add_event(int(r["id"]), "restart", "Vráceno do fronty po restartu ComfyLocal")


# ── události ────────────────────────────────────────────────
def add_event(job_id: int, typ: str, message: str, data: Any = None) -> None:
    with _LOCK:
        conn = connect()
        conn.execute(
            "INSERT INTO comfy_events (job_id, type, message, data_json, created_at) VALUES (?,?,?,?,?)",
            (int(job_id), str(typ), str(message),
             json.dumps(data, ensure_ascii=False) if data is not None else None, now_sql()),
        )
        conn.commit()


def job_events(job_id: int, limit: int = 80) -> List[Dict[str, Any]]:
    with _LOCK:
        rows = connect().execute(
            "SELECT type, message, data_json, created_at FROM comfy_events"
            " WHERE job_id=? ORDER BY id DESC LIMIT ?",
            (int(job_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ── joby ────────────────────────────────────────────────────
def job_file_url(job_id: int, kind: str) -> str:
    return f"api.php?action=job_file&id={int(job_id)}&kind={kind}"


def job_row_to_public(row: Any) -> Dict[str, Any]:
    """Stejný tvar, jaký frontend očekával z api.php."""
    j = dict(row)
    settings_raw = j.pop("settings_json", None)
    try:
        settings = json.loads(settings_raw) if settings_raw else {}
    except Exception:
        settings = {}
    j["settings"] = settings if isinstance(settings, dict) else {}
    try:
        j["output_files_list"] = json.loads(j.get("output_files") or "[]")
    except Exception:
        j["output_files_list"] = []
    job_id = int(j.get("id") or 0)
    j["input_url"] = job_file_url(job_id, "input") if j.get("input_image") and job_id else None
    j["input2_url"] = job_file_url(job_id, "input2") if j["settings"].get("input_image_2") and job_id else None
    j["output_url"] = job_file_url(job_id, "output") if j.get("output_video") and job_id else None
    j["cancel_requested"] = bool(j.get("cancel_requested"))
    return j


def create_job(prompt: str, negative_prompt: str, preset: str, input_image: str,
               input_original_name: str = "", settings: Optional[dict] = None,
               project_id: Optional[int] = None, user_id: Optional[int] = None,
               user_name: str = "") -> int:
    ts = now_sql()
    with _LOCK:
        conn = connect()
        cur = conn.execute(
            "INSERT INTO comfy_jobs (prompt, negative_prompt, preset, input_image, input_original_name,"
            " settings_json, project_id, user_id, user_name, status, progress, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?,?)",
            (prompt, negative_prompt, preset, input_image, input_original_name,
             json.dumps(settings or {}, ensure_ascii=False), project_id,
             int(user_id) if user_id else None, str(user_name or "") or None, ts, ts),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    if "settings" in fields:
        fields["settings_json"] = json.dumps(fields.pop("settings") or {}, ensure_ascii=False)
    fields["updated_at"] = now_sql()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _LOCK:
        conn = connect()
        conn.execute(f"UPDATE comfy_jobs SET {sets} WHERE id=?", tuple(fields.values()) + (int(job_id),))
        conn.commit()


def get_job(job_id: int, public: bool = True) -> Optional[Dict[str, Any]]:
    with _LOCK:
        row = connect().execute("SELECT * FROM comfy_jobs WHERE id=?", (int(job_id),)).fetchone()
    if not row:
        return None
    return job_row_to_public(row) if public else dict(row)


def list_jobs(status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    sql = "SELECT * FROM comfy_jobs"
    params: Tuple = ()
    order = "id DESC"
    if status:
        sql += " WHERE status=?"
        params = (status,)
        order = "id ASC"
    sql += f" ORDER BY {order} LIMIT {limit}"
    with _LOCK:
        rows = connect().execute(sql, params).fetchall()
    return [job_row_to_public(r) for r in rows]


# Co z cizího jobu smí uživatel vidět: že někdo renderuje, kdo to je a kde je
# ve frontě. Prompt, obrázky, nastavení ani chyby se do odpovědi nedostanou.
_FOREIGN_VISIBLE = ("id", "status", "progress", "created_at", "started_at",
                    "finished_at", "user_name", "queue_position")


def redact_foreign_job(job: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: job.get(k) for k in _FOREIGN_VISIBLE if k in job}
    out["foreign"] = True
    out["prompt"] = ""
    out["preset"] = ""
    out["input_url"] = None
    out["input2_url"] = None
    out["output_url"] = None
    out["settings"] = {}
    out["output_files_list"] = []
    out["error"] = None
    return out


def annotate_queue_positions(jobs: List[Dict[str, Any]]) -> None:
    """Doplní jobům ve frontě pořadí (1 = renderuje se / jde na řadu jako první)."""
    waiting = sorted((j for j in jobs if str(j.get("status")) in ACTIVE_STATUSES),
                     key=lambda j: int(j.get("id") or 0))
    for pos, job in enumerate(waiting, start=1):
        job["queue_position"] = pos


def list_jobs_for_user(user_id: Optional[int], is_admin: bool, status: str = "",
                       limit: int = 200) -> List[Dict[str, Any]]:
    """Vlastní joby celé, cizí jen anonymizované (aby byla vidět fronta).

    Admin a režim bez účtů (user_id=None) vidí všechno — jinak by správce nemohl
    frontu spravovat a jednouživatelský provoz by přišel o detail jobu.
    """
    jobs = list_jobs(status, limit)
    annotate_queue_positions(jobs)
    if is_admin or user_id is None:
        return jobs
    out: List[Dict[str, Any]] = []
    for job in jobs:
        owner = job.get("user_id")
        if owner is None or int(owner) == int(user_id):
            out.append(job)
        else:
            out.append(redact_foreign_job(job))
    return out


def average_job_seconds(limit: int = 20) -> Optional[float]:
    """Průměrná doba posledních úspěšných renderů, nebo None když ještě nejsou.

    Bere se jen `done` — chyby spadnou po pár sekundách a průměr by zkreslily.
    """
    with _LOCK:
        rows = connect().execute(
            "SELECT duration_seconds FROM comfy_jobs"
            " WHERE status='done' AND duration_seconds IS NOT NULL AND duration_seconds > 0"
            " ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    values = [float(r["duration_seconds"]) for r in rows]
    return sum(values) / len(values) if values else None


def queue_eta_seconds(user_id: Optional[int]) -> Optional[float]:
    """Za jak dlouho přijde na uživatele řada a jeho job bude hotový.

    Odhad = (kolik jobů je před ním + ten jeho) × průměrná doba renderu.
    Vrací None, když uživatel nic ve frontě nemá nebo když ještě není
    z čeho průměrovat — vymyšlený odhad je horší než žádný.
    """
    if first_waiting_job_id(user_id) is None:
        return None
    avg = average_job_seconds()
    if avg is None:
        return None
    return avg * (jobs_ahead_of_user(user_id) + 1)


def first_waiting_job_id(user_id: Optional[int]) -> Optional[int]:
    """ID nejstaršího čekajícího/běžícího jobu uživatele, nebo None."""
    if not user_id:
        return None
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with _LOCK:
        row = connect().execute(
            f"SELECT MIN(id) FROM comfy_jobs WHERE status IN ({placeholders}) AND user_id=?",
            (*ACTIVE_STATUSES, int(user_id))).fetchone()
    value = row[0] if row else None
    return int(value) if value is not None else None


def jobs_ahead_of_user(user_id: Optional[int]) -> int:
    """Kolik cizích jobů čeká/renderuje před prvním jobem daného uživatele."""
    first_mine = first_waiting_job_id(user_id)
    if first_mine is None:
        return 0
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with _LOCK:
        row = connect().execute(
            f"SELECT COUNT(*) FROM comfy_jobs WHERE status IN ({placeholders}) AND id < ?",
            (*ACTIVE_STATUSES, first_mine)).fetchone()
    return int(row[0] if row else 0)


def queue_counts() -> Dict[str, int]:
    counts = {k: 0 for k in ("pending", "processing", "queued", "generating", "uploading",
                             "downloading", "done_today", "active_total", "finished_total")}
    with _LOCK:
        conn = connect()
        rows = conn.execute("SELECT status, COUNT(*) AS c FROM comfy_jobs GROUP BY status").fetchall()
        today = conn.execute(
            "SELECT COUNT(*) FROM comfy_jobs WHERE status='done'"
            " AND created_at >= datetime('now','start of day')"
        ).fetchone()
    for r in rows:
        status = str(r["status"] or "")
        count = int(r["c"] or 0)
        if status in counts:
            counts[status] = count
        if status in ACTIVE_STATUSES:
            counts["active_total"] += count
        if status in FINISHED_STATUSES:
            counts["finished_total"] += count
    counts["done_today"] = int(today[0] if today else 0)
    return counts


def _pick_pending_row(conn: sqlite3.Connection, fair: bool) -> Any:
    """Který pending job pustit dál.

    Ve `fair` režimu se uživatelé střídají: vybere se nejstarší job toho, kdo
    naposledy rendroval nejdřív (nebo ještě vůbec). Bez toho by dávka 40 obrázků
    od jednoho člověka zablokovala všechny ostatní na desítky minut.
    Joby bez vlastníka (z doby před účty) se berou jako jedna společná skupina.
    """
    if not fair:
        return conn.execute(
            "SELECT * FROM comfy_jobs WHERE status='pending' AND cancel_requested=0"
            " ORDER BY id ASC LIMIT 1"
        ).fetchone()

    # Poslední dokončený render každého uživatele = jeho místo v kolečku.
    # COALESCE kvůli tomu, kdo dnes ještě nic nerendroval — ten má přednost.
    return conn.execute(
        """
        WITH waiting AS (
            SELECT * FROM comfy_jobs WHERE status='pending' AND cancel_requested=0
        ),
        last_run AS (
            SELECT user_id, MAX(COALESCE(finished_at, started_at)) AS last_at
            FROM comfy_jobs
            WHERE COALESCE(finished_at, started_at) IS NOT NULL
            GROUP BY user_id
        )
        SELECT w.* FROM waiting w
        LEFT JOIN last_run r ON (r.user_id IS w.user_id)
        ORDER BY COALESCE(r.last_at, '') ASC, w.id ASC
        LIMIT 1
        """
    ).fetchone()


def claim_next_job() -> Optional[Dict[str, Any]]:
    fair = bool(CONFIG.get("fair_queue", True))
    with _LOCK:
        conn = connect()
        row = _pick_pending_row(conn, fair)
        if not row:
            return None
        job = job_row_to_public(row)
        ts = now_sql()
        conn.execute(
            "UPDATE comfy_jobs SET status='processing', progress=1, current_node='start',"
            " error=NULL, started_at=?, updated_at=? WHERE id=?",
            (ts, ts, job["id"]),
        )
        conn.commit()
    return job


def request_cancel(job_id: int) -> Tuple[bool, str]:
    job = get_job(job_id, public=False)
    if not job:
        return False, "Job nenalezen."
    if str(job["status"]) in FINISHED_STATUSES:
        return False, "Job už je dokončený."
    if str(job["status"]) == "pending":
        update_job(job_id, cancel_requested=1)
        finish_job(job_id, "cancelled", error="Zrušeno uživatelem", message="Job zrušen ve frontě")
        return True, "Job zrušen ve frontě."
    update_job(job_id, cancel_requested=1, current_node="cancelling")
    add_event(job_id, "cancel", "Job zrušen uživatelem")
    return True, "Ruším render v ComfyUI."


def is_cancel_requested(job_id: int) -> bool:
    with _LOCK:
        row = connect().execute(
            "SELECT cancel_requested FROM comfy_jobs WHERE id=?", (int(job_id),)).fetchone()
    return bool(row and row["cancel_requested"])


def finish_job(job_id: int, status: str, error: Optional[str] = None,
               message: Optional[str] = None, output_video: Optional[str] = None,
               output_files: Optional[List[dict]] = None) -> None:
    job = get_job(job_id, public=False) or {}
    duration = None
    started = job.get("started_at")
    if started:
        try:
            t0 = datetime.strptime(str(started), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            duration = round((datetime.now(timezone.utc) - t0).total_seconds(), 1)
        except Exception:
            duration = None
    fields: Dict[str, Any] = {
        "status": status,
        "progress": 100 if status == "done" else int(job.get("progress") or 0),
        "finished_at": now_sql(),
        "error": error,
    }
    if duration is not None:
        fields["duration_seconds"] = duration
    if output_video is not None:
        fields["output_video"] = output_video
    if output_files is not None:
        fields["output_files"] = json.dumps(output_files, ensure_ascii=False)
    update_job(job_id, **fields)
    if message:
        add_event(job_id, status, message)


def delete_job(job_id: int) -> List[str]:
    job = get_job(job_id, public=False)
    if not job:
        return []
    try:
        settings = json.loads(job.get("settings_json") or "{}") or {}
    except Exception:
        settings = {}
    deleted: List[str] = []
    for rel in (job.get("input_image"), job.get("output_video"), settings.get("input_image_2")):
        if not rel:
            continue
        path = CONFIG.base_dir / str(rel)
        try:
            if path.is_file():
                path.unlink()
                deleted.append(str(rel))
        except Exception:
            pass
    with _LOCK:
        conn = connect()
        conn.execute("DELETE FROM comfy_events WHERE job_id=?", (int(job_id),))
        conn.execute("DELETE FROM comfy_jobs WHERE id=?", (int(job_id),))
        conn.commit()
    return deleted


def clear_finished(user_id: Optional[int] = None) -> Tuple[int, List[str]]:
    """user_id=None uklidí vše (admin / režim bez účtů), jinak jen vlastní joby."""
    sql = "SELECT id FROM comfy_jobs WHERE status IN ('done','error','cancelled')"
    params: Tuple = ()
    if user_id is not None:
        sql += " AND user_id=?"
        params = (int(user_id),)
    with _LOCK:
        rows = connect().execute(sql, params).fetchall()
    deleted_files: List[str] = []
    for r in rows:
        deleted_files.extend(delete_job(int(r["id"])))
    return len(rows), deleted_files


def purge_finished_older_than(hours: float) -> int:
    if not hours or hours <= 0:
        return 0
    cutoff_sql = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        rows = connect().execute(
            "SELECT id FROM comfy_jobs WHERE status IN ('done','error','cancelled') AND updated_at < ?",
            (cutoff_sql,),
        ).fetchall()
    for r in rows:
        delete_job(int(r["id"]))
    return len(rows)


def cleanup_uploads() -> int:
    """Smaže uploady, na které už žádný job neodkazuje."""
    referenced = set()
    with _LOCK:
        rows = connect().execute("SELECT input_image, settings_json FROM comfy_jobs").fetchall()
    for r in rows:
        if r["input_image"]:
            referenced.add(str(r["input_image"]))
        try:
            s = json.loads(r["settings_json"] or "{}") or {}
            if s.get("input_image_2"):
                referenced.add(str(s["input_image_2"]))
        except Exception:
            pass
    removed = 0
    for path in CONFIG.uploads_dir.glob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(CONFIG.base_dir).as_posix()
        if rel not in referenced:
            try:
                path.unlink()
                removed += 1
            except Exception:
                pass
    return removed


# ── projekty (workflow) ─────────────────────────────────────
def list_projects(active_only: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT id, name, description, input_type, workflow_file, active, sort_order FROM comfy_projects"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY sort_order, id"
    with _LOCK:
        rows = connect().execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: int) -> Optional[Dict[str, Any]]:
    with _LOCK:
        row = connect().execute("SELECT * FROM comfy_projects WHERE id=?", (int(project_id),)).fetchone()
    return dict(row) if row else None


def upsert_project(name: str, workflow_file: str, description: str = "",
                   input_type: str = "image", sort_order: int = 0) -> int:
    ts = now_sql()
    with _LOCK:
        conn = connect()
        row = conn.execute("SELECT id FROM comfy_projects WHERE workflow_file=?", (workflow_file,)).fetchone()
        if row:
            conn.execute(
                "UPDATE comfy_projects SET name=?, description=?, input_type=?, sort_order=?,"
                " active=1, updated_at=? WHERE id=?",
                (name, description, input_type, sort_order, ts, int(row["id"])),
            )
            conn.commit()
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO comfy_projects (name, description, workflow_file, input_type, sort_order,"
            " active, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
            (name, description, workflow_file, input_type, sort_order, ts, ts),
        )
        conn.commit()
        return int(cur.lastrowid)


def deactivate_projects_except(existing_files: List[str]) -> None:
    with _LOCK:
        conn = connect()
        rows = conn.execute("SELECT id, workflow_file FROM comfy_projects WHERE active=1").fetchall()
        for r in rows:
            if str(r["workflow_file"]) not in existing_files:
                conn.execute("UPDATE comfy_projects SET active=0, updated_at=? WHERE id=?",
                             (now_sql(), int(r["id"])))
        conn.commit()
