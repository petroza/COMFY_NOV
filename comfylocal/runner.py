# -*- coding: utf-8 -*-
"""Render loop. Běží jako vlákno uvnitř appky — žádný samostatný worker,
žádné tokeny, žádný upload výsledku na hosting. Výstup jde do data/outputs.

Stavy jobů a hlášky jsou stejné jako na webu (pending → processing → uploading →
queued → generating → downloading → done), aby fungoval původní frontend.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import db
from .comfy_client import (VIDEO_SUFFIXES, ComfyClient, ComfyError, find_output_files,
                           raise_if_history_failed)
from .config import CONFIG
from .workflow import (build_workflow, is_tensor_size_mismatch, node_stage_label,
                       template_native_resolution)

log = logging.getLogger("comfylocal.runner")


class Cancelled(Exception):
    pass


class Runner(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="comfylocal-runner")
        self.client = ComfyClient()
        self.stop_event = threading.Event()
        self.active_job_id: Optional[int] = None
        self._last_purge = 0.0

    # ── smyčka ──────────────────────────────────────────────
    def run(self) -> None:
        poll = float(CONFIG.get("poll_interval") or 2.0)
        log.info("Render loop běží, ComfyUI = %s", self.client.base)
        while not self.stop_event.is_set():
            try:
                self._maybe_purge()
                job = db.claim_next_job()
                if not job:
                    self.stop_event.wait(poll)
                    continue
                self.active_job_id = int(job["id"])
                try:
                    self._process(job)
                finally:
                    self.active_job_id = None
            except Exception as e:  # pojistka, aby smyčka nikdy neumřela
                log.exception("Render loop chyba: %s", e)
                self.stop_event.wait(poll)

    def reload_client(self) -> ComfyClient:
        """Po změně adresy v Setupu se klient postaví znovu (bez restartu appky)."""
        self.client = ComfyClient()
        log.info("Adresa ComfyUI přenastavena: %s (soubory %s)",
                 self.client.base, self.client.files_base)
        return self.client

    def _maybe_purge(self) -> None:
        hours = float(CONFIG.get("purge_finished_after_hours") or 0)
        if hours <= 0 or time.time() - self._last_purge < 600:
            return
        self._last_purge = time.time()
        removed = db.purge_finished_older_than(hours)
        if removed:
            log.info("Automatický úklid: smazáno %s dokončených jobů", removed)

    # ── stav jobu ───────────────────────────────────────────
    def _check_cancel(self, job_id: int) -> None:
        if db.is_cancel_requested(job_id):
            self.client.interrupt()
            raise Cancelled()

    def _set(self, job_id: int, status: Optional[str] = None, progress: Optional[int] = None,
             current_node: Optional[str] = None, message: Optional[str] = None,
             event: bool = False, **extra) -> None:
        fields = dict(extra)
        if status:
            fields["status"] = status
        if progress is not None:
            fields["progress"] = int(progress)
        if current_node is not None:
            fields["current_node"] = current_node
        db.update_job(job_id, **fields)
        if message and event:
            db.add_event(job_id, status or "info", message)

    # ── jeden job ───────────────────────────────────────────
    def _process(self, job: dict) -> None:
        job_id = int(job["id"])
        settings = dict(job.get("settings") or {})
        client_id = str(uuid.uuid4())
        work_dir = CONFIG.tmp_dir / f"job_{job_id}_{int(time.time())}"
        work_dir.mkdir(parents=True, exist_ok=True)
        log.info("→ Job #%s: %r", job_id, str(job.get("prompt") or "")[:70])
        try:
            if not self.client.online():
                raise ComfyError(
                    f"ComfyUI není dostupné na {self.client.base}. Zkontroluj, že běží, že jsi "
                    "na správné síti/VPN a že comfy_url v config.json je správná."
                )

            self._check_cancel(job_id)
            self._set(job_id, "uploading", 4, "upload_image",
                      "Nahrávám vstupní obrázek do ComfyUI", event=True)
            input_path = CONFIG.base_dir / str(job["input_image"])
            if not input_path.is_file():
                raise ComfyError(f"Vstupní obrázek chybí na disku: {input_path.name}")
            comfy_img = self.client.upload_image(input_path)

            comfy_img_2 = None
            rel2 = settings.get("input_image_2")
            if rel2:
                self._set(job_id, "uploading", 6, "upload_image2", "Nahrávám poslední frejm do ComfyUI")
                path2 = CONFIG.base_dir / str(rel2)
                if not path2.is_file():
                    raise ComfyError(f"Druhý vstupní obrázek chybí na disku: {path2.name}")
                comfy_img_2 = self.client.upload_image(path2)
            elif str(settings.get("input_mode") or "").lower() in ("2pict", "flf2v"):
                raise ComfyError("Job je 2 PICT, ale druhý obrázek u něj chybí.")

            self._check_cancel(job_id)
            workflow_name = self._workflow_for(job)
            self._set(job_id, "queued", 7, "workflow", f"Sestavuji workflow {workflow_name}")
            hist = self._render(job, job_id, client_id, comfy_img, comfy_img_2, workflow_name)
            outputs = find_output_files(hist)
            if not outputs:
                raise ComfyError(
                    "V ComfyUI history není žádný výstup. Render neskončil chybou, ale "
                    "SaveVideo/CreateVideo/SaveImage nic neuložil — zkontroluj zapojení ve workflow."
                )

            self._set(job_id, "downloading", 95, "download",
                      f"Stahuji výstup ({len(outputs)} k dispozici)")
            chosen: Optional[Path] = None
            for item in outputs:
                path = self.client.download_output(item, work_dir)
                if path.suffix.lower() in VIDEO_SUFFIXES:
                    chosen = path
                    break
            if chosen is None:
                chosen = self.client.download_output(outputs[0], work_dir)

            final_path = CONFIG.outputs_dir / f"job_{job_id}_{int(time.time())}{chosen.suffix.lower()}"
            shutil.move(str(chosen), final_path)
            rel = final_path.relative_to(CONFIG.base_dir).as_posix()

            db.finish_job(job_id, "done", error=None, message=f"Hotovo: {final_path.name}",
                          output_video=rel, output_files=outputs)
            db.update_job(job_id, current_node="done", progress=100)
            log.info("✓ Job #%s hotovo: %s", job_id, final_path.name)

        except Cancelled:
            log.info("⏹ Job #%s zrušen", job_id)
            db.finish_job(job_id, "cancelled", error="Zrušeno uživatelem", message="Render zrušen")
            db.update_job(job_id, current_node="cancelled")
        except Exception as e:
            log.exception("✗ Job #%s chyba", job_id)
            db.finish_job(job_id, "error", error=str(e), message=str(e))
            db.update_job(job_id, current_node="error")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _render_once(self, job: dict, job_id: int, client_id: str, comfy_img: str,
                     comfy_img_2: Optional[str], workflow_name: str) -> dict:
        workflow = build_workflow(job, comfy_img, comfy_img_2, self.client, workflow_name)
        prompt_id = self.client.submit(workflow, client_id)
        self._set(job_id, "queued", 8, "queued", f"ComfyUI přijalo prompt {prompt_id}",
                  event=True, comfy_prompt_id=prompt_id)
        self._watch(job_id, prompt_id, client_id, workflow)
        self._check_cancel(job_id)
        self._set(job_id, "downloading", 94, "history", "Načítám výsledek z ComfyUI")
        hist = self.client.history(prompt_id)
        raise_if_history_failed(hist)
        return hist

    def _render(self, job: dict, job_id: int, client_id: str, comfy_img: str,
                comfy_img_2: Optional[str], workflow_name: str) -> dict:
        """Render s jedním záchranným pokusem v nativním rozlišení šablony.

        LTX šablony nesnesou každé rozlišení — sampler pak spadne na nesouhlasu
        tenzorů (`must match the size of tensor …`). Než job zahodit, zkusíme ho
        ještě jednou v rozlišení, se kterým je šablona vyexportovaná, a napíšeme
        to do událostí jobu, aby bylo jasné, proč je výstup menší.
        """
        try:
            return self._render_once(job, job_id, client_id, comfy_img, comfy_img_2, workflow_name)
        except ComfyError as e:
            settings = dict(job.get("settings") or {})
            requested = (int(settings.get("width") or 0), int(settings.get("height") or 0))
            native = template_native_resolution(workflow_name)
            retry_allowed = bool(CONFIG.get("ltx_retry_native_resolution", True))
            if not (retry_allowed and is_tensor_size_mismatch(str(e)) and native and native != requested):
                raise
            db.add_event(job_id, "retry",
                         f"Render v {requested[0]}×{requested[1]} spadl na nesouhlasu tenzorů — "
                         f"zkouším znovu v nativním rozlišení šablony {native[0]}×{native[1]}.",
                         {"error": str(e)[:400], "requested": requested, "native": native})
            log.warning("Job #%s: %s×%s neprošlo (%s) — zkouším nativní %s×%s",
                        job_id, requested[0], requested[1], str(e)[:120], native[0], native[1])
            settings["width"], settings["height"] = native
            settings["resolution_fallback_from"] = list(requested)
            job = {**job, "settings": settings}
            db.update_job(job_id, settings=settings)
            self._set(job_id, "queued", 7, "workflow",
                      f"Zkouším znovu v {native[0]}×{native[1]}", event=True)
            return self._render_once(job, job_id, client_id, comfy_img, comfy_img_2, workflow_name)

    def _workflow_for(self, job: dict) -> str:
        """Workflow z projektu jobu, jinak výchozí podle režimu."""
        from . import projects as projects_mod
        name = projects_mod.workflow_file_for_project(job.get("project_id"))
        if name:
            return name
        settings = job.get("settings") or {}
        two = str(settings.get("input_mode") or "").lower() in ("2pict", "flf2v") or \
            bool(settings.get("input_image_2"))
        return str(CONFIG.get("flf2v_workflow") if two else CONFIG.get("default_workflow"))

    # ── sledování průběhu ───────────────────────────────────
    def _watch(self, job_id: int, prompt_id: str, client_id: str, workflow: dict) -> None:
        ws = self.client.connect_ws(client_id)
        if ws is None:
            self._watch_poll(job_id, prompt_id)
            return

        last_progress = 8
        last_signal = time.time()
        started = time.time()
        last_node = "queued"
        self._set(job_id, "generating", 8, "queued", "ComfyUI generuje")
        try:
            while True:
                self._check_cancel(job_id)
                now = time.time()

                hist = self.client.history(prompt_id, allow_empty=True)
                if hist:
                    raise_if_history_failed(hist)
                    self._set(job_id, "generating", max(last_progress, 93), "history",
                              "ComfyUI dokončilo generování")
                    return

                if now - last_signal >= 20:
                    running, pending, pending_count = self.client.prompt_in_queue(prompt_id)
                    if running:
                        last_progress = max(last_progress, min(90, 15 + int((now - started) / 3)))
                        self._set(job_id, "generating", last_progress, last_node,
                                  f"{node_stage_label(workflow, last_node)}…")
                    elif pending:
                        last_progress = max(last_progress, min(20, 8 + pending_count))
                        self._set(job_id, "queued", last_progress, "queue",
                                  f"Ve frontě ComfyUI ({pending_count} čeká)")
                    else:
                        last_progress = min(92, last_progress + 1)
                        self._set(job_id, "generating", last_progress, last_node,
                                  f"Čekám na dokončení: {node_stage_label(workflow, last_node)}")
                    last_signal = now

                try:
                    raw = ws.recv()
                except Exception as e:
                    if _is_timeout(e):
                        continue
                    log.warning("WS recv chyba (%s), pokračuji pollingem.", e)
                    self._watch_poll(job_id, prompt_id, start_progress=last_progress)
                    return
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                typ = msg.get("type")
                data = msg.get("data") or {}
                pid = data.get("prompt_id")
                if pid and pid != prompt_id:
                    continue
                last_signal = time.time()

                if typ == "executing":
                    node = data.get("node")
                    if node is None:
                        self._set(job_id, "generating", max(last_progress, 93), "done",
                                  "ComfyUI dokončilo graf")
                        return
                    last_node = str(node)
                    self._set(job_id, "generating", max(last_progress, 12), last_node,
                              f"{node_stage_label(workflow, last_node)} – node {last_node}")
                elif typ == "progress":
                    value = data.get("value") or 0
                    maxv = data.get("max") or 1
                    try:
                        ratio = min(1.0, float(value) / max(float(maxv), 1.0))
                    except Exception:
                        ratio = 0.0
                    last_progress = max(last_progress, 12 + int(ratio * 78))
                    last_node = str(data.get("node") or last_node)
                    self._set(job_id, "generating", last_progress, last_node,
                              f"{node_stage_label(workflow, last_node)} – {value}/{maxv}")
                elif typ == "executed":
                    node = data.get("node")
                    if node is not None:
                        last_node = str(node)
                        last_progress = max(last_progress, 85)
                        self._set(job_id, "generating", last_progress, last_node,
                                  f"{node_stage_label(workflow, last_node)} dokončeno")
                elif typ == "execution_cached":
                    last_progress = max(last_progress, 70)
                    self._set(job_id, "generating", last_progress, "cache", "Použita cache ComfyUI")
                elif typ in ("execution_error", "execution_interrupted"):
                    raise ComfyError(f"ComfyUI {typ}: {data}")
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _watch_poll(self, job_id: int, prompt_id: str, timeout_s: int = 3600,
                    start_progress: int = 8) -> None:
        start = time.time()
        pct = max(8, int(start_progress))
        while True:
            self._check_cancel(job_id)
            if time.time() - start > timeout_s:
                raise TimeoutError("ComfyUI timeout při generování")
            hist = self.client.history(prompt_id, allow_empty=True)
            if hist:
                raise_if_history_failed(hist)
                self._set(job_id, "generating", max(93, pct), "history", "ComfyUI dokončilo generování")
                return
            running, pending, pending_count = self.client.prompt_in_queue(prompt_id)
            if running:
                pct = min(92, pct + 2)
                self._set(job_id, "generating", pct, "running", "ComfyUI počítá…")
            elif pending:
                pct = max(pct, min(20, 8 + pending_count))
                self._set(job_id, "queued", pct, "queue", f"Ve frontě ComfyUI ({pending_count} čeká)")
            else:
                pct = min(92, pct + 1)
                self._set(job_id, "generating", pct, "polling", "Čekám na dokončení v ComfyUI")
            # V lokální síti se můžeme ptát častěji než původní worker, který šetřil hosting.
            for _ in range(5):
                self._check_cancel(job_id)
                time.sleep(1)


def _is_timeout(exc: Exception) -> bool:
    import socket
    if isinstance(exc, socket.timeout):
        return True
    return "timed out" in str(exc).lower()


_RUNNER: Optional[Runner] = None


def get_runner() -> Runner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = Runner()
    return _RUNNER


def start_runner() -> Runner:
    runner = get_runner()
    if not runner.is_alive():
        runner.start()
    return runner
