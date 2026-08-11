# -*- coding: utf-8 -*-
"""Fronta: střídání uživatelů, pořadí a odhad času."""
from __future__ import annotations


def add(db, user_id, name, prompt="p"):
    return db.create_job(prompt, "", "preset", "data/uploads/x.png", "x.png", {}, None, user_id, name)


def test_fair_queue_alternates_between_users(app_modules):
    """Dávka od jednoho člověka nesmí zablokovat ostatní.

    Petr pošle 3 joby, Wolf pak 1. Bez střídání by Wolf čekal na všechny tři.
    """
    db = app_modules["db"]
    p1 = add(db, 2, "Petr"); p2 = add(db, 2, "Petr"); p3 = add(db, 2, "Petr")
    w1 = add(db, 1, "Wolf")

    order = []
    for _ in range(4):
        job = db.claim_next_job()
        order.append(int(job["id"]))
        # Simulace dokončení — jinak by se pořadí nemělo o co opřít.
        db.finish_job(int(job["id"]), status="done")

    assert order[0] == p1, "první jde nejstarší job"
    assert order[1] == w1, f"Wolf musí přijít hned po prvním Petrově jobu, ne až po všech (bylo {order})"
    assert set(order) == {p1, p2, p3, w1}


def test_fifo_when_fair_queue_disabled(app_modules):
    """S fair_queue=false se drží striktní pořadí podle vytvoření."""
    db = app_modules["db"]
    config = app_modules["config"]
    config.CONFIG._data["fair_queue"] = False

    p1 = add(db, 2, "Petr"); p2 = add(db, 2, "Petr")
    w1 = add(db, 1, "Wolf")

    order = []
    for _ in range(3):
        job = db.claim_next_job()
        order.append(int(job["id"]))
        db.finish_job(int(job["id"]), status="done")
    assert order == [p1, p2, w1]


def test_queue_positions_are_sequential(app_modules):
    db = app_modules["db"]
    a = add(db, 2, "Petr"); b = add(db, 1, "Wolf"); c = add(db, 2, "Petr")

    jobs = db.list_jobs_for_user(user_id=2, is_admin=False)
    by_id = {int(j["id"]): j for j in jobs}
    assert by_id[a]["queue_position"] == 1
    assert by_id[b]["queue_position"] == 2
    assert by_id[c]["queue_position"] == 3


def test_jobs_ahead_counts_only_jobs_before_mine(app_modules):
    db = app_modules["db"]
    add(db, 1, "Wolf")   # dva cizí joby před Petrem
    add(db, 1, "Wolf")
    add(db, 2, "Petr")

    assert db.jobs_ahead_of_user(2) == 2
    assert db.jobs_ahead_of_user(1) == 0


def test_jobs_ahead_is_zero_without_own_job(app_modules):
    db = app_modules["db"]
    add(db, 1, "Wolf")
    assert db.jobs_ahead_of_user(2) == 0


def test_eta_none_until_there_is_history(app_modules):
    """Bez dokončených renderů se nemá co odhadovat — radši nic než výmysl."""
    db = app_modules["db"]
    add(db, 2, "Petr")
    assert db.average_job_seconds() is None
    assert db.queue_eta_seconds(2) is None


def test_eta_uses_average_duration_and_queue_length(app_modules):
    db = app_modules["db"]
    for _ in range(2):
        jid = add(db, 1, "Wolf")
        db.update_job(jid, status="done", duration_seconds=100.0)

    assert db.average_job_seconds() == 100.0

    add(db, 1, "Wolf")   # jeden cizí job před Petrem
    add(db, 2, "Petr")
    # Petr čeká na 1 cizí job + svůj vlastní => 2 × 100 s
    assert db.queue_eta_seconds(2) == 200.0


def test_no_eta_when_user_has_nothing_in_queue(app_modules):
    """Kdo nic nerendruje, nesmí vidět „hotovo za 3 min".

    Regrese: dřív se počítalo avg × (0 + 1), takže se odhad ukazoval i lidem
    bez jediného jobu ve frontě.
    """
    db = app_modules["db"]
    jid = add(db, 1, "Wolf")
    db.update_job(jid, status="done", duration_seconds=180.0)

    assert db.average_job_seconds() == 180.0     # historie existuje
    assert db.queue_eta_seconds(2) is None       # ale Petr nic nečeká
    assert db.queue_eta_seconds(1) is None       # a Wolfovi už doběhlo


def test_failed_jobs_do_not_skew_eta(app_modules):
    """Chyba spadne za pár sekund; kdyby se počítala, odhad by byl nesmysl."""
    db = app_modules["db"]
    ok_id = add(db, 1, "Wolf")
    db.update_job(ok_id, status="done", duration_seconds=120.0)
    bad_id = add(db, 1, "Wolf")
    db.update_job(bad_id, status="error", duration_seconds=2.0)

    assert db.average_job_seconds() == 120.0
