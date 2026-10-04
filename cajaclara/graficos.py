"""Gráficos (Altair).

Colores: azul y naranja son los slots 1 y 2 de la paleta de referencia (validados como par adyacente);
el rojo es el color de estado "crítico" y siempre va acompañado de una etiqueta de texto.
"""
from __future__ import annotations

import altair as alt
import pandas as pd

from .cobranza import TRAMOS
from .formato import clp, fecha_corta

AZUL = "#2a78d6"
NARANJA = "#eb6834"
ROJO = "#d03b3b"
GRIS = "#898781"
RAMPA_ANTIGUEDAD = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]

_LABEL_PESOS = (
    "datum.value == 0 ? '$0' : (datum.value < 0 ? '-' : '') + '$' + "
    "replace(format(abs(datum.value) / 1000000, '.1f'), '.', ',') + ' M'"
)


def _eje_pesos() -> alt.Axis:
    return alt.Axis(title=None, labelExpr=_LABEL_PESOS, grid=True, gridOpacity=0.25)


def grafico_saldo(diario: pd.DataFrame) -> alt.LayerChart:
    """Saldo de caja día a día; la zona en negativo se pinta aparte y el punto más bajo lleva etiqueta."""
    d = diario.copy()
    d["saldo_pos"] = d["saldo"].clip(lower=0)
    d["saldo_neg"] = d["saldo"].clip(upper=0)
    d["saldo_txt"] = d["saldo"].map(clp)
    d["fecha_txt"] = d["fecha"].map(fecha_corta)

    x = alt.X("fecha:T", axis=alt.Axis(title=None, format="%d/%m", tickCount=8, labelOverlap=True))
    base = alt.Chart(d)
    pos = base.mark_area(color=AZUL, opacity=0.16).encode(x=x, y=alt.Y("saldo_pos:Q", axis=_eje_pesos()), y2=alt.datum(0))
    neg = base.mark_area(color=ROJO, opacity=0.35).encode(x=x, y=alt.Y("saldo_neg:Q", axis=_eje_pesos()), y2=alt.datum(0))
    linea = base.mark_line(color=AZUL, strokeWidth=2).encode(x=x, y=alt.Y("saldo:Q", axis=_eje_pesos()))
    hover = base.mark_circle(opacity=0, size=160).encode(
        x=x,
        y="saldo:Q",
        tooltip=[alt.Tooltip("fecha_txt:N", title="Fecha"), alt.Tooltip("saldo_txt:N", title="Saldo")],
    )
    cero = (
        alt.Chart(pd.DataFrame({"cero": [0]}))
        .mark_rule(color=GRIS, strokeDash=[4, 3])
        .encode(y="cero:Q")
    )

    imin = d["saldo"].idxmin()
    fila = d.loc[imin]
    negativo = fila["saldo"] < 0
    mitad_derecha = imin > len(d) * 0.6
    punto = pd.DataFrame(
        [
            {
                "fecha": fila["fecha"],
                "saldo": fila["saldo"],
                "texto": (f"Faltan {clp(-fila['saldo'])}" if negativo else f"Punto más bajo: {clp(fila['saldo'])}"),
            }
        ]
    )
    marca = (
        alt.Chart(punto)
        .mark_point(filled=True, size=90, color=ROJO if negativo else AZUL, opacity=1)
        .encode(x="fecha:T", y="saldo:Q")
    )
    etiqueta = (
        alt.Chart(punto)
        .mark_text(
            align="right" if mitad_derecha else "left",
            dx=-10 if mitad_derecha else 10,
            dy=16 if negativo else -12,
            color=GRIS,
            fontWeight="bold",
            fontSize=12,
        )
        .encode(x="fecha:T", y="saldo:Q", text="texto:N")
    )
    return alt.layer(pos, neg, cero, linea, marca, etiqueta, hover).properties(height=320)


def grafico_flujo(semanal: pd.DataFrame) -> alt.Chart:
    """Entradas (hacia arriba) y salidas (hacia abajo) de cada semana."""
    largo = pd.concat(
        [
            pd.DataFrame({"semana": semanal["semana"], "tipo": "Entradas", "valor": semanal["entradas"]}),
            pd.DataFrame({"semana": semanal["semana"], "tipo": "Salidas", "valor": -semanal["salidas"]}),
        ]
    )
    largo["monto_txt"] = largo["valor"].abs().map(clp)
    return (
        alt.Chart(largo)
        .mark_bar(size=18, cornerRadiusEnd=3)
        .encode(
            x=alt.X("semana:O", axis=alt.Axis(title="Semana", labelAngle=0)),
            y=alt.Y("valor:Q", axis=_eje_pesos()),
            color=alt.Color(
                "tipo:N",
                scale=alt.Scale(domain=["Entradas", "Salidas"], range=[AZUL, NARANJA]),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip("semana:O", title="Semana"),
                alt.Tooltip("tipo:N", title="Tipo"),
                alt.Tooltip("monto_txt:N", title="Monto"),
            ],
        )
        .properties(height=240)
    )


def grafico_antiguedad(aging: pd.DataFrame) -> alt.LayerChart:
    """Cuánto te deben según cuántos días llevan vencidas."""
    d = aging.copy()
    d["monto_txt"] = d["monto"].map(clp)
    d["tramo"] = d["tramo"].astype(str)
    barras = (
        alt.Chart(d)
        .mark_bar(cornerRadiusEnd=3, size=22)
        .encode(
            y=alt.Y("tramo:N", sort=TRAMOS, axis=alt.Axis(title=None)),
            x=alt.X("monto:Q", axis=alt.Axis(title=None, labelExpr=_LABEL_PESOS, grid=True, gridOpacity=0.25, tickCount=4)),
            color=alt.Color("tramo:N", scale=alt.Scale(domain=TRAMOS, range=RAMPA_ANTIGUEDAD), legend=None),
            tooltip=[
                alt.Tooltip("tramo:N", title="Tramo"),
                alt.Tooltip("documentos:Q", title="Facturas"),
                alt.Tooltip("monto_txt:N", title="Monto"),
            ],
        )
    )
    texto = (
        alt.Chart(d[d["monto"] > 0])
        .mark_text(align="left", dx=6, color=GRIS, fontSize=12)
        .encode(y=alt.Y("tramo:N", sort=TRAMOS), x="monto:Q", text="monto_txt:N")
    )
    return alt.layer(barras, texto).properties(height=220)


def grafico_comparado(antes: pd.DataFrame, despues: pd.DataFrame, nombre_despues: str = "Con el plan") -> alt.LayerChart:
    """Saldo con y sin el plan de rescate."""
    a = pd.DataFrame({"fecha": antes["fecha"], "saldo": antes["saldo"], "serie": "Sin hacer nada"})
    b = pd.DataFrame({"fecha": despues["fecha"], "saldo": despues["saldo"], "serie": nombre_despues})
    d = pd.concat([a, b])
    d["saldo_txt"] = d["saldo"].map(clp)
    d["fecha_txt"] = d["fecha"].map(fecha_corta)
    lineas = (
        alt.Chart(d)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("fecha:T", axis=alt.Axis(title=None, format="%d/%m", tickCount=8, labelOverlap=True)),
            y=alt.Y("saldo:Q", axis=_eje_pesos()),
            color=alt.Color(
                "serie:N",
                scale=alt.Scale(domain=["Sin hacer nada", nombre_despues], range=[AZUL, NARANJA]),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip("fecha_txt:N", title="Fecha"),
                alt.Tooltip("serie:N", title="Escenario"),
                alt.Tooltip("saldo_txt:N", title="Saldo"),
            ],
        )
    )
    cero = alt.Chart(pd.DataFrame({"cero": [0]})).mark_rule(color=GRIS, strokeDash=[4, 3]).encode(y="cero:Q")
    return alt.layer(cero, lineas).properties(height=260)
