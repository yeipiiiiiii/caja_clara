"""Explicaciones en lenguaje simple.

- `explicar_local` funciona siempre, sin internet ni clave de API.
- `explicar_con_claude` redacta una versión más natural. Antes de enviar nada,
  los nombres de clientes y proveedores se reemplazan por alias ("Cliente 1")
  y los montos se redondean; al recibir la respuesta se vuelven a poner los nombres reales.
"""
from __future__ import annotations

import json
import os
import re

import pandas as pd

from .formato import clp, fecha_corta, num1

MODELO_DEFECTO = os.environ.get("CAJACLARA_MODELO", "claude-haiku-4-5-20251001")

SISTEMA = (
    "Eres un asesor de finanzas para dueños de micro y pequeñas empresas en Chile. "
    "Hablas en español de Chile, simple y directo, sin jerga financiera. "
    "Usas únicamente los datos que te entregan: no inventes cifras, fechas ni nombres. "
    "No das asesoría de inversión. Los nombres vienen reemplazados por alias (por ejemplo 'Cliente 1'); "
    "úsalos tal cual."
)


def _r(x: float, paso: int = 10_000) -> int:
    return int(round(float(x) / paso) * paso)


def construir_contexto(
    resumen, semanal: pd.DataFrame, cobranza: pd.DataFrame, plan_adel, plan_pagos, escenario: str, semanas: int
) -> tuple[str, dict]:
    """Datos que se envían a Claude. Devuelve (json, mapa alias -> nombre real)."""
    mapa: dict[str, str] = {}

    def alias(nombre: str, prefijo: str) -> str:
        for a, n in mapa.items():
            if n == nombre and a.startswith(prefijo):
                return a
        a = f"{prefijo} {sum(1 for k in mapa if k.startswith(prefijo)) + 1}"
        mapa[a] = nombre
        return a

    ctx: dict = {
        "escenario": escenario,
        "semanas_proyectadas": semanas,
        "saldo_hoy": _r(resumen.saldo_inicial),
        "saldo_minimo": _r(resumen.saldo_minimo),
        "semana_del_saldo_minimo": resumen.semana_minimo,
        "primera_semana_en_negativo": resumen.primera_semana_negativa,
        "faltante_maximo": _r(resumen.brecha),
        "por_cobrar_total": _r(resumen.por_cobrar_total),
        "por_cobrar_vencido": _r(resumen.por_cobrar_vencido),
        "cobros_dudosos_no_contados": _r(resumen.cobros_dudosos),
        "pagos_proximos_30_dias": _r(resumen.pagos_proximos_30d),
        "semanas_de_gastos_que_cubre_el_saldo": None
        if resumen.colchon_semanas is None
        else round(resumen.colchon_semanas, 1),
        "saldo_minimo_por_semana": [_r(v) for v in semanal["saldo_minimo"]],
    }

    vencidas = cobranza[cobranza["dias_vencida"] > 0].head(5) if len(cobranza) else cobranza
    ctx["clientes_a_cobrar_primero"] = [
        {
            "cliente": alias(r.contraparte, "Cliente"),
            "monto": _r(r.monto),
            "dias_vencida": int(r.dias_vencida),
            "atraso_habitual_en_dias": None if pd.isna(r.atraso_habitual) else int(r.atraso_habitual),
        }
        for r in vencidas.itertuples()
    ]

    ctx["rescate_adelantar_facturas"] = (
        None
        if plan_adel is None or plan_adel.facturas.empty
        else {
            "facturas": [
                {
                    "cliente": alias(r.contraparte, "Cliente"),
                    "monto": _r(r.monto),
                    "dias_de_adelanto": int(r.dias_adelanto),
                }
                for r in plan_adel.facturas.itertuples()
            ],
            "costo_total": _r(plan_adel.costo_total, 1_000),
            "resuelve_el_faltante": bool(plan_adel.resuelve),
            "faltante_que_queda": _r(plan_adel.faltante_restante),
        }
    )

    ctx["rescate_negociar_pagos"] = (
        None
        if plan_pagos is None
        else {
            "proveedores": [
                {"proveedor": alias(r.contraparte, "Proveedor"), "monto": _r(abs(r.monto))}
                for r in plan_pagos.pagos.itertuples()
            ],
            "dias_extra_pedidos": int((plan_pagos.pagos["nueva_fecha"] - plan_pagos.pagos["vence"]).dt.days.iloc[0]),
            "saldo_minimo_despues": _r(plan_pagos.saldo_min_despues),
        }
    )
    return json.dumps(ctx, ensure_ascii=False, indent=2), mapa


def construir_prompt(contexto_json: str) -> str:
    return (
        "Estos son los resultados de la proyección de caja de una pyme chilena "
        "(montos en pesos chilenos, redondeados; los nombres están reemplazados por alias):\n\n"
        f"{contexto_json}\n\n"
        "Escribe una explicación para el dueño del negocio, de máximo 170 palabras:\n"
        "1. La situación en una frase (¿le alcanza la plata o no, y cuándo se complica?).\n"
        "2. Tres acciones concretas, ordenadas por urgencia, usando los alias y montos tal como aparecen.\n"
        "3. Cierra recordando en una línea que es una estimación basada en cómo pagaron antes sus clientes."
    )


def desanonimizar(texto: str, mapa: dict[str, str]) -> str:
    """Vuelve a poner los nombres reales donde Claude usó alias."""
    for a in sorted(mapa, key=len, reverse=True):
        texto = re.sub(rf"\b{re.escape(a)}\b", lambda _m, real=mapa[a]: real, texto)
    return texto


def explicar_con_claude(
    contexto_json: str,
    mapa: dict[str, str],
    api_key: str | None = None,
    modelo: str = MODELO_DEFECTO,
    cliente=None,
    max_tokens: int = 700,
) -> str:
    """Pide a Claude una explicación. `cliente` permite inyectar uno falso en los tests."""
    if cliente is None:
        import anthropic

        cliente = anthropic.Anthropic(api_key=api_key or None)
    resp = cliente.messages.create(
        model=modelo,
        max_tokens=max_tokens,
        system=SISTEMA,
        messages=[{"role": "user", "content": construir_prompt(contexto_json)}],
    )
    texto = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not texto:
        raise RuntimeError("La respuesta de Claude llegó vacía.")
    return desanonimizar(texto, mapa)


def explicar_local(resumen, cobranza: pd.DataFrame, plan_adel, plan_pagos, escenario: str) -> str:
    """Explicación hecha con reglas simples (sin IA). Devuelve markdown."""
    partes: list[str] = []
    if resumen.brecha > 0:
        partes.append(
            f"**Atención: te quedarías sin caja.** En el escenario *{escenario.lower()}*, desde el "
            f"{fecha_corta(resumen.primera_fecha_negativa)} (semana {resumen.primera_semana_negativa}) el saldo "
            f"queda en negativo. El peor momento es el {fecha_corta(resumen.fecha_minimo)}, "
            f"con {clp(resumen.saldo_minimo)}: te faltarían **{clp(resumen.brecha)}**."
        )
    else:
        partes.append(
            f"**Tu caja aguanta.** En el escenario *{escenario.lower()}* el saldo nunca baja de "
            f"{clp(resumen.saldo_minimo)} (el punto más bajo es el {fecha_corta(resumen.fecha_minimo)}, "
            f"semana {resumen.semana_minimo})."
        )
    if resumen.colchon_semanas is not None:
        partes.append(f"Tu saldo de hoy cubre unas {num1(resumen.colchon_semanas)} semanas de salidas promedio.")

    acciones: list[str] = []
    vencidas = cobranza[cobranza["dias_vencida"] > 0].head(3) if len(cobranza) else cobranza
    if len(vencidas):
        nombres = ", ".join(f"{r.contraparte} ({clp(r.monto)}, {int(r.dias_vencida)} días)" for r in vencidas.itertuples())
        acciones.append(f"**Cobra primero:** {nombres}. Los mensajes listos están en la pestaña *Cobranza*.")
    if plan_adel is not None and not plan_adel.facturas.empty:
        n = len(plan_adel.facturas)
        quien = ", ".join(sorted(set(plan_adel.facturas["contraparte"])))
        if plan_adel.resuelve:
            acciones.append(
                f"**Adelanta {n} factura{'s' if n > 1 else ''}** ({quien}): cubre el hueco y cuesta cerca de "
                f"{clp(plan_adel.costo_total)}."
            )
        else:
            acciones.append(
                f"**Adelantar facturas ayuda, pero no alcanza:** aun así faltarían {clp(plan_adel.faltante_restante)}."
            )
    if plan_pagos is not None:
        quien = ", ".join(sorted(set(plan_pagos.pagos["contraparte"])))
        dias = int((plan_pagos.pagos["nueva_fecha"] - plan_pagos.pagos["vence"]).dt.days.iloc[0])
        acciones.append(
            f"**Pide más plazo** ({dias} días) a {quien}: el punto más bajo subiría a "
            f"{clp(plan_pagos.saldo_min_despues)}."
        )
    if resumen.cobros_dudosos > 0:
        acciones.append(
            f"**Ojo:** no conté {clp(resumen.cobros_dudosos)} de facturas con muchísimo atraso; "
            "es poco probable que lleguen pronto."
        )
    if acciones:
        partes.append("**Qué hacer, en orden:**\n\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(acciones, 1)))
    partes.append(
        "_Es una estimación basada en cómo han pagado tus clientes antes. "
        "Cambia el escenario o simula un atraso para ver qué pasa._"
    )
    return "\n\n".join(partes)
