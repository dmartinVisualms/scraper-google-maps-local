"""Resolución de la API key sin meterla en el código ni en git.

Prioridad: variable de entorno GOOGLE_MAPS_API_KEY > fichero .env local (gitignored).
ponytail: parser KEY=VALUE de 5 líneas; no merece python-dotenv.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"  # raíz del repo
_KEY = "GOOGLE_MAPS_API_KEY"


def _read_env_file(name: str, path: "Path | None" = None) -> str:
    path = path or _ENV_FILE
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def resolve_api_key() -> str:
    """Devuelve la key del entorno o, si no, del .env local. '' si no hay ninguna."""
    return os.environ.get(_KEY, "").strip() or _read_env_file(_KEY)


if __name__ == "__main__":  # ponytail: self-check
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / ".env").write_text('GOOGLE_MAPS_API_KEY="abc123"\n', encoding="utf-8")
    assert _read_env_file("GOOGLE_MAPS_API_KEY", d / ".env") == "abc123"
    print("secrets self-check OK")
