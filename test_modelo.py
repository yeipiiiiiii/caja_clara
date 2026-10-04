import math

import pandas as pd
import pytest

from cajaclara.cobranza import antiguedad_cartera, mensaje_cobranza, prioridad_cobranza
from cajaclara.datos import (
    _parse_monto,
    generar_demo,
    leer_archivo,
    normalizar_documentos,
    plantilla_excel,
)
from cajaclara.explicador import (
    construir_contexto,
    desanonimizar,
    explicar_con_claude,
    explicar_local,
)
from cajaclara.formato import clp, clp_corto, fecha_corta
from cajaclara.modelo import (
    ESCENARIOS,
    Parametros,
    _fechas_mensuales,
    analizar,
    estimar_atrasos,
)
from cajaclara.rescate import Factoring, costo_adelanto, efecto_posponer_pagos, plan_adelantos
from cajaclara.modelo import proyectar

HOY = pd.Timestamp("2026-10-04")


def _docs(filas):
    """filas: (tipo, contraparte, monto, dias_vencimiento, dias_pago | None) relativos a HOY."""
    crudo = pd.DataFrame(
        [
            {
                "tipo": t,
                "contraparte": c,
                "monto": m,
                "fecha_vencimiento": HOY + pd.Timedelta(days=v),
                "fecha_pago": pd.NaT if p is None else HOY + pd.Timedelta(days=p),
            }
            for t, c, m, v, p in filas
        ]
    )
    docs, _ = normalizar_documentos(crudo)
    return docs


def _analisis(demo, esc="Esperado", semanas=12, hoy=HOY, extra=None):
    p = Parametros(hoy, demo.saldo_inicial, semanas, ESCENARIOS[esc], extra or {})
    return analizar(demo.docs, demo.gastos, p, demo.ingresos)


# ------------------------------------------------------------------ formato
def test_formatos():
    assert clp(1234567) == "$1.234.567"
    assert clp(-5000) == "-$5.000"
    assert clp(None) == "—"
    assert clp_corto(1_500_000) == "$1,5 M"
    assert clp_corto(350_000) == "$350 mil"
    assert fecha_corta("2026-11-03") == "3 nov"


# ------------------------------------------------------------------ carga de datos
@pytest.mark.parametrize(
    "texto,esperado",
    [("$1.234.567", 1234567), ("1,234", 1234), ("12,5", 12.5), ("1.234,50", 1234.5), (850000, 850000), ("-500", -500)],
)
def test_parse_monto(texto, esperado):
    assert _parse_monto(texto) == pytest.approx(esperado)


def test_parse_monto_invalido():
    assert math.isnan(_parse_monto("abc"))
    assert math.isnan(_parse_monto(""))


def test_normalizar_sinonimos_y_filas_con_error():
    crudo = pd.DataFrame(
        {
            "Cliente": ["A", "B", "C", "D", "E"],
            "Tipo": ["por cobrar", "compra", "xx", "cobro", "cobro"],
            "Importe": ["$1.000.000", 500000, 100, "abc", 100],
            "Vencimiento": ["10/11/2026", "2026-11-15", "2026-11-01", "2026-11-01", "no es fecha"],
            "Fecha pago": [None, None, None, None, None],
        }
    )
    docs, avisos = normalizar_documentos(crudo)
    assert list(docs["tipo"]) == ["cobro", "pago"] or set(docs["tipo"]) == {"cobro", "pago"}
    assert len(docs) == 2
    assert len(avisos) == 3
    assert "Fila 4" in avisos[0] and "Fila 5" in avisos[1] and "Fila 6" in avisos[2]
    # 10/11/2026 se interpreta día/mes (10 de noviembre), no mes/día
    assert docs.loc[docs["contraparte"] == "A", "fecha_vencimiento"].iloc[0] == pd.Timestamp("2026-11-10")


def test_normalizar_falta_columna():
    with pytest.raises(ValueError, match="Faltan columnas obligatorias"):
        normalizar_documentos(pd.DataFrame({"tipo": ["cobro"], "monto": [1]}))


def test_plantilla_se_puede_leer_de_vuelta():
    carga = leer_archivo("plantilla.xlsx", plantilla_excel())
    assert len(carga.docs) == 5
    assert len(carga.gastos) == 3
    assert len(carga.ingresos) == 2
    assert carga.avisos == []


def test_leer_csv_con_punto_y_coma():
    csv = "tipo;cliente;monto;vencimiento\ncobro;Ana;$1.500.000;15/12/2026\npago;Luis;300.000;20/12/2026\n"
    carga = leer_archivo("datos.csv", csv.encode("utf-8"))
    assert list(carga.docs["monto"]) == [1_500_000, 300_000]


def test_formato_no_soportado():
    with pytest.raises(ValueError, match="Formato no soportado"):
        leer_archivo("datos.pdf", b"")


def test_demo_es_reproducible_y_relativa_a_hoy():
    a, b = generar_demo(HOY), generar_demo(HOY)
    pd.testing.assert_frame_equal(a.docs, b.docs)
    c = generar_demo(HOY + pd.Timedelta(days=100))
    desplazamiento = (c.docs["fecha_vencimiento"] - a.docs["fecha_vencimiento"]).dt.days
    assert set(desplazamiento) == {100}


# ------------------------------------------------------------------ aprendizaje de atrasos
def test_atrasos_mezcla_con_el_promedio_general_y_nunca_baja_de_cero():
    docs = _docs(
        [("cobro", "A", 100, -30 + 0, -10)] * 6  # A: vence hace 30, paga hace 10 -> 20 días tarde (x6)
        + [("cobro", "B", 100, -30, -30)]  # B: 1 sola factura, pagó puntual
        + [("cobro", "C", 100, -30, -35)] * 3  # C: paga 5 días antes
    )
    tabla, global_ = estimar_atrasos(docs)
    est = dict(zip(tabla["contraparte"], tabla["atraso_estimado"]))
    assert est["A"] == 20
    assert est["B"] == round((1 * 0 + 3 * global_) / 4)  # pocos datos: se acerca al promedio general
    assert all(v >= 0 for v in est.values())


def test_sin_historial_se_asume_pago_al_vencimiento():
    docs = _docs([("cobro", "A", 100, 5, None)])
    tabla, global_ = estimar_atrasos(docs)
    assert tabla.empty and global_ == 0


# ------------------------------------------------------------------ proyección
def _params(esc="Optimista", saldo=1000, semanas=2, extra=None):
    return Parametros(HOY, saldo, semanas, ESCENARIOS[esc], extra or {})


def test_proyeccion_calculada_a_mano():
    docs = _docs([("cobro", "A", 500, 10, None), ("pago", "P", 300, 3, None)])
    an = analizar(docs, pd.DataFrame(columns=["concepto", "monto", "dia_mes"]), _params())
    d = an.proyeccion.diario.set_index("fecha")["saldo"]
    assert d[HOY] == 1000
    assert d[HOY + pd.Timedelta(days=3)] == 700
    assert d[HOY + pd.Timedelta(days=10)] == 1200
    s = an.proyeccion.semanal.set_index("semana")
    assert s.loc[1, "salidas"] == 300 and s.loc[1, "saldo_final"] == 700
    assert s.loc[2, "entradas"] == 500 and s.loc[2, "saldo_final"] == 1200
    assert an.resumen.brecha == 0


def test_pago_vencido_sale_hoy_y_cobro_vencido_llega_despues():
    docs = _docs([("pago", "P", 300, -5, None), ("cobro", "A", 500, -10, None)])
    an = analizar(docs, pd.DataFrame(columns=["concepto", "monto", "dia_mes"]), _params("Esperado"))
    ev = an.eventos.set_index("concepto")
    assert ev.loc["Pago a proveedor", "fecha"] == HOY
    assert ev.loc["Cobro", "fecha"] == HOY + pd.Timedelta(days=ESCENARIOS["Esperado"].espera_vencidas)


def test_cobros_muy_vencidos_se_excluyen_salvo_en_optimista():
    docs = _docs([("cobro", "A", 500, -95, None)])
    vacio = pd.DataFrame(columns=["concepto", "monto", "dia_mes"])
    assert len(analizar(docs, vacio, _params("Esperado")).eventos) == 0
    assert len(analizar(docs, vacio, _params("Optimista")).eventos) == 1
    assert analizar(docs, vacio, _params("Esperado")).resumen.cobros_dudosos == 500


def test_gasto_del_dia_31_cae_el_ultimo_dia_del_mes():
    fechas = _fechas_mensuales(31, pd.Timestamp("2027-02-01"), pd.Timestamp("2027-03-31"))
    assert fechas == [pd.Timestamp("2027-02-28"), pd.Timestamp("2027-03-31")]


def test_escenarios_son_monotonos_y_la_demo_cuenta_la_historia_esperada():
    demo = generar_demo(HOY)
    opt, esp, pes = (_analisis(demo, e).resumen for e in ("Optimista", "Esperado", "Pesimista"))
    assert opt.saldo_minimo >= esp.saldo_minimo >= pes.saldo_minimo
    assert opt.brecha == 0
    assert esp.brecha > 0 and esp.primera_semana_negativa in (4, 5, 6)


@pytest.mark.parametrize("hoy", ["2026-10-04", "2027-01-30", "2027-03-15", "2026-12-28", "2027-05-31"])
def test_la_demo_es_estable_cualquier_dia_del_anio(hoy):
    hoy = pd.Timestamp(hoy)
    demo = generar_demo(hoy)
    esp = _analisis(demo, "Esperado", hoy=hoy)
    assert _analisis(demo, "Optimista", hoy=hoy).resumen.brecha == 0
    assert esp.resumen.brecha > 0
    assert plan_adelantos(esp.eventos, esp.params, Factoring()).resuelve


def test_que_pasa_si_un_cliente_se_atrasa_mas():
    demo = generar_demo(HOY)
    base = _analisis(demo).resumen.saldo_minimo
    peor = _analisis(demo, extra={"Constructora Andes": 30}).resumen.saldo_minimo
    assert peor <= base


# ------------------------------------------------------------------ cobranza
def test_antiguedad_suma_lo_que_te_deben():
    demo = generar_demo(HOY)
    aging = antiguedad_cartera(demo.docs, HOY)
    pendiente = demo.docs[(demo.docs["tipo"] == "cobro") & demo.docs["fecha_pago"].isna()]["monto"].sum()
    assert aging["monto"].sum() == pendiente
    assert list(aging["tramo"].astype(str))[0] == "Por vencer"


def test_prioridad_de_cobranza_y_mensajes():
    demo = generar_demo(HOY)
    an = _analisis(demo)
    prio = prioridad_cobranza(demo.docs, HOY, an.atrasos)
    assert prio.iloc[0]["contraparte"] == "Municipalidad de Costanera"
    assert (prio["dias_vencida"] > 0).all()
    suave = mensaje_cobranza("Cliente X", 500_000, 3, HOY, "Mi Pyme")
    duro = mensaje_cobranza("Cliente X", 500_000, 75, HOY, "Mi Pyme")
    assert "$500.000" in suave and "Mi Pyme" in suave
    assert suave != duro and "plan de pago" in duro


# ------------------------------------------------------------------ rescate
def test_costo_del_adelanto():
    bruto, costo, neto = costo_adelanto(1_000_000, 30, Factoring(tasa_mensual=0.02, anticipo=0.9, comision_fija=5000))
    assert bruto == 900_000
    assert costo == pytest.approx(900_000 * 0.02 + 5000)
    assert neto == pytest.approx(bruto - costo)


def test_plan_de_adelantos_resuelve_y_esta_verificado():
    demo = generar_demo(HOY)
    an = _analisis(demo)
    plan = plan_adelantos(an.eventos, an.params, Factoring())
    assert plan.resuelve and plan.costo_total > 0
    assert plan.saldo_min_antes < 0 <= plan.saldo_min_despues
    # se re-proyecta la caja completa con los adelantos y de verdad no baja de cero
    assert proyectar(plan.eventos_despues, an.params).diario["saldo"].min() >= 0
    # y adelantar sale barato respecto de lo que se adelanta
    assert plan.costo_total < 0.05 * plan.facturas["adelanto_bruto"].sum()


def test_sin_faltante_no_hay_nada_que_adelantar():
    demo = generar_demo(HOY)
    an = _analisis(demo, "Optimista")
    plan = plan_adelantos(an.eventos, an.params, Factoring())
    assert plan.facturas.empty and plan.resuelve and plan.costo_total == 0
    assert efecto_posponer_pagos(an.eventos, an.params) is None


def test_negociar_plazos_mejora_el_punto_mas_bajo():
    demo = generar_demo(HOY)
    an = _analisis(demo)
    pp = efecto_posponer_pagos(an.eventos, an.params, dias=15)
    assert pp is not None and pp.mejora > 0 and len(pp.pagos) <= 3


# ------------------------------------------------------------------ explicaciones
def test_explicacion_local_segun_la_situacion():
    demo = generar_demo(HOY)
    esp, opt = _analisis(demo), _analisis(demo, "Optimista")
    prio = prioridad_cobranza(demo.docs, HOY, esp.atrasos)
    plan = plan_adelantos(esp.eventos, esp.params, Factoring())
    texto = explicar_local(esp.resumen, prio, plan, efecto_posponer_pagos(esp.eventos, esp.params), "Esperado")
    assert "te quedarías sin caja" in texto and clp(esp.resumen.brecha) in texto and "Cobra primero" in texto
    assert "Tu caja aguanta" in explicar_local(opt.resumen, prio, None, None, "Optimista")


class _Bloque:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ClienteFalso:
    def __init__(self, respuesta):
        self.respuesta, self.llamadas = respuesta, []
        self.messages = self

    def create(self, **kw):
        self.llamadas.append(kw)
        return type("R", (), {"content": [_Bloque(self.respuesta)]})()


def test_a_claude_solo_viajan_alias_y_se_restituyen_los_nombres():
    demo = generar_demo(HOY)
    esp = _analisis(demo)
    prio = prioridad_cobranza(demo.docs, HOY, esp.atrasos)
    plan = plan_adelantos(esp.eventos, esp.params, Factoring())
    pp = efecto_posponer_pagos(esp.eventos, esp.params)
    contexto, mapa = construir_contexto(esp.resumen, esp.proyeccion.semanal, prio, plan, pp, "Esperado", 12)

    nombres = set(demo.docs["contraparte"])
    assert not any(n in contexto for n in nombres)
    assert "Cliente 1" in contexto and mapa["Cliente 1"] in nombres

    falso = _ClienteFalso("Cobra primero a Cliente 1 y pide plazo a Proveedor 1.")
    texto = explicar_con_claude(contexto, mapa, modelo="modelo-x", cliente=falso)
    enviado = falso.llamadas[0]
    assert enviado["model"] == "modelo-x"
    assert not any(n in enviado["messages"][0]["content"] for n in nombres)
    assert mapa["Cliente 1"] in texto and "Cliente 1" not in texto
    assert mapa["Proveedor 1"] in texto


def test_desanonimizar_no_confunde_alias_con_prefijo_comun():
    mapa = {"Cliente 1": "Ana", "Cliente 10": "Beto"}
    assert desanonimizar("Cliente 10 y Cliente 1", mapa) == "Beto y Ana"


def test_respuesta_vacia_de_claude_da_error():
    with pytest.raises(RuntimeError):
        explicar_con_claude("{}", {}, cliente=_ClienteFalso("   "))
