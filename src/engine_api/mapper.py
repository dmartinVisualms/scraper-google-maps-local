from __future__ import annotations

from typing import Optional

from src.domain import BusinessRecord


def _localized_text(value: Optional[dict]) -> str:
    if not value:
        return ""
    return value.get("text", "") or ""


def map_place(
    place: dict,
    *,
    source_query: str,
    municipio_origen: str,
    retrieved_at_utc: str,
) -> BusinessRecord:
    telefono = (
        place.get("nationalPhoneNumber")
        or place.get("internationalPhoneNumber")
        or ""
    )
    rating = place.get("rating")
    rating_str = "" if rating is None else str(rating)
    loc = place.get("location") or {}
    lat = "" if loc.get("latitude") is None else str(loc["latitude"])
    lon = "" if loc.get("longitude") is None else str(loc["longitude"])
    return BusinessRecord(
        nombre=_localized_text(place.get("displayName")),
        telefono=telefono,
        direccion=place.get("formattedAddress", "") or "",
        web=place.get("websiteUri", "") or "",
        rating=rating_str,
        categoria=_localized_text(place.get("primaryTypeDisplayName")),
        source_query=source_query,
        retrieved_at_utc=retrieved_at_utc,
        maps_url=place.get("googleMapsUri", "") or "",
        municipio_origen=municipio_origen,
        lat=lat,
        lon=lon,
    )
