"""Caja Clara · ¿Te alcanzará la plata en las próximas semanas?

Ejecutar con:  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import os
from datetime import date

import pandas as pd
import streamlit as st

from cajaclara.cobranza import antiguedad_cartera, mensaje_cobranza, prioridad_cobranza
from cajaclara.datos import (
    Carga,
    generar_demo,
    gastos_vacios,
    leer_archivo,
    normalizar_gastos,
    normalizar_ingresos,
    plantilla_excel,
)
from cajaclara.explicador import MODELO_DEFECTO, construir_contexto, explicar_con_claude, explicar_local
from cajaclara.formato import clp, fecha_corta, md_seguro as md, num1
from cajaclara.graficos import grafico_antiguedad, grafico_comparado, grafico_flujo, grafico_saldo
from cajaclara.modelo import ESCENARIOS, Parametros, analizar, proyectar
from cajaclara.rescate import Factoring, efecto_posponer_pagos, plan_adelantos

st.set_page_config(page_title="Caja Clara", page_icon="📈", layout="wide")

FUENTE_DEMO = "Datos de ejemplo"
FUENTE_PROPIA = "Subir mi archivo"
NINGUNO = "(ninguno)"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@st.cache_data
def _plantilla() -> bytes:
    return plantilla_excel()


def _txt_dias(d: int) -> str:
    if d > 0:
        return "1 día vencida" if d == 1 else f"{d} días vencida"
    if d == 0:
        return "vence hoy"
    return f"vence en {-d} días"


# ----------------------------------------------------------------------------
# Barra lateral: datos, escenario y opciones
# ----------------------------------------------------------------------------
carga: Carga | None = None
empresa = "Mi negocio"
saldo_defecto = 0
firma = "sin_archivo"

with st.sidebar:
    st.title("Caja Clara")
    st.caption("¿Te alcanzará la plata en las próximas semanas?")

    st.subheader("1 · Tus datos")
    fuente = st.radio("¿Qué datos usar?", [FUENTE_DEMO, FUENTE_PROPIA])
    hoy_fecha = st.date_input("Fecha de hoy", value=date.today(), format="DD/MM/YYYY")
    hoy = pd.Timestamp(hoy_fecha)

    if fuente == FUENTE_DEMO:
        demo = generar_demo(hoy)
        carga = Carga(docs=demo.docs, gastos=demo.gastos, avisos=[], ingresos=demo.ingresos)
        empresa, saldo_defecto, firma = demo.empresa, int(demo.saldo_inicial), f"demo_{hoy_fecha.isoformat()}"
    else:
        archivo = st.file_uploader("Excel (.xlsx) o CSV", type=["xlsx", "xlsm", "csv"])
        if archivo is not None:
            firma = f"{archivo.name}_{archivo.size}"
            try:
                carga = leer_archivo(archivo.name, archivo.getvalue())
            except ValueError as e:
                st.error(str(e))
            except Exception as e:  # archivo dañado u otro problema inesperado
                st.error(f"No pude leer el archivo ({type(e).__name__}). Prueba con la plantilla.")
    st.download_button("Descargar plantilla de Excel", data=_plantilla(), file_name="plantilla_caja_clara.xlsx", mime=MIME_XLSX)

    saldo = st.number_input("Saldo actual en caja ($)", value=saldo_defecto, step=100_000, format="%d", key=f"saldo_{firma}")

    st.subheader("2 · Gastos fijos y ventas esperadas")
    cfg = {
        "concepto": st.column_config.TextColumn("Concepto"),
        "monto": st.column_config.NumberColumn("Monto ($)", min_value=0, step=10_000, format="%d"),
        "dia_mes": st.column_config.NumberColumn("Día del mes", min_value=1, max_value=31, step=1, format="%d"),
    }
    st.caption("Gastos fijos que se repiten cada mes (arriendo, sueldos…):")
    gastos_ed = st.data_editor(
        carga.gastos if carga is not None else gastos_vacios(),
        num_rows="dynamic", hide_index=True, column_config=cfg, key=f"gastos_{firma}",
    )
    st.caption("Ventas que aún no facturas pero esperas cobrar cada mes (opcional):")
    ingresos_ed = st.data_editor(
        carga.ingresos if carga is not None else gastos_vacios(),
        num_rows="dynamic", hide_index=True, column_config=cfg, key=f"ingresos_{firma}",
    )

    st.subheader("3 · Escenario")
    nombre_esc = st.select_slider(
        "¿Cómo pagan tus clientes?",
        options=list(ESCENARIOS),
        value="Esperado",
        help="Optimista: todos pagan al vencimiento. Esperado: cada cliente se atrasa lo que históricamente se ha atrasado. "
        "Pesimista: además, 15 días más de atraso.",
    )
    semanas = st.slider("Semanas a proyectar", 8, 16, 12)
    clientes = (
        sorted(carga.docs.loc[(carga.docs["tipo"] == "cobro") & carga.docs["fecha_pago"].isna(), "contraparte"].unique())
        if carga is not None
        else []
    )
    cliente_wi = st.selectbox("¿Y si este cliente se atrasa más?", [NINGUNO, *clientes])
    dias_wi = st.slider("Días extra de atraso", 0, 60, 0, step=5, disabled=cliente_wi == NINGUNO)

    st.subheader("4 · Claude (opcional)")
    clave = st.text_input(
        "Clave de API de Anthropic",
        type="password",
        help="Opcional. Sin clave igual verás una explicación hecha localmente. "
        "Si ya tienes ANTHROPIC_API_KEY en tu entorno, se usa automáticamente.",
    )
    clave_efectiva = clave or os.environ.get("ANTHROPIC_API_KEY", "")
    with st.expander("Avanzado"):
        modelo = st.text_input("Modelo", value=MODELO_DEFECTO)

# ----------------------------------------------------------------------------
# Pantalla de bienvenida si todavía no hay datos
# ----------------------------------------------------------------------------
if carga is None:
    st.title("Caja Clara")
    st.write(
        "Sube un Excel o CSV con tus facturas por cobrar y por pagar para ver si tu caja alcanza "
        "en las próximas semanas, a quién cobrar primero y qué hacer si se viene un hueco."
    )
    st.info("¿Primera vez? Descarga la plantilla en el menú lateral, reemplaza los ejemplos con tus datos y súbela. "
            "O cambia a *Datos de ejemplo* para explorar la app.")
    st.stop()

# ----------------------------------------------------------------------------
# Cálculo
# ----------------------------------------------------------------------------
gastos = normalizar_gastos(gastos_ed)
ingresos = normalizar_ingresos(ingresos_ed)
extra = {cliente_wi: dias_wi} if cliente_wi != NINGUNO and dias_wi > 0 else {}
params = Parametros(hoy, float(saldo), semanas, ESCENARIOS[nombre_esc], extra)
an = analizar(carga.docs, gastos, params, ingresos)
res, proy = an.resumen, an.proyeccion
prio = prioridad_cobranza(carga.docs, hoy, an.atrasos)

st.title("Caja Clara")
detalle_wi = f" · simulando {dias_wi} días extra de atraso de {cliente_wi}" if extra else ""
st.caption(f"{empresa} · proyección a {semanas} semanas · escenario {nombre_esc.lower()}{detalle_wi}")

if res.brecha > 0:
    if res.fecha_minimo == res.primera_fecha_negativa:
        peor = f"ese día faltarían **{clp(res.brecha)}**"
    else:
        peor = f"en el peor momento ({fecha_corta(res.fecha_minimo)}) faltarían **{clp(res.brecha)}**"
    st.error(
        md(
            f"**Te quedarías sin caja el {fecha_corta(res.primera_fecha_negativa)}** "
            f"(semana {res.primera_semana_negativa}): {peor}. Mira la pestaña *Plan de rescate*."
        ),
        icon="🚨",
    )
else:
    st.success(
        md(
            f"**Tu caja aguanta las próximas {semanas} semanas.** El punto más bajo es {clp(res.saldo_minimo)} "
            f"el {fecha_corta(res.fecha_minimo)}."
        ),
        icon="✅",
    )
if carga.avisos:
    st.warning(f"Omití {len(carga.avisos)} fila(s) con problemas. Los detalles están en la pestaña *Datos*.")

m1, m2, m3, m4 = st.columns(4)
m1.metric(
    "Saldo hoy",
    clp(res.saldo_inicial),
    help=None if res.colchon_semanas is None else f"Cubre unas {num1(res.colchon_semanas)} semanas de salidas promedio.",
)
m2.metric(
    "Punto más bajo",
    clp(res.saldo_minimo),
    delta=f"{fecha_corta(res.fecha_minimo)} · semana {res.semana_minimo}",
    delta_color="inverse" if res.brecha > 0 else "off",
    delta_arrow="off",
)
m3.metric(
    "Te deben",
    clp(res.por_cobrar_total),
    delta=f"{clp(res.por_cobrar_vencido)} ya vencido" if res.por_cobrar_vencido > 0 else None,
    delta_color="inverse",
    delta_arrow="off",
)
m4.metric("Debes pagar (30 días)", clp(res.pagos_proximos_30d))

plan_a = plan_p = None
tab_proy, tab_cob, tab_rescate, tab_expl, tab_datos = st.tabs(
    ["Proyección", "Cobranza", "Plan de rescate", "Explicación", "Datos"]
)

# ---- Proyección ------------------------------------------------------------
with tab_proy:
    st.subheader("Saldo de caja día a día")
    st.altair_chart(grafico_saldo(proy.diario), width="stretch")
    st.subheader("Entradas y salidas por semana")
    st.altair_chart(grafico_flujo(proy.semanal), width="stretch")
    sem = proy.semanal
    vista = pd.DataFrame(
        {
            "Semana": sem["semana"],
            "Período": [f"{fecha_corta(a)} – {fecha_corta(b)}" for a, b in zip(sem["desde"], sem["hasta"])],
            "Entradas": sem["entradas"].map(clp),
            "Salidas": sem["salidas"].map(clp),
            "Saldo al cierre": sem["saldo_final"].map(clp),
            "Saldo más bajo": sem["saldo_minimo"].map(clp),
            "Estado": ["⚠ Faltan " + clp(-m) if m < 0 else "OK" for m in sem["saldo_minimo"]],
        }
    )
    st.dataframe(vista, hide_index=True, width="stretch")
    if res.cobros_dudosos > 0:
        st.caption(
            md(
                f"No contamos {clp(res.cobros_dudosos)} en facturas con muchísimos días de atraso: "
                "es poco probable que lleguen pronto."
            )
        )

# ---- Cobranza --------------------------------------------------------------
with tab_cob:
    izq, der = st.columns([3, 2])
    with izq:
        st.subheader("¿Cuánto te deben y hace cuánto?")
        aging = antiguedad_cartera(carga.docs, hoy)
        st.altair_chart(grafico_antiguedad(aging), width="stretch")
    with der:
        st.subheader("Resumen por tramo")
        st.dataframe(
            pd.DataFrame(
                {"Tramo": aging["tramo"].astype(str), "Facturas": aging["documentos"].astype(int), "Monto": aging["monto"].map(clp)}
            ),
            hide_index=True,
            width="stretch",
        )

    st.subheader("A quién cobrar primero")
    if prio.empty:
        st.info("No tienes facturas vencidas ni por vencer esta semana.")
    else:
        st.dataframe(
            pd.DataFrame(
                {
                    "#": prio["prioridad"],
                    "Cliente": prio["contraparte"],
                    "Monto": prio["monto"].map(clp),
                    "Vence": prio["fecha_vencimiento"].map(fecha_corta),
                    "Estado": [
                        ("⚠ " if f else "") + _txt_dias(int(d)) for d, f in zip(prio["dias_vencida"], prio["fuera_de_lo_habitual"])
                    ],
                    "Suele pagar": prio["atraso_habitual"].map(lambda h: "—" if pd.isna(h) else f"{int(h)} días tarde"),
                    "Qué hacer": prio["accion"],
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "#": st.column_config.NumberColumn("#", width="small"),
                "Qué hacer": st.column_config.TextColumn("Qué hacer", width="large"),
            },
        )
        if prio["fuera_de_lo_habitual"].any():
            st.caption("⚠ = esa factura lleva más atraso de lo que ese cliente suele tardar en pagar.")
        etiquetas = [f"{r.contraparte} · {clp(r.monto)} · {_txt_dias(int(r.dias_vencida))}" for r in prio.itertuples()]
        elegido = st.selectbox("Mensaje listo para enviar a:", etiquetas)
        fila = prio.iloc[etiquetas.index(elegido)]
        st.code(
            mensaje_cobranza(fila["contraparte"], fila["monto"], int(fila["dias_vencida"]), fila["fecha_vencimiento"], empresa),
            language=None,
            wrap_lines=True,
        )

# ---- Plan de rescate -------------------------------------------------------
with tab_rescate:
    if res.brecha == 0:
        st.success("No necesitas un plan de rescate: con estos supuestos la caja no queda en negativo.", icon="✅")
        st.caption("Prueba cambiar el escenario a *Pesimista* o simular que un cliente grande se atrasa más.")
    else:
        st.markdown(
            md(
                f"La caja se pone en negativo desde el **{fecha_corta(res.primera_fecha_negativa)}** y el hueco máximo es de "
                f"**{clp(res.brecha)}** ({fecha_corta(res.fecha_minimo)}). Dos formas de taparlo:"
            )
        )
        st.subheader("Opción A · Adelantar facturas (factoring)")
        c1, c2, c3 = st.columns(3)
        tasa = c1.number_input("Tasa mensual (%)", 0.0, 10.0, 1.5, 0.1, help="Costo mensual sobre lo que te adelantan. Pide cotización real.")
        anticipo = c2.number_input("Anticipo (%)", 50, 100, 90, 5, help="Parte de la factura que te pagan de inmediato.")
        comision = c3.number_input("Comisión por factura ($)", 0, 200_000, 0, 1_000)
        plan_a = plan_adelantos(
            an.eventos, params, Factoring(tasa_mensual=tasa / 100, anticipo=anticipo / 100, comision_fija=float(comision))
        )
        if plan_a.facturas.empty:
            st.warning("No hay facturas por cobrar que se puedan adelantar después del primer día en negativo.")
        else:
            f = plan_a.facturas
            st.dataframe(
                pd.DataFrame(
                    {
                        "Cliente": f["contraparte"],
                        "Monto factura": f["monto"].map(clp),
                        "Cobro esperado": f["fecha_esperada"].map(fecha_corta),
                        "Días adelantados": f["dias_adelanto"].astype(int),
                        "Recibes en 2 días": f["neto_hoy"].map(clp),
                        "Costo": f["costo"].map(clp),
                    }
                ),
                hide_index=True,
                width="stretch",
            )
            if plan_a.resuelve:
                st.success(
                    md(
                        f"Adelantando {len(f)} factura(s) la caja no baja de cero. Costo total estimado: "
                        f"**{clp(plan_a.costo_total)}**."
                    ),
                    icon="✅",
                )
            else:
                st.warning(
                    md(
                        f"Adelantar todas las facturas posibles no alcanza: aún faltarían {clp(plan_a.faltante_restante)}. "
                        "Combínalo con la opción B."
                    ),
                    icon="⚠️",
                )
            st.altair_chart(grafico_comparado(proy.diario, proyectar(plan_a.eventos_despues, params).diario), width="stretch")

        st.subheader("Opción B · Negociar más plazo con proveedores")
        dias_neg = st.slider("Días extra a pedir", 5, 45, 15, 5)
        plan_p = efecto_posponer_pagos(an.eventos, params, dias_neg)
        if plan_p is None:
            st.info("No hay pagos grandes antes del punto más bajo que valga la pena negociar.")
        else:
            p = plan_p.pagos
            st.dataframe(
                pd.DataFrame(
                    {
                        "Proveedor": p["contraparte"],
                        "Monto": p["monto"].abs().map(clp),
                        "Vence": p["vence"].map(fecha_corta),
                        "Nueva fecha": p["nueva_fecha"].map(fecha_corta),
                    }
                ),
                hide_index=True,
                width="stretch",
            )
            st.markdown(
                md(f"El punto más bajo pasaría de **{clp(plan_p.saldo_min_antes)}** a **{clp(plan_p.saldo_min_despues)}**.")
            )
        st.caption(
            "Adelantar facturas tiene un costo financiero; negociar plazos no, pero depende de tu relación con cada proveedor."
        )

# ---- Explicación -----------------------------------------------------------
with tab_expl:
    st.markdown(md(explicar_local(res, prio, plan_a, plan_p, nombre_esc)))
    st.divider()
    st.subheader("Versión redactada por Claude")
    contexto_json, mapa = construir_contexto(res, proy.semanal, prio, plan_a, plan_p, nombre_esc, semanas)
    with st.expander("¿Qué se envía a Claude?"):
        st.caption(
            md(
                "Los nombres de clientes y proveedores se reemplazan por alias y los montos se redondean a $10.000. "
                "No se envían tus archivos ni fechas de facturas. Los nombres reales se restituyen al mostrar la respuesta."
            )
        )
        st.code(contexto_json, language="json")
    huella = hashlib.sha1(contexto_json.encode()).hexdigest()
    if not clave_efectiva:
        st.info("Para esta versión ingresa una clave de API de Anthropic en el menú lateral (sección 4).")
    if st.button("Redactar con Claude", disabled=not clave_efectiva):
        with st.spinner("Claude está escribiendo…"):
            try:
                st.session_state["ia"] = (huella, explicar_con_claude(contexto_json, mapa, clave_efectiva, modelo))
                st.session_state.pop("ia_error", None)
            except Exception as e:
                st.session_state["ia_error"] = f"No pude obtener la respuesta de Claude ({type(e).__name__}): {str(e)[:200]}"
    if st.session_state.get("ia_error"):
        st.error(st.session_state["ia_error"])
    ia = st.session_state.get("ia")
    if ia and ia[0] == huella:
        st.markdown(md(ia[1]))
    elif ia:
        st.caption("Cambiaste los datos o el escenario: vuelve a presionar el botón para actualizar esta versión.")

# ---- Datos -----------------------------------------------------------------
with tab_datos:
    for aviso in carga.avisos:
        st.warning(md(aviso))
    st.subheader("Cuánto se atrasa cada cliente")
    if an.atrasos.empty:
        st.info("No hay facturas ya pagadas en tus datos, así que supusimos que todos pagan al vencimiento. "
                "Agrega la fecha de pago de facturas anteriores para que la proyección aprenda de tu historial.")
    else:
        st.dataframe(
            an.atrasos.assign(atraso_mediano=an.atrasos["atraso_mediano"].round().astype(int)).rename(
                columns={
                    "contraparte": "Cliente",
                    "n_pagos": "Facturas pagadas",
                    "atraso_mediano": "Atraso mediano (días)",
                    "atraso_estimado": "Atraso que usamos (días)",
                }
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Con pocas facturas pagadas mezclamos el atraso del cliente con el promedio general para no sacar "
            "conclusiones de 1 o 2 casos. Si un cliente paga antes del vencimiento, igual usamos 0 (criterio conservador)."
        )
    st.subheader("Facturas cargadas")
    d = carga.docs
    st.dataframe(
        pd.DataFrame(
            {
                "Tipo": d["tipo"].map({"cobro": "Por cobrar", "pago": "Por pagar"}),
                "Cliente / proveedor": d["contraparte"],
                "Monto": d["monto"].map(clp),
                "Vence": d["fecha_vencimiento"].map(fecha_corta),
                "Pagada el": d["fecha_pago"].map(lambda f: "—" if pd.isna(f) else fecha_corta(f)),
            }
        ),
        hide_index=True,
        width="stretch",
    )
    with st.expander("Movimientos proyectados (cómo se arma la proyección)"):
        ev = an.eventos
        st.dataframe(
            pd.DataFrame(
                {
                    "Fecha": ev["fecha"].map(fecha_corta),
                    "Concepto": ev["concepto"],
                    "Detalle": ev["contraparte"],
                    "Monto": ev["monto"].map(clp),
                }
            ),
            hide_index=True,
            width="stretch",
        )

st.divider()
st.caption(
    "Prototipo educativo. Las proyecciones son estimaciones basadas en tu historial y no constituyen asesoría financiera."
)
