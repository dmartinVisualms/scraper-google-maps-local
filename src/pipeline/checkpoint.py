"""Persistencia de progreso para reanudar jobs colgados.

Cada job tiene un fichero `out/{job_id}.checkpoint.json` con:
- `args`: argumentos originales del RunRequest, para reconstruir la línea de comandos.
- `output_csv`: ruta del CSV asociado al job.
- `completed_municipios`: lista de labels de municipios cuyo procesado terminó.
- `current_municipio`: estado parcial del municipio en curso (texto + sectores leaf/subdivided).

Granularidad híbrida (alineada con `_select_strategy`):
- Municipios text-only (< 15k hab) → checkpoint atómico de municipio.
- Municipios con grid (≥ 15k hab) → checkpoint por sector dentro del municipio.

La escritura es atómica: tmpfile + os.replace().
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOGGER = logging.getLogger(__name__)

SectorKey = Tuple[float, float, float]  # (lat, lon, cell_deg) redondeado a 6 decimales


def _round(v: float) -> float:
    return round(float(v), 6)


def sector_key(lat: float, lon: float, cell_deg: float) -> SectorKey:
    return (_round(lat), _round(lon), _round(cell_deg))


@dataclass
class CityResumeState:
    """Estado parcial de un municipio para reanudar.

    `completed_sectors` mapea SectorKey → "leaf" (sector terminado sin subdividir)
    o "subdivided" (sector que generó hijos; al reanudar, se salta su búsqueda
    pero se vuelven a iterar los hijos).
    """
    label: str
    poblacion: int = 0
    text_done: bool = False
    completed_sectors: Dict[SectorKey, str] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "poblacion": self.poblacion,
            "text_done": self.text_done,
            "completed_sectors": [
                {"lat": k[0], "lon": k[1], "cell": k[2], "mode": v}
                for k, v in self.completed_sectors.items()
            ],
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "CityResumeState":
        sectors: Dict[SectorKey, str] = {}
        for s in data.get("completed_sectors", []):
            key = sector_key(s["lat"], s["lon"], s["cell"])
            sectors[key] = s.get("mode", "leaf")
        return cls(
            label=data["label"],
            poblacion=int(data.get("poblacion", 0) or 0),
            text_done=bool(data.get("text_done", False)),
            completed_sectors=sectors,
        )


class CheckpointStore:
    """Almacén de checkpoint con escritura atómica y lock asíncrono."""

    def __init__(self, path: Path, data: Dict[str, Any]) -> None:
        self._path = Path(path)
        self._data = data
        # Lazy: el Lock necesita un event loop activo. En Python 3.9, crearlo
        # desde un constructor llamado fuera de coroutine falla.
        self._lock: Optional[asyncio.Lock] = None
        # Caché en memoria del estado del municipio actual
        cm = data.get("current_municipio")
        self._current: Optional[CityResumeState] = (
            CityResumeState.from_json(cm) if cm else None
        )

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ── Constructores ──────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        path: Path,
        args: Dict[str, Any],
        output_csv: str,
        job_id: str = "",
    ) -> "CheckpointStore":
        """Crea un checkpoint nuevo y lo persiste a disco."""
        data: Dict[str, Any] = {
            "job_id": job_id,
            "args": args,
            "output_csv": output_csv,
            "completed_municipios": [],
            "current_municipio": None,
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(path, data)
        store._flush_sync()
        return store

    @classmethod
    def load(cls, path: Path) -> "CheckpointStore":
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path, data)

    # ── Lectura ────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def args(self) -> Dict[str, Any]:
        return dict(self._data.get("args", {}))

    @property
    def output_csv(self) -> str:
        return self._data.get("output_csv", "")

    @property
    def completed_municipios(self) -> set:
        return set(self._data.get("completed_municipios", []))

    def is_municipio_done(self, label: str) -> bool:
        return label in self.completed_municipios

    def get_resume_state(self, label: str) -> Optional[CityResumeState]:
        """Devuelve el estado parcial si el `current_municipio` coincide con `label`."""
        if self._current and self._current.label == label:
            return self._current
        return None

    def sector_state(self, sector_key_: SectorKey) -> Optional[str]:
        """Devuelve 'leaf', 'subdivided' o None para un sector dado del municipio actual."""
        if not self._current:
            return None
        return self._current.completed_sectors.get(sector_key_)

    # ── Mutaciones ─────────────────────────────────────────────────────────

    async def start_municipio(self, label: str, poblacion: int) -> CityResumeState:
        """Marca el inicio del procesado de un municipio (idempotente)."""
        async with self._get_lock():
            if self._current and self._current.label == label:
                # Resume: conservamos el estado parcial.
                return self._current
            self._current = CityResumeState(label=label, poblacion=poblacion)
            self._data["current_municipio"] = self._current.to_json()
            await self._flush()
            return self._current

    async def mark_text_done(self, label: str) -> None:
        async with self._get_lock():
            if self._current and self._current.label == label:
                self._current.text_done = True
                self._data["current_municipio"] = self._current.to_json()
                await self._flush()

    async def mark_sector_done(
        self, label: str, lat: float, lon: float, cell_deg: float, mode: str = "leaf",
    ) -> None:
        """`mode`: 'leaf' (sector terminado sin subdividir) o 'subdivided' (generó hijos)."""
        if mode not in {"leaf", "subdivided"}:
            raise ValueError(f"mode inválido: {mode}")
        async with self._get_lock():
            if not (self._current and self._current.label == label):
                return
            key = sector_key(lat, lon, cell_deg)
            self._current.completed_sectors[key] = mode
            self._data["current_municipio"] = self._current.to_json()
            await self._flush()

    async def mark_municipio_done(self, label: str) -> None:
        async with self._get_lock():
            done = list(self._data.get("completed_municipios", []))
            if label not in done:
                done.append(label)
            self._data["completed_municipios"] = done
            if self._current and self._current.label == label:
                self._current = None
                self._data["current_municipio"] = None
            await self._flush()

    # ── Persistencia ───────────────────────────────────────────────────────

    async def _flush(self) -> None:
        await asyncio.to_thread(self._flush_sync)

    def _flush_sync(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)
