from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class BusinessRecord:
    nombre: str
    telefono: str
    direccion: str
    web: str
    rating: str
    categoria: str
    source_query: str
    retrieved_at_utc: str
    maps_url: str
    municipio_origen: str = ""
    lat: str = ""
    lon: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
