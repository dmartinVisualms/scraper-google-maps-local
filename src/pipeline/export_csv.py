from __future__ import annotations

import csv
from pathlib import Path

from src.domain import BusinessRecord


FIELDNAMES = [
    "nombre",
    "telefono",
    "direccion",
    "web",
    "rating",
    "categoria",
    "source_query",
    "retrieved_at_utc",
    "maps_url",
    "municipio_origen",
    "lat",
    "lon",
]


def export_csv(path: str, records: list[BusinessRecord]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for record in records:
            writer.writerow(record.to_dict())
