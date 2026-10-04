"""Formatos de presentación (pesos chilenos y fechas en español)."""
from __future__ import annotations

import math

import pandas as pd

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def clp(valor) -> str:
    """1234567 -> '$1.234.567' (negativos: '-$1.234')."""
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return "—"
    n = int(round(float(valor)))
    signo = "-" if n < 0 else ""
    return f"{signo}${abs(n):,}".replace(",", ".")


def clp_corto(valor) -> str:
    """Versión compacta para títulos y ejes: $1,2 M o $350 mil."""
    v = float(valor)
    signo = "-" if v < 0 else ""
    n = abs(v)
    if n >= 1_000_000:
        return f"{signo}${n / 1_000_000:.1f} M".replace(".", ",")
    if n >= 1_000:
        return f"{signo}${n / 1_000:.0f} mil"
    return f"{signo}${n:.0f}"


def num1(valor) -> str:
    """Un decimal con coma: 0.3 -> '0,3'."""
    return f"{float(valor):.1f}".replace(".", ",")


def md_seguro(texto: str) -> str:
    """Escapa el signo $ para que Streamlit no lo interprete como fórmula LaTeX ('$1.000 y $2.000' se vería como matemática)."""
    return str(texto).replace("$", "\\$")


def fecha_corta(fecha) -> str:
    """Timestamp -> '10 nov'."""
    f = pd.Timestamp(fecha)
    return f"{f.day} {MESES[f.month - 1]}"
