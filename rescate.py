"""Plan de rescate: qué hacer cuando la proyección muestra que la caja se queda corta."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .modelo import Parametros, proyectar

COLS_FACTURAS = [
    "contraparte", "monto", "fecha_esperada", "dias_adelanto", "adelanto_bruto", "costo", "neto_hoy",
]


@dataclass(frozen=True)
class Factoring:
    """Condiciones típicas de adelantar una factura (ajústalas a tu cotización real)."""

    tasa_mensual: float = 0.015  # 1,5% mensual sobre lo adelantado
    anticipo: float = 0.90  # te adelantan el 90%; el resto llega cuando el cliente paga
    comision_fija: float = 0.0  # por factura
    dias_desembolso: int = 2  # días hasta recibir la plata


@dataclass
class PlanAdelantos:
    facturas: pd.DataFrame
    costo_total: float
    saldo_min_antes: float
    saldo_min_despues: float
    resuelve: bool
    eventos_despues: pd.DataFrame

    @property
    def faltante_restante(self) -> float:
        return max(0.0, -self.saldo_min_despues)


@dataclass
class PlanPagos:
    pagos: pd.DataFrame
    saldo_min_antes: float
    saldo_min_despues: float
    eventos_despues: pd.DataFrame

    @property
    def mejora(self) -> float:
        return self.saldo_min_despues - self.saldo_min_antes


def costo_adelanto(monto: float, dias: float, f: Factoring) -> tuple[float, float, float]:
    """(adelanto bruto, costo, neto que llega hoy) al adelantar una factura `dias` días antes de su cobro."""
    bruto = monto * f.anticipo
    costo = bruto * f.tasa_mensual * dias / 30 + f.comision_fija
    return bruto, costo, bruto - costo


def _aplicar_adelanto(ev: pd.DataFrame, r, f: Factoring, fecha_desembolso: pd.Timestamp) -> pd.DataFrame:
    bruto, _, neto = costo_adelanto(r.monto, r.dias, f)
    nuevas = [
        {"fecha": fecha_desembolso, "monto": neto, "concepto": "Adelanto de factura",
         "contraparte": r.contraparte, "doc_id": r.doc_id}
    ]
    if r.monto - bruto > 0:
        nuevas.append(
            {"fecha": r.fecha, "monto": r.monto - bruto, "concepto": "Saldo de factura adelantada",
             "contraparte": r.contraparte, "doc_id": r.doc_id}
        )
    ev = ev[~((ev["doc_id"] == r.doc_id) & (ev["concepto"] == "Cobro"))]
    return pd.concat([ev, pd.DataFrame(nuevas)], ignore_index=True)


def plan_adelantos(eventos: pd.DataFrame, params: Parametros, f: Factoring) -> PlanAdelantos:
    """Elige las facturas más baratas de adelantar hasta que la caja deje de quedar en negativo.

    Cada candidata se evalúa re-proyectando la caja completa, así el resultado es verificado,
    no una estimación. Costo = lo adelantado x tasa mensual x (días adelantados / 30).
    """
    base = proyectar(eventos, params)
    antes = float(base.diario["saldo"].min())
    if antes >= 0:
        return PlanAdelantos(pd.DataFrame(columns=COLS_FACTURAS), 0.0, antes, antes, True, eventos)

    primera_neg = base.diario.loc[base.diario["saldo"] < 0, "fecha"].iloc[0]
    fecha_desembolso = params.hoy + pd.Timedelta(days=f.dias_desembolso)
    cand = eventos[
        (eventos["concepto"] == "Cobro") & (eventos["fecha"] > max(primera_neg, fecha_desembolso))
    ].copy()
    if cand.empty:
        return PlanAdelantos(pd.DataFrame(columns=COLS_FACTURAS), 0.0, antes, antes, False, eventos)

    cand["dias"] = (cand["fecha"] - fecha_desembolso).dt.days
    cand["bruto"] = cand["monto"] * f.anticipo
    cand["costo"] = cand["bruto"] * f.tasa_mensual * cand["dias"] / 30 + f.comision_fija
    cand["costo_pct"] = cand["costo"] / cand["bruto"]
    cand = cand.sort_values(["costo_pct", "monto"], ascending=[True, False])

    ev, elegidas, saldo_min = eventos.copy(), [], antes
    for r in cand.itertuples():
        ev = _aplicar_adelanto(ev, r, f, fecha_desembolso)
        elegidas.append(r)
        saldo_min = float(proyectar(ev, params).diario["saldo"].min())
        if saldo_min >= 0:
            break

    tabla = pd.DataFrame(
        [
            {
                "contraparte": r.contraparte,
                "monto": r.monto,
                "fecha_esperada": r.fecha,
                "dias_adelanto": r.dias,
                "adelanto_bruto": r.bruto,
                "costo": r.costo,
                "neto_hoy": r.bruto - r.costo,
            }
            for r in elegidas
        ],
        columns=COLS_FACTURAS,
    )
    return PlanAdelantos(tabla, float(tabla["costo"].sum()), antes, saldo_min, saldo_min >= 0, ev)


def efecto_posponer_pagos(
    eventos: pd.DataFrame, params: Parametros, dias: int = 15, max_pagos: int = 3
) -> PlanPagos | None:
    """¿Cuánto mejora la caja si negocias más plazo en los pagos grandes que pesan antes del peor momento?"""
    base = proyectar(eventos, params)
    d = base.diario
    antes = float(d["saldo"].min())
    if antes >= 0:
        return None
    fecha_min = d.loc[d["saldo"].idxmin(), "fecha"]
    pagos = eventos[(eventos["concepto"] == "Pago a proveedor") & (eventos["fecha"] <= fecha_min)]
    pagos = pagos.nlargest(max_pagos, "monto")
    if pagos.empty:
        return None
    ev = eventos.copy()
    ev.loc[pagos.index, "fecha"] = ev.loc[pagos.index, "fecha"] + pd.Timedelta(days=dias)
    despues = float(proyectar(ev, params).diario["saldo"].min())
    tabla = pagos[["contraparte", "monto", "fecha"]].rename(columns={"fecha": "vence"}).copy()
    tabla["nueva_fecha"] = tabla["vence"] + pd.Timedelta(days=dias)
    return PlanPagos(tabla.reset_index(drop=True), antes, despues, ev)
