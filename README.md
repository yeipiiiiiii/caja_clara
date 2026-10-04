# Caja Clara

**¿Te va a alcanzar la plata en las próximas semanas?**
Prototipo de una herramienta para micro y pequeños negocios chilenos: toma tus facturas por cobrar, tus pagos a proveedores y tus gastos fijos, aprende **cuánto se atrasa realmente cada cliente en pagarte**, y proyecta tu caja de 8 a 16 semanas. Si en algún momento quedas en negativo, te dice **cuándo**, **por cuánto** y **qué hacer** (a quién cobrar primero, qué facturas adelantar y cuánto cuesta, qué pagos negociar).

> Prototipo educativo. Todo funciona con **datos de ejemplo ficticios** ("Imprenta Los Aromos"). Las proyecciones son estimaciones y no constituyen asesoría financiera.

## Por qué existe

La mayoría de las pymes no quiebra por falta de ventas, sino por **descalce de caja**: pagan a 30 días y les pagan a 60. Las planillas asumen que cada cliente paga en la fecha de vencimiento; en la realidad casi ninguno lo hace. Caja Clara usa el historial de pagos para proyectar con los atrasos reales de cada cliente.

Tampoco depende de APIs bancarias (Open Finance en Chile está postergado a 2027): funciona con un Excel o CSV que el dueño ya tiene o puede exportar de su sistema de facturación.

## Cómo correrlo

```bash
python -m venv .venv
source .venv/bin/activate        # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Se abre en <http://localhost:8501> con los datos de ejemplo cargados. Para usar tus datos: **Subir mi archivo** en el menú lateral → descarga la plantilla de Excel, reemplaza los ejemplos y súbela.

### Tests

```bash
python -m pytest -q
```

45 tests: motor de cálculo, lectura de archivos, plan de rescate, anonimización y la app completa en modo headless (`streamlit.testing`).

## Qué hace

| Pestaña | Contenido |
|---|---|
| **Proyección** | Saldo de caja día a día (el tramo en negativo se pinta en rojo y el punto más bajo lleva etiqueta), entradas/salidas por semana y tabla semanal. |
| **Cobranza** | Antigüedad de lo que te deben, ranking de a quién cobrar primero (monto × días de atraso) con alerta si alguien paga más tarde de lo habitual, y mensajes listos para enviar con tono según el atraso. |
| **Plan de rescate** | **A:** adelantar facturas (factoring) eligiendo las más baratas hasta que la caja no baje de cero, re-proyectando todo el flujo y mostrando el costo. **B:** pedir más plazo a los proveedores más grandes antes del punto crítico. |
| **Explicación** | Resumen en lenguaje simple (siempre disponible, sin IA) y, opcionalmente, una versión redactada por Claude. |
| **Datos** | Atraso estimado por cliente, facturas cargadas y cada movimiento proyectado (para auditar de dónde sale cada cifra). |

**Escenarios** (menú lateral): *Optimista* (todos pagan al vencimiento), *Esperado* (atraso histórico de cada cliente) y *Pesimista* (atraso histórico + 15 días). También puedes simular **"¿y si este cliente se atrasa X días más?"**.

## Cómo calcula

- **Atraso por cliente:** mediana de los días entre vencimiento y pago de sus facturas ya pagadas, mezclada con la mediana general cuando hay pocos datos: `(n·mediana_cliente + 3·mediana_general) / (n + 3)`. Nunca baja de 0: que alguien pague antes no adelanta tu caja.
- **Facturas ya vencidas:** en *Esperado* se asume cobro a los 7 días; las que llevan más de 90 días vencidas no se cuentan (se informan aparte).
- **Gastos fijos e ingresos esperados:** se repiten cada mes en el día indicado (día 31 = fin de mes).
- **Factoring:** costo = monto adelantado × tasa mensual × días adelantados / 30 + comisión. Los valores por defecto (1,5 % mensual, 90 % de anticipo, 2 días de desembolso) son **supuestos editables**: pide una cotización real.

## Privacidad al usar Claude (opcional)

La explicación base **no usa IA ni internet**. Si pegas una clave de API de Anthropic (o defines `ANTHROPIC_API_KEY`), puedes pedir una versión más natural. Antes de enviar nada:

- los nombres de clientes y proveedores se reemplazan por alias ("Cliente 1", "Proveedor 1");
- los montos se redondean a $10.000;
- no se envían tus archivos ni las fechas de las facturas.

Los nombres reales se restituyen localmente al mostrar la respuesta. En la pestaña *Explicación* puedes ver exactamente qué se envía.

## Estructura

```
app.py                  Interfaz (Streamlit)
cajaclara/
  datos.py              Lectura/validación de Excel y CSV, plantilla, datos de ejemplo
  modelo.py             Aprendizaje de atrasos, escenarios y proyección de caja
  cobranza.py           Antigüedad, prioridades y mensajes de cobranza
  rescate.py            Factoring y negociación de plazos
  explicador.py         Explicación local y con Claude (con anonimización)
  graficos.py           Gráficos (Altair)
  formato.py            Pesos chilenos y fechas en español
tests/                  pytest
```

## Limitaciones conocidas

- Con pocos clientes o pocas facturas pagadas, el atraso estimado es poco confiable (por eso se mezcla con el promedio general).
- No modela IVA, estacionalidad, ni costos de ventas nuevas: los ingresos esperados se ingresan a mano.
- No hay cuentas de usuario ni almacenamiento: los datos viven solo en la sesión del navegador.
- Falta validarlo con negocios reales: es la prueba que más importa antes de seguir construyendo.

## Próximos pasos sugeridos

1. Probarlo con ~5 pymes reales (con sus datos anonimizados) y medir si la proyección habría anticipado sus apreturas.
2. Importar directo desde exportaciones de SII / sistemas de facturación.
3. Alertas semanales por correo o WhatsApp.
