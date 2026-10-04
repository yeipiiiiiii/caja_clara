"""Motor de proyección de caja.

Idea central: en vez de asumir que cada cliente paga en la fecha de vencimiento,
aprendemos del historial cuántos días se atrasa cada uno y proyectamos la caja
día a día con esas fechas realistas.
"""
from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Escenario:
    nombre: str
    usar_historial: bool  # ¿usar el atraso aprendido de cada cliente?
    atraso_extra: int  # días de atraso adicionales para todos
    espera_vencidas: int  # días hasta cobrar una factura que ya debió llegar y no llegó
    excluir_vencidas_mas_de: int | None  # cobros más vencidos que esto no se cuentan


ESCENARIOS = {
    "Optimista": Escenario("Optimista", False, 0, 1, None),
    "Esperado": Escenario("Esperado", True, 0, 7, 90),
    "Pesimista": Escenario("Pesimista", True, 15, 7, 60),
}


@dataclass(frozen=True)
class Parametros:
    hoy: pd.Timestamp
    saldo_inicial: float
    semanas: int = 12
    escenario: Escenario = ESCENARIOS["Esperado"]
    extra_por_cliente: dict = field(default_factory=dict)  # "¿y si este cliente se atrasa X días más?"


@dataclass
class Proyeccion:
    diario: pd.DataFrame  # fecha, neto, saldo, semana
    semanal: pd.DataFrame  # semana, desde, hasta, entradas, salidas, saldo_final, saldo_minimo


@dataclass
class Resumen:
    saldo_inicial: float
    saldo_minimo: float
    fecha_minimo: pd.Timestamp
    semana_minimo: int
    primera_fecha_negativa: pd.Timestamp | None
    primera_semana_negativa: int | None
    brecha: float  # cuánta plata falta en el peor momento (0 si nunca falta)
    por_cobrar_total: float
    por_cobrar_vencido: float
    cobros_dudosos: float  # vencidos hace tanto que no los contamos en la proyección
    por_pagar_total: float
    pagos_proximos_30d: float
    salidas_semanales_promedio: float
    colchon_semanas: float | None  # cuántas semanas de gastos cubre el saldo de hoy
    entradas_en_horizonte: float

    def a_dict(self) -> dict:
        return asdict(self)


@dataclass
class Analisis:
    params: Parametros
    atrasos: pd.DataFrame
    atraso_global: int
    eventos: pd.DataFrame
    proyeccion: Proyeccion
    resumen: Resumen


def estimar_atrasos(docs: pd.DataFrame, k: float = 3.0) -> tuple[pd.DataFrame, int]:
    """Cuántos días se atrasa cada cliente respecto del vencimiento.

    Con pocos pagos históricos mezclamos la mediana del cliente con la general
    (peso k) para no sacar conclusiones de 1 o 2 facturas. Nunca bajamos de 0:
    pagar antes del vencimiento no adelanta la caja en nuestra proyección.
    """
    cols = ["contraparte", "n_pagos", "atraso_mediano", "atraso_estimado"]
    pagados = docs[(docs["tipo"] == "cobro") & docs["fecha_pago"].notna()].copy()
    if pagados.empty:
        return pd.DataFrame(columns=cols), 0
    pagados["atraso"] = (pagados["fecha_pago"] - pagados["fecha_vencimiento"]).dt.days
    global_med = float(pagados["atraso"].median())
    g = (
        pagados.groupby("contraparte")["atraso"]
        .agg(n_pagos="count", atraso_mediano="median")
        .reset_index()
    )
    g["atraso_estimado"] = (
        ((g["n_pagos"] * g["atraso_mediano"] + k * global_med) / (g["n_pagos"] + k))
        .round()
        .clip(lower=0)
        .astype(int)
    )
    g["atraso_mediano"] = g["atraso_mediano"].round(1)
    return g[cols].sort_values("atraso_estimado", ascending=False).reset_index(drop=True), max(0, int(round(global_med)))


def _fechas_mensuales(dia: int, hoy: pd.Timestamp, fin: pd.Timestamp) -> list[pd.Timestamp]:
    """Fechas en que cae un gasto mensual entre hoy y fin (el día 31 pasa al último día del mes)."""
    fechas = []
    y, m = hoy.year, hoy.month
    while (y, m) <= (fin.year, fin.month):
        d = min(dia, calendar.monthrange(y, m)[1])
        f = pd.Timestamp(year=y, month=m, day=d)
        if hoy <= f <= fin:
            fechas.append(f)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return fechas


def construir_eventos(
    docs: pd.DataFrame,
    gastos: pd.DataFrame,
    params: Parametros,
    atrasos: pd.DataFrame,
    atraso_global: int,
    ingresos: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Lista de movimientos futuros de plata: fecha, monto (+entra / -sale), concepto, contraparte, doc_id."""
    esc, hoy = params.escenario, params.hoy
    mapa = dict(zip(atrasos["contraparte"], atrasos["atraso_estimado"])) if len(atrasos) else {}
    filas = []
    for r in docs[docs["fecha_pago"].isna()].itertuples(index=False):
        if r.tipo == "cobro":
            vencida = (hoy - r.fecha_vencimiento).days
            if esc.excluir_vencidas_mas_de is not None and vencida > esc.excluir_vencidas_mas_de:
                continue
            extra = esc.atraso_extra + int(params.extra_por_cliente.get(r.contraparte, 0))
            base = mapa.get(r.contraparte, atraso_global) if esc.usar_historial else 0
            esperada = r.fecha_vencimiento + pd.Timedelta(days=int(base))
            # Si ya debió llegar y no llegó, esperamos `espera_vencidas` días desde hoy.
            # Los días extra se suman al final: así Pesimista nunca cobra antes que Esperado.
            fecha = (esperada if esperada > hoy else hoy + pd.Timedelta(days=esc.espera_vencidas)) + pd.Timedelta(
                days=extra
            )
            filas.append((fecha, float(r.monto), "Cobro", r.contraparte, int(r.doc_id)))
        else:
            fecha = max(r.fecha_vencimiento, hoy)  # lo que ya venció se paga hoy
            filas.append((fecha, -float(r.monto), "Pago a proveedor", r.contraparte, int(r.doc_id)))

    fin = hoy + pd.Timedelta(days=7 * params.semanas - 1)
    for g in gastos.itertuples(index=False):
        for f in _fechas_mensuales(int(g.dia_mes), hoy, fin):
            filas.append((f, -float(g.monto), "Gasto fijo", g.concepto, -1))
    if ingresos is not None:
        for g in ingresos.itertuples(index=False):
            for f in _fechas_mensuales(int(g.dia_mes), hoy, fin):
                filas.append((f, float(g.monto), "Ingreso esperado", g.concepto, -1))

    ev = pd.DataFrame(filas, columns=["fecha", "monto", "concepto", "contraparte", "doc_id"])
    ev["fecha"] = pd.to_datetime(ev["fecha"])
    ev["monto"] = ev["monto"].astype(float)
    ev["doc_id"] = ev["doc_id"].astype(int)
    return ev.sort_values("fecha", kind="stable").reset_index(drop=True)


def proyectar(eventos: pd.DataFrame, params: Parametros) -> Proyeccion:
    """Saldo día a día y resumen por semana (semana 1 = hoy y los 6 días siguientes)."""
    dias = pd.date_range(params.hoy, periods=7 * params.semanas, freq="D")
    ev = eventos[(eventos["fecha"] >= dias[0]) & (eventos["fecha"] <= dias[-1])]
    neto = ev.groupby("fecha")["monto"].sum().reindex(dias, fill_value=0.0)
    saldo = params.saldo_inicial + neto.cumsum()
    diario = pd.DataFrame(
        {
            "fecha": dias,
            "neto": neto.to_numpy(),
            "saldo": saldo.to_numpy(),
            "semana": np.arange(len(dias)) // 7 + 1,
        }
    )
    sem = diario.groupby("semana").agg(
        desde=("fecha", "min"),
        hasta=("fecha", "max"),
        saldo_final=("saldo", "last"),
        saldo_minimo=("saldo", "min"),
    )
    ev = ev.assign(semana=((ev["fecha"] - dias[0]).dt.days // 7) + 1)
    entradas = ev[ev["monto"] > 0].groupby("semana")["monto"].sum()
    salidas = -ev[ev["monto"] < 0].groupby("semana")["monto"].sum()
    sem["entradas"] = entradas.reindex(sem.index, fill_value=0.0)
    sem["salidas"] = salidas.reindex(sem.index, fill_value=0.0)
    return Proyeccion(diario=diario, semanal=sem.reset_index())


def resumir(proy: Proyeccion, docs: pd.DataFrame, params: Parametros) -> Resumen:
    d = proy.diario
    hoy, esc = params.hoy, params.escenario
    imin = d["saldo"].idxmin()
    saldo_min = float(d.loc[imin, "saldo"])
    neg = d[d["saldo"] < 0]

    pend_c = docs[(docs["tipo"] == "cobro") & docs["fecha_pago"].isna()]
    vencidos = pend_c[pend_c["fecha_vencimiento"] < hoy]
    dias_venc = (hoy - pend_c["fecha_vencimiento"]).dt.days
    if esc.excluir_vencidas_mas_de is not None:
        dudosos = pend_c.loc[dias_venc > esc.excluir_vencidas_mas_de, "monto"].sum()
    else:
        dudosos = 0.0
    pend_p = docs[(docs["tipo"] == "pago") & docs["fecha_pago"].isna()]
    prox30 = pend_p[pend_p["fecha_vencimiento"] <= hoy + pd.Timedelta(days=30)]

    salidas_prom = float(proy.semanal["salidas"].mean())
    return Resumen(
        saldo_inicial=float(params.saldo_inicial),
        saldo_minimo=saldo_min,
        fecha_minimo=d.loc[imin, "fecha"],
        semana_minimo=int(d.loc[imin, "semana"]),
        primera_fecha_negativa=neg["fecha"].iloc[0] if len(neg) else None,
        primera_semana_negativa=int(neg["semana"].iloc[0]) if len(neg) else None,
        brecha=max(0.0, -saldo_min),
        por_cobrar_total=float(pend_c["monto"].sum()),
        por_cobrar_vencido=float(vencidos["monto"].sum()),
        cobros_dudosos=float(dudosos),
        por_pagar_total=float(pend_p["monto"].sum()),
        pagos_proximos_30d=float(prox30["monto"].sum()),
        salidas_semanales_promedio=salidas_prom,
        colchon_semanas=(params.saldo_inicial / salidas_prom) if salidas_prom > 0 else None,
        entradas_en_horizonte=float(proy.semanal["entradas"].sum()),
    )


def analizar(
    docs: pd.DataFrame, gastos: pd.DataFrame, params: Parametros, ingresos: pd.DataFrame | None = None
) -> Analisis:
    """Todo el pipeline: aprender atrasos -> armar movimientos -> proyectar -> resumir."""
    atrasos, atraso_global = estimar_atrasos(docs)
    eventos = construir_eventos(docs, gastos, params, atrasos, atraso_global, ingresos)
    proy = proyectar(eventos, params)
    return Analisis(params, atrasos, atraso_global, eventos, proy, resumir(proy, docs, params))
