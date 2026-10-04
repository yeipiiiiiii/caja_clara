"""Carga y limpieza de datos, plantilla de Excel y datos de ejemplo."""
from __future__ import annotations

import io
import numbers
import random
import re
import unicodedata
from dataclasses import dataclass, field

import pandas as pd

COLUMNAS_DOCS = ["tipo", "contraparte", "monto", "fecha_emision", "fecha_vencimiento", "fecha_pago"]
OBLIGATORIAS = ["tipo", "contraparte", "monto", "fecha_vencimiento"]

_SINONIMOS = {
    "tipo": {"tipo", "movimiento", "clase", "tipo_documento"},
    "contraparte": {"contraparte", "cliente", "proveedor", "nombre", "razon_social", "cliente_proveedor"},
    "monto": {"monto", "importe", "valor", "total", "monto_total", "monto_bruto"},
    "fecha_emision": {"fecha_emision", "emision", "fecha_de_emision", "fecha"},
    "fecha_vencimiento": {"fecha_vencimiento", "vencimiento", "vence", "fecha_de_vencimiento", "fecha_vence"},
    "fecha_pago": {"fecha_pago", "pagado_el", "fecha_de_pago", "fecha_pagada", "pagada_el"},
}
_TIPO_COBRO = {"cobro", "por_cobrar", "venta", "ingreso", "cliente", "cxc", "c"}
_TIPO_PAGO = {"pago", "por_pagar", "compra", "egreso", "proveedor", "cxp", "gasto", "p"}


@dataclass
class Carga:
    """Resultado de leer y limpiar los datos del usuario."""

    docs: pd.DataFrame
    gastos: pd.DataFrame
    avisos: list[str] = field(default_factory=list)
    ingresos: pd.DataFrame = field(default_factory=lambda: gastos_vacios())


@dataclass
class Demo:
    empresa: str
    docs: pd.DataFrame
    gastos: pd.DataFrame
    saldo_inicial: float
    ingresos: pd.DataFrame = field(default_factory=lambda: gastos_vacios())


def _slug(texto) -> str:
    s = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _parse_monto(x) -> float:
    """Acepta números y textos como '$1.234.567', '1,234', '12,5' o '1.234,50'."""
    if x is None:
        return float("nan")
    if isinstance(x, numbers.Number):
        return float(x)
    s = str(x).strip().replace("$", "").replace(" ", "")
    if not s:
        return float("nan")
    negativo = s.startswith("-")
    s = s.lstrip("-")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)  # separador de miles
    elif "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return float("nan")
    return -v if negativo else v


def _fechas(serie: pd.Series) -> pd.Series:
    return pd.to_datetime(serie, dayfirst=True, errors="coerce", format="mixed").dt.normalize()


def normalizar_documentos(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Deja la tabla de facturas en el formato interno. Devuelve (tabla, avisos).

    Las filas con problemas se descartan y se explica por qué en `avisos`.
    Lanza ValueError si faltan columnas obligatorias o no queda ninguna fila válida.
    """
    avisos: list[str] = []
    df = df.copy().reset_index(drop=True)

    renombres: dict = {}
    for col in df.columns:
        s = _slug(col)
        for canon, alias in _SINONIMOS.items():
            if s in alias and canon not in renombres.values():
                renombres[col] = canon
                break
    df = df.rename(columns=renombres)

    faltan = [c for c in OBLIGATORIAS if c not in df.columns]
    if faltan:
        raise ValueError(
            "Faltan columnas obligatorias: " + ", ".join(faltan) + ". Descarga la plantilla para ver el formato."
        )
    for c in ("fecha_emision", "fecha_pago"):
        if c not in df.columns:
            df[c] = pd.NaT

    df["fila"] = df.index + 2  # número de fila en Excel (la 1 es el encabezado)
    df = df.dropna(how="all", subset=OBLIGATORIAS)

    df["tipo"] = df["tipo"].map(lambda v: _slug(v) if pd.notna(v) else "")
    df["tipo"] = df["tipo"].map(
        lambda s: "cobro" if s in _TIPO_COBRO else ("pago" if s in _TIPO_PAGO else None)
    )
    df["monto"] = df["monto"].map(_parse_monto)
    for c in ("fecha_emision", "fecha_vencimiento", "fecha_pago"):
        df[c] = _fechas(df[c])
    df["contraparte"] = df["contraparte"].fillna("Sin nombre").astype(str).str.strip()
    df.loc[df["contraparte"] == "", "contraparte"] = "Sin nombre"

    ok = pd.Series(True, index=df.index)
    for i, r in df.iterrows():
        if pd.isna(r["tipo"]):
            avisos.append(f"Fila {r['fila']}: tipo no reconocido (usa «cobro» o «pago»). Se omitió.")
            ok[i] = False
        elif pd.isna(r["monto"]) or r["monto"] <= 0:
            avisos.append(f"Fila {r['fila']}: monto inválido. Se omitió.")
            ok[i] = False
        elif pd.isna(r["fecha_vencimiento"]):
            avisos.append(f"Fila {r['fila']}: fecha de vencimiento inválida. Se omitió.")
            ok[i] = False

    docs = df.loc[ok].copy()
    if docs.empty:
        raise ValueError("No quedó ninguna fila válida. Revisa los datos o descarga la plantilla.")
    docs["fecha_emision"] = docs["fecha_emision"].fillna(docs["fecha_vencimiento"])
    docs = docs[COLUMNAS_DOCS + ["fila"]].sort_values("fecha_vencimiento", kind="stable").reset_index(drop=True)
    docs.insert(0, "doc_id", range(len(docs)))
    return docs, avisos


def gastos_vacios() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "concepto": pd.Series(dtype="str"),
            "monto": pd.Series(dtype="float"),
            "dia_mes": pd.Series(dtype="int"),
        }
    )


def normalizar_recurrentes(df: pd.DataFrame | None, concepto_defecto: str = "Gasto") -> pd.DataFrame:
    """Movimientos que se repiten cada mes (gastos fijos o ingresos esperados): concepto, monto y día del mes."""
    if df is None or len(df) == 0:
        return gastos_vacios()
    ren = {}
    for col in df.columns:
        s = _slug(col)
        if s in {"concepto", "gasto", "nombre", "descripcion"}:
            ren[col] = "concepto"
        elif s in {"monto", "importe", "valor"}:
            ren[col] = "monto"
        elif s in {"dia_mes", "dia", "dia_del_mes", "dia_de_pago"}:
            ren[col] = "dia_mes"
    df = df.rename(columns=ren)
    for c in ("concepto", "monto", "dia_mes"):
        if c not in df.columns:
            df[c] = None
    out = pd.DataFrame(
        {
            "concepto": df["concepto"].fillna(concepto_defecto).astype(str).str.strip(),
            "monto": df["monto"].map(_parse_monto),
            "dia_mes": pd.to_numeric(df["dia_mes"], errors="coerce"),
        }
    )
    out = out.dropna(subset=["monto", "dia_mes"])
    out = out[out["monto"] > 0].copy()
    out["dia_mes"] = out["dia_mes"].clip(1, 31).round().astype(int)
    return out.reset_index(drop=True)


def normalizar_gastos(df: pd.DataFrame | None) -> pd.DataFrame:
    return normalizar_recurrentes(df, "Gasto")


def normalizar_ingresos(df: pd.DataFrame | None) -> pd.DataFrame:
    return normalizar_recurrentes(df, "Ingreso esperado")


def leer_archivo(nombre: str, contenido: bytes) -> Carga:
    """Lee un Excel (.xlsx) o CSV subido por el usuario."""
    n = nombre.lower()
    gastos_raw = None
    ingresos_raw = None
    if n.endswith((".csv", ".txt")):
        try:
            crudo = pd.read_csv(io.BytesIO(contenido), sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError:
            crudo = pd.read_csv(io.BytesIO(contenido), sep=None, engine="python", encoding="latin-1")
    elif n.endswith((".xlsx", ".xlsm")):
        xls = pd.ExcelFile(io.BytesIO(contenido))
        hojas = {_slug(h): h for h in xls.sheet_names}
        crudo = xls.parse(hojas.get("documentos", xls.sheet_names[0]))
        if "gastos" in hojas:
            gastos_raw = xls.parse(hojas["gastos"])
        if "ingresos" in hojas:
            ingresos_raw = xls.parse(hojas["ingresos"])
    else:
        raise ValueError("Formato no soportado: sube un Excel (.xlsx) o un CSV.")
    docs, avisos = normalizar_documentos(crudo)
    return Carga(
        docs=docs,
        gastos=normalizar_gastos(gastos_raw),
        avisos=avisos,
        ingresos=normalizar_ingresos(ingresos_raw),
    )


def plantilla_excel() -> bytes:
    """Excel de ejemplo con las hojas Documentos, Gastos e Instrucciones."""
    from openpyxl.utils import get_column_letter

    hoy = pd.Timestamp.today().normalize()
    d = lambda n: (hoy + pd.Timedelta(days=n)).date()  # noqa: E731
    documentos = pd.DataFrame(
        [
            ["cobro", "Cliente Ejemplo 1", 850000, d(-40), d(-10), d(-3)],
            ["cobro", "Cliente Ejemplo 1", 920000, d(-20), d(10), None],
            ["cobro", "Cliente Ejemplo 2", 1500000, d(-35), d(-5), None],
            ["pago", "Proveedor Ejemplo", 700000, d(-10), d(15), None],
            ["pago", "Proveedor Ejemplo", 450000, d(-5), d(25), None],
        ],
        columns=["tipo", "contraparte", "monto", "fecha_emision", "fecha_vencimiento", "fecha_pago"],
    )
    gastos = pd.DataFrame(
        [["Arriendo", 600000, 5], ["Sueldos", 1800000, 28], ["Luz, agua e internet", 150000, 15]],
        columns=["concepto", "monto", "dia_mes"],
    )
    ingresos = pd.DataFrame(
        [["Ventas nuevas (estimado)", 2000000, 12], ["Ventas nuevas (estimado)", 2000000, 26]],
        columns=["concepto", "monto", "dia_mes"],
    )
    instrucciones = pd.DataFrame(
        {
            "Cómo llenar esta plantilla": [
                "Hoja Documentos: una fila por factura o boleta.",
                "tipo: «cobro» si te deben a ti, «pago» si tú le debes a alguien.",
                "fecha_pago: déjala vacía si aún no se paga. Las ya pagadas sirven para aprender cuánto se atrasa cada cliente.",
                "Hoja Gastos: gastos fijos que se repiten cada mes (arriendo, sueldos, etc.) y el día del mes en que se pagan.",
                "Hoja Ingresos (opcional): ventas que aún no facturas pero esperas cobrar cada mes, y el día en que suelen llegar. Si la dejas vacía, la proyección solo cuenta lo ya facturado y se verá más pesimista.",
                "El saldo actual de tu caja se ingresa directamente en la app.",
                "Borra las filas de ejemplo antes de subir tus datos.",
            ]
        }
    )
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        documentos.to_excel(xw, sheet_name="Documentos", index=False)
        gastos.to_excel(xw, sheet_name="Gastos", index=False)
        ingresos.to_excel(xw, sheet_name="Ingresos", index=False)
        instrucciones.to_excel(xw, sheet_name="Instrucciones", index=False)
        for ws in xw.book.worksheets:
            for i in range(1, ws.max_column + 1):
                ws.column_dimensions[get_column_letter(i)].width = 24 if ws.title != "Instrucciones" else 110
    return buf.getvalue()


# --------------------------------------------------------------------------
# Datos de ejemplo: una imprenta ficticia con clientes que pagan a distinto ritmo
# --------------------------------------------------------------------------

# nombre: (plazo en días, atraso medio, desviación, monto típico, n° de facturas históricas)
_CLIENTES = {
    "Colegio San Rafael": (30, 1, 2, 850_000, 6),
    "Clínica Sonrisa": (30, 8, 4, 420_000, 7),
    "Constructora Andes": (45, 24, 7, 1_900_000, 5),
    "Municipalidad de Costanera": (60, 38, 8, 2_400_000, 4),
    "Hogar Plus": (30, 16, 5, 1_300_000, 6),
}

# (cliente, monto, emitida hace N días)
_PENDIENTES_COBRO = [
    ("Municipalidad de Costanera", 2_450_000, 50),
    ("Municipalidad de Costanera", 2_300_000, 100),
    ("Constructora Andes", 1_850_000, 80),
    ("Constructora Andes", 2_100_000, 30),
    ("Hogar Plus", 1_350_000, 48),
    ("Hogar Plus", 1_200_000, 10),
    ("Hogar Plus", 600_000, 125),
    ("Clínica Sonrisa", 460_000, 40),
    ("Clínica Sonrisa", 380_000, 5),
    ("Colegio San Rafael", 900_000, 20),
    ("Colegio San Rafael", 820_000, 2),
]

# (proveedor, monto, vence en N días)
_PENDIENTES_PAGO = [
    ("Papelera del Sur", 1_650_000, 14),
    ("Tintas y Químicos Ltda.", 780_000, 20),
    ("Arriendo de maquinaria", 950_000, 25),
    ("Papelera del Sur", 2_300_000, 29),
    ("Planchas y Repuestos SpA", 2_600_000, 33),
]

# (concepto, monto, se paga dentro de N días; desde ahí se repite el mismo día de cada mes)
_GASTOS_DEMO = [
    ("Arriendo del local", 780_000, 9),
    ("Leasing camioneta", 420_000, 14),
    ("Luz, agua e internet", 190_000, 20),
    ("IVA y otros impuestos", 640_000, 22),
    ("Sueldos y leyes sociales", 2_350_000, 24),
]

# Ventas que aún no se facturan pero se esperan cada mes.
_INGRESOS_DEMO = [
    ("Ventas nuevas (estimado)", 1_600_000, 12),
    ("Ventas nuevas (estimado)", 1_600_000, 26),
]

SALDO_DEMO = 500_000


def generar_demo(hoy, semilla: int = 11) -> Demo:
    """Datos ficticios, siempre relativos a `hoy` y reproducibles."""
    hoy = pd.Timestamp(hoy).normalize()
    rnd = random.Random(semilla)
    filas = []

    # Historial de facturas ya pagadas: de aquí aprende el modelo cuánto se atrasa cada cliente.
    for cliente, (plazo, atraso_medio, desv, tipico, n) in _CLIENTES.items():
        min_off = plazo + atraso_medio + 2 * desv + 5
        for j in range(n):
            off = round(220 - j * (220 - min_off) / max(n - 1, 1))
            emision = hoy - pd.Timedelta(days=off)
            venc = emision + pd.Timedelta(days=plazo)
            atraso = max(-2, int(round(rnd.gauss(atraso_medio, desv))))
            pago = min(venc + pd.Timedelta(days=atraso), hoy - pd.Timedelta(days=1))
            monto = int(round(tipico * rnd.uniform(0.7, 1.3), -3))
            filas.append(["cobro", cliente, monto, emision, venc, pago])

    for cliente, monto, hace in _PENDIENTES_COBRO:
        plazo = _CLIENTES[cliente][0]
        emision = hoy - pd.Timedelta(days=hace)
        filas.append(["cobro", cliente, monto, emision, emision + pd.Timedelta(days=plazo), pd.NaT])

    for proveedor, monto, vence_en in _PENDIENTES_PAGO:
        venc = hoy + pd.Timedelta(days=vence_en)
        filas.append(["pago", proveedor, monto, venc - pd.Timedelta(days=30), venc, pd.NaT])

    crudo = pd.DataFrame(filas, columns=COLUMNAS_DOCS)
    docs, _ = normalizar_documentos(crudo)
    # Los días de pago se fijan relativos a "hoy" para que la demo cuente la misma historia cualquier día del año.
    def recurrentes(items):
        return pd.DataFrame(
            [(c, m, (hoy + pd.Timedelta(days=off)).day) for c, m, off in items],
            columns=["concepto", "monto", "dia_mes"],
        )

    return Demo(
        empresa="Imprenta Los Aromos (ejemplo)",
        docs=docs,
        gastos=normalizar_gastos(recurrentes(_GASTOS_DEMO)),
        saldo_inicial=SALDO_DEMO,
        ingresos=normalizar_ingresos(recurrentes(_INGRESOS_DEMO)),
    )
