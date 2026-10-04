"""Cobranza: antigüedad de la cartera, a quién cobrar primero y mensajes listos para enviar."""
from __future__ import annotations

import pandas as pd

from .formato import clp, fecha_corta

TRAMOS = ["Por vencer", "1-30 días", "31-60 días", "61-90 días", "Más de 90 días"]


def _pendientes_cobro(docs: pd.DataFrame) -> pd.DataFrame:
    return docs[(docs["tipo"] == "cobro") & docs["fecha_pago"].isna()].copy()


def antiguedad_cartera(docs: pd.DataFrame, hoy: pd.Timestamp) -> pd.DataFrame:
    """Cuánta plata te deben, agrupada por días de atraso."""
    pend = _pendientes_cobro(docs)
    pend["dias_vencida"] = (hoy - pend["fecha_vencimiento"]).dt.days
    pend["tramo"] = pd.cut(
        pend["dias_vencida"], bins=[-float("inf"), 0, 30, 60, 90, float("inf")], labels=TRAMOS
    )
    g = pend.groupby("tramo", observed=False).agg(documentos=("monto", "count"), monto=("monto", "sum"))
    return g.reindex(TRAMOS).fillna(0).reset_index()


def accion_sugerida(dias_vencida: int) -> str:
    if dias_vencida <= 0:
        return "Aviso preventivo (WhatsApp o correo)"
    if dias_vencida <= 7:
        return "Recordatorio amable"
    if dias_vencida <= 30:
        return "Llamar y pedir una fecha concreta de pago"
    if dias_vencida <= 60:
        return "Mensaje formal y llamada a quien aprueba los pagos"
    return "Escalar: renegociar, frenar nuevas ventas a crédito y evaluar cobranza"


def prioridad_cobranza(
    docs: pd.DataFrame, hoy: pd.Timestamp, atrasos: pd.DataFrame, dias_preventivo: int = 7
) -> pd.DataFrame:
    """Facturas vencidas (y las que vencen pronto) ordenadas por urgencia = monto x días de atraso."""
    cols = [
        "prioridad", "doc_id", "contraparte", "monto", "fecha_vencimiento",
        "dias_vencida", "atraso_habitual", "fuera_de_lo_habitual", "accion",
    ]
    pend = _pendientes_cobro(docs)
    pend["dias_vencida"] = (hoy - pend["fecha_vencimiento"]).dt.days
    pend = pend[pend["dias_vencida"] >= -dias_preventivo].copy()
    if pend.empty:
        return pd.DataFrame(columns=cols)
    habitual = dict(zip(atrasos["contraparte"], atrasos["atraso_estimado"])) if len(atrasos) else {}
    pend["atraso_habitual"] = pend["contraparte"].map(habitual)
    pend["peso"] = pend["monto"] * (1 + pend["dias_vencida"].clip(lower=0) / 30)
    pend.loc[pend["dias_vencida"] <= 0, "peso"] *= 0.25
    pend["fuera_de_lo_habitual"] = pend["atraso_habitual"].notna() & (
        pend["dias_vencida"] > pend["atraso_habitual"] + 10
    )
    pend["accion"] = pend["dias_vencida"].map(accion_sugerida)
    pend = pend.sort_values("peso", ascending=False).reset_index(drop=True)
    pend["prioridad"] = range(1, len(pend) + 1)
    return pend[cols]


def mensaje_cobranza(contraparte: str, monto: float, dias_vencida: int, fecha_venc, empresa: str = "") -> str:
    """Texto listo para copiar, con el tono según cuánto lleva vencida la factura."""
    firma = f"\n\nSaludos,\n{empresa}" if empresa else "\n\nSaludos"
    venc = fecha_corta(fecha_venc)
    if dias_vencida <= 0:
        cuerpo = (
            f"Hola {contraparte}, te escribimos para recordarte que la factura por {clp(monto)} "
            f"vence el {venc}. Si necesitas algún dato para procesar el pago, avísanos. ¡Gracias!"
        )
    elif dias_vencida <= 7:
        cuerpo = (
            f"Hola {contraparte}, te recordamos que la factura por {clp(monto)}, con vencimiento el {venc}, "
            "aparece pendiente. Si ya la pagaste, ignora este mensaje y envíanos el comprobante. ¡Gracias!"
        )
    elif dias_vencida <= 30:
        cuerpo = (
            f"Hola {contraparte}, la factura por {clp(monto)} venció el {venc} y lleva {dias_vencida} días "
            "pendiente. ¿Nos puedes confirmar en qué fecha quedará pagada? Así ordenamos nuestra caja. Gracias."
        )
    elif dias_vencida <= 60:
        cuerpo = (
            f"Estimados de {contraparte}: la factura por {clp(monto)} venció el {venc} y lleva {dias_vencida} "
            "días impaga. Les solicitamos regularizar el pago dentro de los próximos 5 días hábiles "
            "o indicarnos una fecha formal de pago. Quedamos atentos."
        )
    else:
        cuerpo = (
            f"Estimados de {contraparte}: la factura por {clp(monto)} lleva {dias_vencida} días vencida "
            f"(vencimiento {venc}). Necesitamos acordar hoy un plan de pago. Mientras la deuda siga "
            "pendiente, no podremos mantener nuevas ventas a crédito. Esperamos su respuesta."
        )
    return cuerpo + firma
