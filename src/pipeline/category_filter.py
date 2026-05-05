"""Filtrado de registros por coincidencia de categoría.

Compara la categoría extraída de Google Maps con el término de búsqueda,
normalizando tildes, mayúsculas y plurales simples en español.
"""
from __future__ import annotations

import unicodedata


def _normalize(text: str) -> str:
    """Minúsculas + elimina diacríticos (tildes)."""
    nfkd = unicodedata.normalize("NFKD", text.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _stem(text: str) -> str:
    """Normaliza y elimina 's' final para cubrir singular/plural español.
    Ej: 'zapaterías' → 'zapateria', 'tiendas' → 'tienda'.
    """
    return _normalize(text).rstrip("s")


def category_matches(search_term: str, record_category: str) -> bool:
    """True si `record_category` es compatible con `search_term`.

    Reglas:
    - Categoría vacía → acepta siempre (el negocio puede ser válido aunque
      Google Maps no tenga categoría asignada).
    - Categoría no vacía → acepta si el stem del término de búsqueda aparece
      como subcadena en la categoría normalizada.
    """
    if not record_category.strip():
        return True
    return _stem(search_term) in _normalize(record_category)
