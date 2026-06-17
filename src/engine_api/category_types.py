"""Mapeo categoría (texto libre, español) -> tipo de Google Places (Table A).

Se usa para `includedType` + `strictTypeFiltering` en modo API, derivado del
campo CATEGORÍA. Si no hay match, se devuelve None -> sin filtro estricto
(más resultados, menos precisos). Claves = stems en minúscula (substring),
así "zapater" casa con "zapatería"/"zapaterías".
"""
from __future__ import annotations

from typing import Optional

# stem -> tipo Google (solo tipos válidos de la New API)
CATEGORY_TYPE_MAP = {
    "zapater": "shoe_store",
    "calzado": "shoe_store",
    "ropa": "clothing_store",
    "moda": "clothing_store",
    "restaurant": "restaurant",
    "bar": "bar",
    "cafeter": "cafe",
    "café": "cafe",
    "cafe": "cafe",
    "farmacia": "pharmacy",
    "supermercado": "supermarket",
    "peluquer": "hair_salon",
    "ferreter": "hardware_store",
    "librer": "book_store",
    "joyer": "jewelry_store",
    "mueble": "furniture_store",
    "gimnasio": "gym",
    "hotel": "lodging",
    "panader": "bakery",
    "florister": "florist",
}


def guess_type(category: str) -> Optional[str]:
    """Devuelve el tipo Google para una categoría, o None si no hay match."""
    c = (category or "").strip().lower()
    for stem, gtype in CATEGORY_TYPE_MAP.items():
        if stem in c:
            return gtype
    return None


if __name__ == "__main__":  # ponytail: self-check
    assert guess_type("zapaterías") == "shoe_store"
    assert guess_type("Tiendas de ropa") == "clothing_store"
    assert guess_type("restaurantes") == "restaurant"
    assert guess_type("notariios raros") is None
    print("category_types self-check OK")
