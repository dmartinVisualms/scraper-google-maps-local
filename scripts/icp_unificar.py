"""Une la pestaña Análisis de todos los out/*_icp.xlsx en out/icp_unificado.xlsx.

Uso: python -m scripts.icp_unificar
Añade al principio la columna «fichero» (origen de cada fila).
"""
from pathlib import Path

import openpyxl

OUT = Path(__file__).resolve().parents[1] / "out"
DEST = OUT / "icp_unificado.xlsx"


def main() -> None:
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Análisis")
    cabecera = None
    n = 0
    for f in sorted(OUT.glob("*_icp.xlsx")):
        src = openpyxl.load_workbook(f, read_only=True)["Análisis"]
        it = src.iter_rows(values_only=True)
        h = list(next(it))
        if cabecera is None:
            cabecera = h
            ws.append(["fichero"] + cabecera)
        idx = [h.index(c) if c in h else None for c in cabecera]  # ponytail: columnas del primer fichero
        nombre = f.name.replace("_icp.xlsx", "")
        for r in it:
            ws.append([nombre] + [r[i] if i is not None else None for i in idx])
            n += 1
    wb.save(DEST)
    print(f"{DEST}: {n} filas")


if __name__ == "__main__":
    main()
