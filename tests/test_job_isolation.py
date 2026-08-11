# -*- coding: utf-8 -*-
"""Uživatelé si nesmí vidět do jobů.

Tohle je ta nejcitlivější část appky: v UI se nesprávné chování nepozná
(fronta vypadá pořád stejně), ale rozbité filtrování by lidem ukázalo cizí
prompty a videa. Proto se to testuje na úrovni dat, ne přes prohlížeč.
"""
from __future__ import annotations


def make_jobs(db):
    """Petr (2) a Wolf (1) mají každý job; ještě jeden job je bez vlastníka."""
    return {
        "petr": db.create_job("PETR tajny", "", "p", "data/uploads/a.png", "a.png", {}, None, 2, "Petr"),
        "wolf": db.create_job("WOLF tajny", "", "p", "data/uploads/b.png", "b.png", {}, None, 1, "Wolf"),
        "orphan": db.create_job("STARY job", "", "p", "data/uploads/c.png", "c.png", {}, None, None, ""),
    }


def find(jobs, job_id):
    return next(j for j in jobs if int(j["id"]) == int(job_id))


def test_foreign_job_hides_content_but_shows_owner(app_modules):
    db = app_modules["db"]
    ids = make_jobs(db)

    seen = db.list_jobs_for_user(user_id=2, is_admin=False)
    wolf_job = find(seen, ids["wolf"])

    assert wolf_job["foreign"] is True
    assert wolf_job["prompt"] == ""
    assert wolf_job["input_url"] is None
    assert wolf_job["output_url"] is None
    assert wolf_job["settings"] == {}
    # Jméno a pořadí ve frontě naopak vidět MUSÍ, o to jde.
    assert wolf_job["user_name"] == "Wolf"
    assert wolf_job["queue_position"] >= 1


def test_own_job_stays_complete(app_modules):
    db = app_modules["db"]
    ids = make_jobs(db)

    mine = find(db.list_jobs_for_user(user_id=2, is_admin=False), ids["petr"])
    assert mine.get("foreign") is not True
    assert mine["prompt"] == "PETR tajny"
    assert mine["input_url"] is not None


def test_admin_sees_everything(app_modules):
    db = app_modules["db"]
    ids = make_jobs(db)

    seen = db.list_jobs_for_user(user_id=1, is_admin=True)
    assert find(seen, ids["petr"])["prompt"] == "PETR tajny"
    assert find(seen, ids["wolf"])["prompt"] == "WOLF tajny"
    assert not any(j.get("foreign") for j in seen)


def test_single_user_mode_sees_everything(app_modules):
    """Bez účtů (user_id=None) se nic neskrývá — jinak by uživatel přišel o detail."""
    db = app_modules["db"]
    ids = make_jobs(db)

    seen = db.list_jobs_for_user(user_id=None, is_admin=False)
    assert find(seen, ids["wolf"])["prompt"] == "WOLF tajny"
    assert not any(j.get("foreign") for j in seen)


def test_orphan_jobs_visible_to_all(app_modules):
    """Joby z doby před účty nemají vlastníka a nesmí se nikomu ztratit."""
    db = app_modules["db"]
    ids = make_jobs(db)

    orphan = find(db.list_jobs_for_user(user_id=2, is_admin=False), ids["orphan"])
    assert orphan.get("foreign") is not True
    assert orphan["prompt"] == "STARY job"


def test_may_see_job_rules(app_modules):
    """Stejná pravidla, jaká hlídají cancel/delete/rerun/job_file v compat.py."""
    db = app_modules["db"]
    from comfylocal.compat import may_see_job
    ids = make_jobs(db)

    petr_job = db.get_job(ids["petr"])
    wolf_job = db.get_job(ids["wolf"])
    orphan_job = db.get_job(ids["orphan"])

    assert may_see_job(petr_job, user_id=2, is_admin=False) is True
    assert may_see_job(wolf_job, user_id=2, is_admin=False) is False
    assert may_see_job(wolf_job, user_id=1, is_admin=False) is True
    assert may_see_job(wolf_job, user_id=2, is_admin=True) is True     # admin smí
    assert may_see_job(wolf_job, user_id=None, is_admin=False) is True  # režim bez účtů
    assert may_see_job(orphan_job, user_id=2, is_admin=False) is True
    assert may_see_job(None, user_id=2, is_admin=False) is False


def test_clear_finished_only_removes_own_jobs(app_modules):
    """Uživatel nesmí „uklidit" hotové joby někoho jiného."""
    db = app_modules["db"]
    ids = make_jobs(db)
    for jid in ids.values():
        db.update_job(jid, status="done")

    removed, _ = db.clear_finished(user_id=2)
    assert removed == 1
    remaining = {int(j["id"]) for j in db.list_jobs()}
    assert ids["petr"] not in remaining
    assert ids["wolf"] in remaining
    assert ids["orphan"] in remaining
