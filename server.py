from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import signal
import subprocess
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from typing import Any, Optional

from fastapi import Body, FastAPI, Form
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from src.comunidad.dataset import list_comunidades

app = FastAPI(title="Google Maps Scraper")

BASE_DIR = Path(__file__).parent
HISTORY_PATH = BASE_DIR / "out" / "history.json"


def _slugify(text: str) -> str:
    """Convierte texto a slug ASCII seguro para nombres de fichero."""
    normalized = unicodedata.normalize("NFD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lower = ascii_text.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", lower)
    slug = slug.strip("_")
    return slug[:40]


def _make_output_path(city: str, category: str) -> str:
    """Genera una ruta única para el CSV basada en ciudad, categoría y timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"out/{_slugify(city)}_{_slugify(category)}_{ts}.csv"


def _checkpoint_path(job_id: str) -> Path:
    return BASE_DIR / "out" / f"{job_id}.checkpoint.json"


def _build_cli_command(args: dict, output: str, resume_checkpoint: Optional[str] = None) -> list:
    """Reconstruye la línea de comandos para `python -m src.cli` desde un dict de args."""
    cmd = [
        sys.executable, "-u", "-m", "src.cli",
        "--category", str(args.get("category", "")),
        "--output", output,
        "--headless", str(args.get("headless", "true")),
        "--max-results", str(args.get("max_results", 0)),
        "--slow-ms", str(args.get("slow_ms", 250)),
        "--timeout-ms", str(args.get("timeout_ms", 15000)),
        "--concurrency", str(args.get("concurrency", 3)),
        "--adaptive-subdivision", str(args.get("adaptive_subdivision", "true")),
    ]
    if args.get("comunidad"):
        cmd += ["--comunidad", str(args["comunidad"]),
                "--min-poblacion", str(args.get("min_poblacion", 5000))]
    elif args.get("city"):
        cmd += ["--city", str(args["city"])]
    if resume_checkpoint:
        cmd += ["--resume-checkpoint", resume_checkpoint]
    return cmd


# job_id -> {"lines": [...], "status": "running"|"done"|"error"|"stopped", "output": str, "proc": Process|None}
jobs: dict[str, dict] = {}

# analyze_job_id (== scraping job_id) -> {"lines": [...], "status": "running"|"done"|"error", "proc": Process|None, "xlsx_output": str}
analyze_jobs: dict[str, dict] = {}

BRANDS_PATH = BASE_DIR / "config" / "excluded_brands.json"


def _load_history() -> None:
    """Carga el historial de ejecuciones desde disco al arrancar el servidor."""
    if not HISTORY_PATH.exists():
        return
    try:
        entries = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        for entry in entries:
            jobs[entry["job_id"]] = {
                "city": entry.get("city", ""),
                "category": entry.get("category", ""),
                "started_at": entry.get("started_at", ""),
                "status": entry.get("status", "done"),
                "valid_count": entry.get("valid_count", 0),
                "output": entry.get("output", ""),
                "args": entry.get("args", {}),
                "lines": [],
                "proc": None,
            }
    except Exception:
        pass  # fichero corrupto — arrancar sin historial


def _save_history() -> None:
    """Persiste el historial de ejecuciones a disco."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "job_id": jid,
            "city": j.get("city", ""),
            "category": j.get("category", ""),
            "started_at": j.get("started_at", ""),
            "status": j["status"],
            "valid_count": j.get("valid_count", 0),
            "output": j.get("output", ""),
            "args": j.get("args", {}),
        }
        for jid, j in jobs.items()
        if j.get("started_at")
    ]
    HISTORY_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_load_history()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/comunidades")
async def get_comunidades() -> dict:
    try:
        return {"comunidades": list_comunidades()}
    except Exception as exc:  # noqa: BLE001
        return {"comunidades": [], "error": str(exc)}


@app.get("/analyze/{job_id}", response_class=HTMLResponse)
async def analyze_page(job_id: str) -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "static" / "analyze.html").read_text(encoding="utf-8"))


@app.post("/run-analyze/{job_id}")
async def run_analyze(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        return {"error": "job not found"}

    csv_output = job.get("output", "")
    if not csv_output:
        return {"error": "no output path"}

    xlsx_output = csv_output.replace(".csv", ".xlsx")
    analyze_jobs[job_id] = {
        "status": "running",
        "lines": [],
        "proc": None,
        "xlsx_output": xlsx_output,
    }

    cmd = [
        sys.executable, "-u", "-m", "src.analyzer.cli",
        "--csv-path", csv_output,
        "--brands-path", "config/excluded_brands.json",
    ]

    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BASE_DIR),
        )
        analyze_jobs[job_id]["proc"] = proc
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            analyze_jobs[job_id]["lines"].append(line)
        await proc.wait()
        if analyze_jobs[job_id]["status"] != "stopped":
            analyze_jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"

    asyncio.create_task(run())
    return {"job_id": job_id, "xlsx_output": xlsx_output}


@app.get("/analyze-stream/{job_id}")
async def analyze_stream(job_id: str) -> StreamingResponse:
    if job_id not in analyze_jobs:
        async def not_found():
            yield "data: Job de análisis no encontrado\n\nevent: done\ndata: error\n\n"
        return StreamingResponse(not_found(), media_type="text/event-stream")

    async def event_generator():
        sent = 0
        while True:
            aj = analyze_jobs[job_id]
            lines = aj["lines"]
            while sent < len(lines):
                safe = lines[sent].replace("\n", " ")
                yield f"data: {safe}\n\n"
                sent += 1
            if aj["status"] != "running":
                yield f"event: done\ndata: {aj['status']}\n\n"
                break
            await asyncio.sleep(0.15)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/brands")
async def get_brands() -> dict:
    if not BRANDS_PATH.exists():
        return {"brands": []}
    return json.loads(BRANDS_PATH.read_text(encoding="utf-8"))


@app.post("/brands")
async def save_brands(payload: Any = Body(...)) -> dict:
    BRANDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRANDS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"status": "ok"}


@app.get("/download-xlsx/{job_id}")
async def download_xlsx(job_id: str) -> FileResponse:
    aj = analyze_jobs.get(job_id)
    if not aj:
        return FileResponse("/dev/null")
    xlsx_path = BASE_DIR / aj["xlsx_output"]
    return FileResponse(
        str(xlsx_path),
        filename=xlsx_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/run")
async def run_scraper(
    city: Optional[str] = Form(None),
    category: str = Form(...),
    headless: str = Form("true"),
    max_results: int = Form(0),
    slow_ms: int = Form(250),
    timeout_ms: int = Form(15000),
    concurrency: int = Form(3),
    adaptive_subdivision: str = Form("false"),
    comunidad: Optional[str] = Form(None),
    min_poblacion: int = Form(5000),
) -> dict:
    comunidad = (comunidad or "").strip() or None
    city = (city or "").strip() or None
    if not comunidad and not city:
        return {"error": "Debes indicar comunidad o ciudad"}

    job_id = str(uuid.uuid4())
    label_for_output = comunidad if comunidad else city
    output = _make_output_path(label_for_output, category)
    args_dict = {
        "city": city or "",
        "comunidad": comunidad or "",
        "category": category,
        "headless": headless,
        "max_results": max_results,
        "slow_ms": slow_ms,
        "timeout_ms": timeout_ms,
        "concurrency": concurrency,
        "adaptive_subdivision": adaptive_subdivision,
        "min_poblacion": min_poblacion,
    }
    jobs[job_id] = {
        "city": comunidad if comunidad else city,
        "category": category,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "valid_count": 0,
        "output": output,
        "args": args_dict,
        "lines": [],
        "proc": None,
    }

    # Crear checkpoint inicial — disponible aunque el proceso muera antes del primer flush
    ckpt_path = _checkpoint_path(job_id)
    try:
        from src.pipeline.checkpoint import CheckpointStore
        CheckpointStore.create(ckpt_path, args_dict, output, job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        # No es fatal — el job puede correr sin checkpoint, simplemente no se podrá reanudar.
        print(f"WARN: no se pudo crear checkpoint inicial: {exc}", file=sys.stderr)

    cmd = _build_cli_command(args_dict, output, resume_checkpoint=str(ckpt_path))

    asyncio.create_task(_run_subprocess(job_id, cmd))
    return {"job_id": job_id, "output": output}


async def _run_subprocess(job_id: str, cmd: list) -> None:
    """Lanza el subproceso del CLI, captura logs SSE y actualiza estado del job."""
    # start_new_session=True crea un process group propio (PGID == PID).
    # Esto permite matar TODO el árbol (Python + Playwright driver + Chromium
    # + helpers) con os.killpg() desde /stop, sin dejar huérfanos.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(BASE_DIR),
        start_new_session=True,
    )
    jobs[job_id]["proc"] = proc
    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        jobs[job_id]["lines"].append(line)
        m = re.search(r"valid=(\d+)", line)
        if m:
            jobs[job_id]["valid_count"] = int(m.group(1))
    await proc.wait()
    if jobs[job_id]["status"] != "stopped":
        if proc.returncode == 0:
            jobs[job_id]["status"] = "done"
        elif jobs[job_id].get("valid_count", 0) > 0:
            jobs[job_id]["status"] = "partial"
        else:
            jobs[job_id]["status"] = "error"
    _save_history()


@app.get("/stream/{job_id}")
async def stream(job_id: str) -> StreamingResponse:
    if job_id not in jobs:
        async def not_found():
            yield "data: Job no encontrado\n\nevent: done\ndata: error\n\n"
        return StreamingResponse(not_found(), media_type="text/event-stream")

    async def event_generator():
        sent = 0
        while True:
            job = jobs[job_id]
            lines = job["lines"]
            while sent < len(lines):
                # Escape newlines within the log line so SSE stays valid
                safe = lines[sent].replace("\n", " ")
                yield f"data: {safe}\n\n"
                sent += 1
            if job["status"] != "running":
                yield f"event: done\ndata: {job['status']}\n\n"
                break
            await asyncio.sleep(0.15)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/stop/{job_id}")
async def stop_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    if job["status"] != "running":
        return {"status": job["status"]}
    job["status"] = "stopped"
    proc = job.get("proc")
    if proc and proc.returncode is None:
        # Matar TODO el process group: Python CLI + Playwright driver + Chromium
        # + helpers. proc.terminate() solo señala al padre, lo cual es insuficiente
        # cuando hay tareas asyncio activas y procesos hijo que no responden.
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            # Fallback: si tras 3s el proceso sigue vivo, SIGKILL al grupo entero.
            asyncio.create_task(_force_kill_after(proc, pgid, delay=3.0))
        except (ProcessLookupError, PermissionError) as exc:
            # PGID ya muerto o sin permiso → último intento al proceso individual
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    _save_history()
    return {"status": "stopped"}


async def _force_kill_after(proc: asyncio.subprocess.Process, pgid: int, delay: float) -> None:
    """SIGKILL al process group si el proceso sigue vivo tras `delay` segundos."""
    await asyncio.sleep(delay)
    if proc.returncode is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@app.get("/history")
async def history() -> list:
    out_list = []
    for jid, j in reversed(list(jobs.items())):
        if not j.get("started_at"):
            continue
        ckpt = _checkpoint_path(jid)
        csv_exists = (BASE_DIR / j.get("output", "")).exists() if j.get("output") else False
        resumable = (
            j["status"] in {"partial", "stopped", "error"}
            and ckpt.exists() and csv_exists and bool(j.get("args"))
        )
        out_list.append({
            "job_id": jid,
            "city": j.get("city", ""),
            "category": j.get("category", ""),
            "started_at": j.get("started_at", ""),
            "status": j["status"],
            "valid_count": j.get("valid_count", 0),
            "output": j.get("output", ""),
            "checkpoint_available": resumable,
        })
    return out_list


@app.post("/resume/{job_id}")
async def resume_job(job_id: str) -> dict:
    """Reanuda un job en estado partial/stopped/error desde su checkpoint."""
    job = jobs.get(job_id)
    if not job:
        return {"error": "job not found"}
    if job["status"] == "running":
        return {"error": "job ya está en ejecución"}
    if job["status"] not in {"partial", "stopped", "error"}:
        return {"error": f"job en estado no reanudable: {job['status']}"}

    ckpt_path = _checkpoint_path(job_id)
    if not ckpt_path.exists():
        return {"error": "checkpoint no encontrado — este job no es reanudable"}
    output = job.get("output", "")
    if not output or not (BASE_DIR / output).exists():
        return {"error": "CSV de salida no encontrado"}
    args_dict = job.get("args") or {}
    if not args_dict:
        return {"error": "args originales no persistidos — no se puede reconstruir el comando"}

    # Resetear estado de ejecución (conservar valid_count previo y output).
    job["status"] = "running"
    job["proc"] = None
    job["lines"].append(f"── Reanudando desde checkpoint: {ckpt_path.name} ──")

    cmd = _build_cli_command(args_dict, output, resume_checkpoint=str(ckpt_path))
    asyncio.create_task(_run_subprocess(job_id, cmd))
    return {"job_id": job_id, "output": output, "resumed": True}


@app.post("/open-folder/{job_id}")
async def open_folder(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    output_path = BASE_DIR / job["output"]
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", "-R", str(output_path)])
        elif system == "Windows":
            subprocess.Popen(["explorer", str(output_path.parent)])
        else:
            subprocess.Popen(["xdg-open", str(output_path.parent)])
        return {"status": "ok"}
    except Exception as exc:
        return {"error": str(exc)}


@app.delete("/history/{job_id}")
async def delete_history_entry(job_id: str) -> dict:
    """Elimina un job del historial. Borra también su checkpoint y CSV asociados.

    Si el job está en ejecución (`running`) se rechaza — primero hay que parar.
    """
    job = jobs.get(job_id)
    if not job:
        return {"error": "job not found"}
    if job.get("status") == "running":
        return {"error": "el job está en ejecución; deténlo antes de borrarlo"}

    # Borrar artefactos del disco (best-effort)
    csv_path = BASE_DIR / job.get("output", "") if job.get("output") else None
    ckpt_path = _checkpoint_path(job_id)
    xlsx_path = (
        Path(str(csv_path).replace(".csv", ".xlsx")) if csv_path else None
    )
    for p in (csv_path, ckpt_path, xlsx_path):
        if p and p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    jobs.pop(job_id, None)
    analyze_jobs.pop(job_id, None)
    _save_history()
    return {"status": "ok", "deleted": job_id}


@app.get("/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if not job:
        return FileResponse("/dev/null")  # fallback; should not happen
    output_path = BASE_DIR / job["output"]
    return FileResponse(str(output_path), filename=output_path.name, media_type="text/csv")
