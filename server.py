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


# job_id -> {"lines": [...], "status": "running"|"done"|"error"|"stopped", "output": str, "proc": Process|None}
jobs: dict[str, dict] = {}

# analyze_job_id (== scraping job_id) -> {"lines": [...], "status": "running"|"done"|"error", "proc": Process|None, "xlsx_output": str}
analyze_jobs: dict[str, dict] = {}

BRANDS_PATH = BASE_DIR / "config" / "excluded_brands.json"


def _count_csv_rows(output: str) -> int:
    """Cuenta las filas de datos (sin cabecera) del CSV. 0 si no existe."""
    if not output:
        return 0
    path = BASE_DIR / output
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            # -1 por la cabecera
            count = sum(1 for _ in fh) - 1
            return max(count, 0)
    except OSError:
        return 0


def _pid_alive(pid: Optional[int]) -> bool:
    """Comprueba si un PID sigue vivo. Devuelve False si pid es None o no existe."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def _load_history() -> None:
    """Carga el historial de ejecuciones desde disco al arrancar el servidor."""
    if not HISTORY_PATH.exists():
        return
    try:
        entries = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return  # fichero corrupto — arrancar sin historial

    try:
        comunidades_set = {c.lower() for c in list_comunidades()}
    except Exception:
        comunidades_set = set()

    for entry in entries:
        params = entry.get("params") or {}
        # Retrocompat: entradas previas a v0.8.2 no guardan params.
        # Si el campo "city" coincide con una comunidad conocida, deducimos modo comunidad.
        if not params:
            city_field = (entry.get("city") or "").strip()
            if city_field.lower() in comunidades_set:
                params = {
                    "comunidad": city_field,
                    "category": entry.get("category", ""),
                }
        status = entry.get("status", "done")
        pid = entry.get("pid")
        # Orphan recovery: si está marcado "running" pero el PID no existe,
        # el subprocess murió (probablemente al reiniciar uvicorn). Marcar
        # como "stopped" para que se pueda reanudar/borrar desde la UI.
        if status == "running" and not _pid_alive(pid):
            status = "stopped"
            pid = None
        # Reconciliar valid_count con el CSV real para entradas terminadas
        # (running se mantiene para no incurrir en lectura constante).
        valid_count = entry.get("valid_count", 0)
        if status != "running":
            real = _count_csv_rows(entry.get("output", ""))
            if real > 0:
                valid_count = real
        jobs[entry["job_id"]] = {
            "city": entry.get("city", ""),
            "category": entry.get("category", ""),
            "started_at": entry.get("started_at", ""),
            "status": status,
            "valid_count": valid_count,
            "output": entry.get("output", ""),
            "params": params,
            "pid": pid,
            "lines": [],
            "proc": None,
        }


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
            "params": j.get("params") or {},
            "pid": j.get("pid"),
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
    engine: str = Form("scraper"),
    included_type: Optional[str] = Form(None),
) -> dict:
    comunidad = (comunidad or "").strip() or None
    city = (city or "").strip() or None
    if not comunidad and not city:
        return {"error": "Debes indicar comunidad o ciudad"}

    job_id = str(uuid.uuid4())
    label_for_output = comunidad if comunidad else city
    output = _make_output_path(label_for_output, category)
    params = {
        "city": city,
        "comunidad": comunidad,
        "category": category,
        "headless": headless,
        "max_results": max_results,
        "slow_ms": slow_ms,
        "timeout_ms": timeout_ms,
        "concurrency": concurrency,
        "adaptive_subdivision": adaptive_subdivision,
        "min_poblacion": min_poblacion,
        "engine": engine,
        "included_type": (included_type or "").strip() or None,
    }
    jobs[job_id] = {
        "city": comunidad if comunidad else city,
        "category": category,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "valid_count": 0,
        "output": output,
        "params": params,
        "lines": [],
        "proc": None,
    }

    cmd = _build_scraper_cmd(params, output, resume_csv=None)
    asyncio.create_task(_spawn_scraper(job_id, cmd))
    return {"job_id": job_id, "output": output}


def _build_scraper_cmd(
    params: dict, output: str, resume_csv: Optional[str] = None,
) -> list:
    cmd = [
        sys.executable, "-u", "-m", "src.cli",
        "--category", params["category"],
        "--output", output,
        "--headless", str(params.get("headless", "true")),
        "--max-results", str(params.get("max_results", 0)),
        "--slow-ms", str(params.get("slow_ms", 250)),
        "--timeout-ms", str(params.get("timeout_ms", 15000)),
        "--concurrency", str(params.get("concurrency", 3)),
        "--adaptive-subdivision", str(params.get("adaptive_subdivision", "false")),
    ]
    if params.get("comunidad"):
        cmd += [
            "--comunidad", params["comunidad"],
            "--min-poblacion", str(params.get("min_poblacion", 5000)),
        ]
    elif params.get("city"):
        cmd += ["--city", params["city"]]
    if params.get("engine") == "api":
        cmd += ["--engine", "api"]
        if params.get("included_type"):
            cmd += ["--included-type", params["included_type"]]
    if resume_csv:
        cmd += ["--resume-csv", resume_csv]
    return cmd


async def _spawn_scraper(job_id: str, cmd: list) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(BASE_DIR),
    )
    jobs[job_id]["proc"] = proc
    jobs[job_id]["pid"] = proc.pid
    _save_history()  # persistir PID para que /stop pueda matarlo tras reload de uvicorn
    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        jobs[job_id]["lines"].append(line)
        m = re.search(r"valid=(\d+)", line)
        if m:
            jobs[job_id]["valid_count"] = int(m.group(1))
    await proc.wait()
    # Reconciliar valid_count con el CSV real (regex sobre el log puede ir
    # desincronizado si el subprocess muere a media línea o si hubo
    # múltiples spawns).
    real_count = _count_csv_rows(jobs[job_id].get("output", ""))
    jobs[job_id]["valid_count"] = real_count
    if jobs[job_id]["status"] != "stopped":
        if proc.returncode == 0:
            jobs[job_id]["status"] = "done"
        elif real_count > 0:
            jobs[job_id]["status"] = "partial"
        else:
            jobs[job_id]["status"] = "error"
    jobs[job_id]["pid"] = None
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


@app.post("/resume/{job_id}")
async def resume_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    if job.get("status") == "running":
        return {"error": "ya está en ejecución"}
    params = job.get("params") or {}
    if not params.get("comunidad"):
        return {"error": "sólo se pueden reanudar ejecuciones en modo comunidad"}
    output = job.get("output") or ""
    if not output or not (BASE_DIR / output).exists():
        return {"error": "no se encuentra el CSV original"}

    job["status"] = "running"
    job["lines"] = []
    job["proc"] = None
    job["valid_count"] = _count_csv_rows(output)
    cmd = _build_scraper_cmd(params, output, resume_csv=output)
    asyncio.create_task(_spawn_scraper(job_id, cmd))
    return {"job_id": job_id, "output": output}


@app.delete("/history/{job_id}")
async def delete_history(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    if job.get("status") == "running":
        return {"error": "no se puede borrar un job en ejecución"}
    jobs.pop(job_id, None)
    _save_history()
    return {"status": "ok"}


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
        proc.terminate()
    else:
        # Fallback: el proc puede no estar en memoria si uvicorn se reinició
        # mientras el subprocess seguía vivo. Matar por PID persistido.
        pid = job.get("pid")
        if _pid_alive(pid):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
    job["pid"] = None
    job["valid_count"] = _count_csv_rows(job.get("output", ""))
    _save_history()
    return {"status": "stopped"}


@app.get("/history")
async def history() -> list:
    return [
        {
            "job_id": jid,
            "city": j.get("city", ""),
            "category": j.get("category", ""),
            "started_at": j.get("started_at", ""),
            "status": j["status"],
            "valid_count": j.get("valid_count", 0),
            "output": j.get("output", ""),
            "params": j.get("params") or {},
        }
        for jid, j in reversed(list(jobs.items()))
        if j.get("started_at")
    ]


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


@app.get("/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if not job:
        return FileResponse("/dev/null")  # fallback; should not happen
    output_path = BASE_DIR / job["output"]
    return FileResponse(str(output_path), filename=output_path.name, media_type="text/csv")
