"""Prueba la app completa en modo headless con el AppTest de Streamlit."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def _correr() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    return at


def _widget(lista, etiqueta):
    return next(w for w in lista if w.label.startswith(etiqueta))


def _textos(elementos):
    return " ".join(e.value for e in elementos)


def test_la_demo_abre_sin_errores_y_avisa_del_faltante():
    at = _correr()
    assert not at.exception
    assert "Te quedarías sin caja" in _textos(at.error)
    assert len(at.metric) == 4
    assert len(at.tabs) == 5


def test_escenario_optimista_dice_que_la_caja_aguanta():
    at = _correr()
    _widget(at.select_slider, "¿Cómo pagan").set_value("Optimista").run()
    assert not at.exception
    assert "Tu caja aguanta" in _textos(at.success)


def test_escenario_pesimista_empeora_el_faltante():
    at = _correr()
    _widget(at.select_slider, "¿Cómo pagan").set_value("Pesimista").run()
    assert not at.exception
    assert "Te quedarías sin caja" in _textos(at.error)


def test_simular_que_un_cliente_se_atrasa():
    at = _correr()
    _widget(at.selectbox, "¿Y si este cliente").set_value("Constructora Andes").run()
    _widget(at.slider, "Días extra de atraso").set_value(30).run()
    assert not at.exception
    assert "simulando 30 días extra de atraso de Constructora Andes" in _textos(at.caption)


def test_subir_archivo_sin_archivo_muestra_la_bienvenida():
    at = _correr()
    at.radio[0].set_value("Subir mi archivo").run()
    assert not at.exception
    assert "Primera vez" in _textos(at.info)


def test_cambiar_semanas_y_fecha_no_rompe_nada():
    at = _correr()
    _widget(at.slider, "Semanas a proyectar").set_value(16).run()
    assert not at.exception
    assert len(at.tabs) == 5


def test_ningun_texto_deja_un_signo_peso_sin_escapar():
    """Streamlit interpreta '$...$' como fórmula LaTeX: todo monto en texto markdown debe ir escapado (\\$)."""
    at = _correr()
    for esc in ("Esperado", "Pesimista", "Optimista"):
        _widget(at.select_slider, "¿Cómo pagan").set_value(esc).run()
        assert not at.exception
        for grupo in (at.markdown, at.error, at.success, at.warning, at.info, at.caption):
            for el in grupo:
                assert "$" not in el.value.replace("\\$", ""), el.value[:120]


def test_los_decimales_van_con_coma():
    at = _correr()
    texto = " ".join(m.value for m in at.markdown)
    assert "semanas de salidas promedio" in texto
    assert "0.3 semanas" not in texto and "0,3 semanas" in texto
