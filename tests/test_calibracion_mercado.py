"""Tests de la calibración contra precios de instrumentos.

La prueba central vuelve a ser la **recuperación de parámetros sintéticos**, igual que
en ``test_curva_nss``, pero un paso más atrás en la cadena: en vez de generar tasas con
una curva conocida y recalibrar sobre ellas, se generan **precios** de bonos y letras
descontando sus flujos con la curva conocida, y se verifica que calibrar sobre esos
precios devuelve la misma curva. Eso ejercita el camino completo —cronograma, devengo,
descuento, ponderación por duración— y no solo la parametrización.

Las comprobaciones contra cotizaciones reales están marcadas ``licenciado`` y se saltan
solas cuando el archivo de precios no está, que es el caso en cualquier clon del
repositorio: los precios son de un proveedor comercial y no se versionan.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from motor_tes.calibracion_mercado import (
    calibrar_desde_precios,
    comparar_curvas,
)
from motor_tes.curva_nss import NSSParams, tasa_cero_cupon, valor_presente
from motor_tes.instrumentos import (
    Cotizacion,
    InstrumentoCotizado,
    InstrumentoTES,
    TipoInstrumento,
    cronograma_cupones,
    interes_corrido,
)

LIQUIDACION = date(2026, 8, 19)

#: La misma curva de referencia que usa ``test_curva_nss``.
CURVA_VERDADERA = NSSParams(
    beta0=0.11, beta1=-0.02, beta2=0.03, beta3=-0.015, lambda1=1.5, lambda2=6.0
)

#: Canasta con forma realista: dos letras cortas y diez bonos repartidos hasta 30 años.
#: ``(etiqueta, tipo, cupón, vencimiento)``.
CANASTA = [
    ("6M", TipoInstrumento.LETRA, 0.0, date(2027, 2, 19)),
    ("1Y", TipoInstrumento.LETRA, 0.0, date(2027, 8, 19)),
    ("2Y", TipoInstrumento.BONO, 0.060, date(2028, 4, 28)),
    ("3Y", TipoInstrumento.BONO, 0.110, date(2029, 8, 22)),
    ("4Y", TipoInstrumento.BONO, 0.125, date(2030, 2, 27)),
    ("5Y", TipoInstrumento.BONO, 0.070, date(2031, 3, 26)),
    ("7Y", TipoInstrumento.BONO, 0.1325, date(2033, 2, 9)),
    ("10Y", TipoInstrumento.BONO, 0.0625, date(2036, 7, 9)),
    ("15Y", TipoInstrumento.BONO, 0.0925, date(2042, 5, 28)),
    ("20Y", TipoInstrumento.BONO, 0.115, date(2046, 7, 25)),
    ("25Y", TipoInstrumento.BONO, 0.0725, date(2050, 10, 26)),
    ("30Y", TipoInstrumento.BONO, 0.120, date(2058, 3, 13)),
]


def cotizado_sintetico(
    etiqueta: str,
    tipo: TipoInstrumento,
    cupon: float,
    vencimiento: date,
    curva: NSSParams = CURVA_VERDADERA,
    liquidacion: date = LIQUIDACION,
) -> InstrumentoCotizado:
    """Instrumento cuyo precio sale de descontar sus flujos con ``curva``.

    Al valorarlo exactamente con la curva, el precio observado y el del modelo coinciden
    por construcción: si la calibración funciona, el residual tiene que ser nulo.
    """
    ric = f"{etiqueta}=SYN"
    ficha = InstrumentoTES(ric, etiqueta, tipo, cupon, vencimiento)
    provisorio = InstrumentoCotizado(
        ficha, Cotizacion(ric, liquidacion, 1.0, None, 0.0, None)
    )
    sucio = valor_presente(provisorio.flujos(), curva)
    if tipo is TipoInstrumento.LETRA:
        corrido = 0.0
    else:
        _, previo = cronograma_cupones(vencimiento, liquidacion)
        corrido = interes_corrido(cupon, liquidacion, previo)
    limpio = sucio - corrido
    definitivo = InstrumentoCotizado(
        ficha, Cotizacion(ric, liquidacion, limpio, limpio, 0.0, 0.0)
    )
    tir = definitivo.tir()
    return InstrumentoCotizado(
        ficha, Cotizacion(ric, liquidacion, limpio, limpio, tir, tir)
    )


@pytest.fixture
def canasta_sintetica() -> list[InstrumentoCotizado]:
    """Los doce instrumentos de :data:`CANASTA` valorados con la curva verdadera."""
    return [cotizado_sintetico(*fila) for fila in CANASTA]


# ---------------------------------------------------------------------------
# Recuperación de la curva
# ---------------------------------------------------------------------------


class TestRecuperacionSintetica:
    def test_recupera_la_curva_desde_precios(self, canasta_sintetica) -> None:
        """El test central: precios generados por una curva la devuelven al calibrar."""
        resultado = calibrar_desde_precios(canasta_sintetica, svensson=True)
        assert resultado.exito
        assert resultado.params.curva_equivalente_a(
            CURVA_VERDADERA, t_max=20.0, tol_bps=1.0
        )

    def test_los_residuales_son_practicamente_nulos(self, canasta_sintetica) -> None:
        resultado = calibrar_desde_precios(canasta_sintetica, svensson=True)
        assert resultado.rmse_bps < 0.5
        assert resultado.max_residuo_bps < 1.0

    def test_tambien_recupera_con_nelson_siegel_de_cuatro_parametros(self) -> None:
        """Sin el segundo término de curvatura, contra una curva NS pura."""
        curva_ns = NSSParams(0.115, -0.025, 0.02, 0.0, 2.0, 2.0)
        canasta = [cotizado_sintetico(*fila, curva=curva_ns) for fila in CANASTA]
        resultado = calibrar_desde_precios(canasta, svensson=False)
        assert resultado.params.curva_equivalente_a(curva_ns, t_max=20.0, tol_bps=1.0)

    def test_el_multistart_prueba_varios_arranques(self, canasta_sintetica) -> None:
        resultado = calibrar_desde_precios(canasta_sintetica, svensson=True)
        assert resultado.n_arranques > 1

    def test_los_grados_de_libertad_cuentan_solo_los_ajustados(
        self, canasta_sintetica
    ) -> None:
        """Doce instrumentos menos seis parámetros: Svensson por fin es estimable."""
        resultado = calibrar_desde_precios(
            canasta_sintetica, svensson=True, plazo_minimo_anios=0.0
        )
        assert len(resultado.ajustados) == 12
        assert resultado.grados_libertad == 6
        assert not resultado.es_interpolacion


# ---------------------------------------------------------------------------
# Segmentación del tramo corto
# ---------------------------------------------------------------------------


class TestSegmentacionDelTramoCorto:
    def test_los_cortos_se_reportan_pero_no_se_ajustan(self, canasta_sintetica) -> None:
        """Quedan fuera del objetivo y aun así traen residual calculado."""
        resultado = calibrar_desde_precios(canasta_sintetica, plazo_minimo_anios=1.5)
        fuera = resultado.no_ajustados
        assert {r.etiqueta for r in fuera} == {"6M", "1Y"}
        assert all(np.isfinite(r.residuo_bps) for r in fuera)
        assert all(r.precio_modelo > 0.0 for r in fuera)

    def test_el_rmse_ignora_a_los_no_ajustados(self, canasta_sintetica) -> None:
        resultado = calibrar_desde_precios(canasta_sintetica, plazo_minimo_anios=1.5)
        bps = np.array([r.residuo_bps for r in resultado.ajustados])
        assert resultado.rmse_bps == pytest.approx(
            float(np.sqrt(np.mean(bps**2))), rel=1e-12
        )

    def test_la_tabla_trae_todos_los_instrumentos(self, canasta_sintetica) -> None:
        resultado = calibrar_desde_precios(canasta_sintetica, plazo_minimo_anios=1.5)
        tabla = resultado.tabla()
        assert len(tabla) == 12
        assert tabla["ajustado"].sum() == 10

    def test_el_plazo_minimo_es_inclusivo(self, canasta_sintetica) -> None:
        """Un instrumento que cae justo en el umbral entra al ajuste.

        La letra a un año vence exactamente 365 días después de la liquidación, así que
        su plazo es 1.0 clavado: sirve para fijar de qué lado cae el borde.
        """
        resultado = calibrar_desde_precios(canasta_sintetica, plazo_minimo_anios=1.0)
        al_borde = next(r for r in resultado.residuos if r.etiqueta == "1Y")
        assert al_borde.plazo_anios == pytest.approx(1.0, rel=1e-15)
        assert al_borde.ajustado


# ---------------------------------------------------------------------------
# Fechas de cotización heterogéneas
# ---------------------------------------------------------------------------


class TestFechas:
    def test_la_curva_es_tan_fresca_como_su_insumo_mas_viejo(self) -> None:
        """Con cotizaciones de días distintos, la antigüedad queda explícita."""
        canasta = [cotizado_sintetico(*fila) for fila in CANASTA]
        rezagado = cotizado_sintetico(*CANASTA[5], liquidacion=date(2026, 8, 18))
        canasta[5] = rezagado
        resultado = calibrar_desde_precios(canasta)
        assert resultado.fecha_valoracion == date(2026, 8, 19)
        assert resultado.fecha_mas_antigua == date(2026, 8, 18)
        assert resultado.antiguedad_maxima_dias == 1

    def test_sin_rezago_la_antiguedad_es_cero(self, canasta_sintetica) -> None:
        resultado = calibrar_desde_precios(canasta_sintetica)
        assert resultado.antiguedad_maxima_dias == 0
        assert resultado.fecha_valoracion == resultado.fecha_mas_antigua


# ---------------------------------------------------------------------------
# Validación de entradas
# ---------------------------------------------------------------------------


class TestValidacion:
    def test_rechaza_canasta_vacia(self) -> None:
        with pytest.raises(ValueError, match="está vacía"):
            calibrar_desde_precios([])

    def test_rechaza_instrumentos_repetidos(self, canasta_sintetica) -> None:
        with pytest.raises(ValueError, match="repetidos"):
            calibrar_desde_precios([*canasta_sintetica, canasta_sintetica[3]])

    def test_rechaza_si_nadie_supera_el_plazo_minimo(self, canasta_sintetica) -> None:
        with pytest.raises(ValueError, match="Ningún instrumento supera"):
            calibrar_desde_precios(canasta_sintetica, plazo_minimo_anios=50.0)

    def test_rechaza_sistema_subdeterminado(self, canasta_sintetica) -> None:
        with pytest.raises(ValueError, match="subdeterminado"):
            calibrar_desde_precios(canasta_sintetica[:5], svensson=True)

    def test_con_cuatro_parametros_cinco_instrumentos_alcanzan(
        self, canasta_sintetica
    ) -> None:
        """La misma canasta que Svensson rechaza le sirve a Nelson-Siegel."""
        resultado = calibrar_desde_precios(canasta_sintetica[:5], svensson=False)
        assert resultado.grados_libertad == 1


# ---------------------------------------------------------------------------
# Comparación de curvas
# ---------------------------------------------------------------------------


class TestCompararCurvas:
    def test_una_curva_contra_si_misma_no_difiere(self) -> None:
        tabla = comparar_curvas(CURVA_VERDADERA, CURVA_VERDADERA, [1.0, 5.0, 10.0])
        np.testing.assert_allclose(tabla["diferencia_bps"], 0.0, atol=1e-9)

    def test_un_desplazamiento_paralelo_se_ve_igual_en_todo_plazo(self) -> None:
        """Subir beta0 en 22.8 bps desplaza la curva entera esa misma cantidad."""
        desplazada = NSSParams(
            CURVA_VERDADERA.beta0 + 22.8e-4,
            CURVA_VERDADERA.beta1,
            CURVA_VERDADERA.beta2,
            CURVA_VERDADERA.beta3,
            CURVA_VERDADERA.lambda1,
            CURVA_VERDADERA.lambda2,
        )
        tabla = comparar_curvas(desplazada, CURVA_VERDADERA, [0.5, 1.0, 5.0, 10.0, 30.0])
        np.testing.assert_allclose(tabla["diferencia_bps"], 22.8, atol=1e-9)

    def test_respeta_los_nombres_de_columna(self) -> None:
        tabla = comparar_curvas(
            CURVA_VERDADERA,
            CURVA_VERDADERA,
            [1.0],
            nombre_referencia="banrep",
            nombre_contraste="mercado",
        )
        assert list(tabla.columns) == [
            "plazo_anios",
            "banrep",
            "mercado",
            "diferencia_bps",
        ]

    def test_las_tasas_coinciden_con_la_curva(self) -> None:
        tabla = comparar_curvas(CURVA_VERDADERA, CURVA_VERDADERA, [2.0, 7.0])
        esperado = np.asarray(tasa_cero_cupon(np.array([2.0, 7.0]), CURVA_VERDADERA))
        np.testing.assert_allclose(tabla["referencia"], esperado, rtol=1e-15)

    @pytest.mark.parametrize("plazos", [[], [0.0, 1.0], [-1.0]])
    def test_rechaza_plazos_invalidos(self, plazos) -> None:
        with pytest.raises(ValueError):
            comparar_curvas(CURVA_VERDADERA, CURVA_VERDADERA, plazos)
