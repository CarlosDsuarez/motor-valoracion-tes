"""Validación cruzada contra QuantLib.

Estos tests son el árbitro externo del motor: si una convención está mal aplicada, aquí
se nota aunque el resto de la suite esté en verde. Van marcados con ``quantlib`` para
poder excluirlos si la dependencia no está instalada.
"""

from __future__ import annotations

import pytest

from motor_tes.curva_nss import NSSParams

ql = pytest.importorskip("QuantLib", reason="QuantLib no está instalado")

from motor_tes.benchmark_quantlib import (  # noqa: E402
    TOLERANCIA_DF,
    _bono_quantlib,
    comparar_bono,
    comparar_factores_descuento,
    comparar_forward,
)

pytestmark = pytest.mark.quantlib

CURVA = NSSParams(0.11, -0.02, 0.03, -0.015, 1.5, 6.0)
SPOT = 3144.14
SOFR = 0.0366


class TestFactoresDescuento:
    def test_coinciden_en_todos_los_plazos(self) -> None:
        for resultado in comparar_factores_descuento(CURVA):
            assert resultado.pasa, str(resultado)

    def test_la_coincidencia_es_a_precision_de_maquina(self) -> None:
        """No es que entren en una tolerancia holgada: son el mismo número."""
        for resultado in comparar_factores_descuento(CURVA):
            assert resultado.desvio < 1e-14, str(resultado)

    def test_tambien_coinciden_en_una_curva_plana(self) -> None:
        plana = NSSParams(0.10, 0.0, 0.0, 0.0, 1.0, 1.0)
        for resultado in comparar_factores_descuento(plana):
            assert resultado.pasa, str(resultado)


class TestBono:
    @pytest.mark.parametrize(
        "cupon, plazo, frecuencia",
        [(0.1325, 7, 1), (0.08, 5, 2), (0.10, 10, 1), (0.0, 3, 1)],
    )
    def test_valor_presente_dv01_y_duracion(self, cupon, plazo, frecuencia) -> None:
        for resultado in comparar_bono(
            CURVA, cupon=cupon, plazo_anios=plazo, frecuencia=frecuencia
        ):
            assert resultado.pasa, str(resultado)

    def test_los_flujos_de_quantlib_caen_mas_tarde_por_dias_bisiestos(self) -> None:
        """Primer efecto: fechas de calendario reales en vez de años exactos."""
        evaluacion = ql.Date(3, 8, 2026)
        dias = (evaluacion + ql.Period(7, ql.Years)) - evaluacion
        assert dias == 2557  # 7*365 = 2555, más dos 29 de febrero
        assert dias / 365.0 == pytest.approx(7.005479, abs=1e-6)

    def test_quantlib_paga_cupon_mayor_en_periodos_bisiestos(self) -> None:
        """Segundo efecto, de signo opuesto: Actual365Fixed devenga 366/365.

        Es la razón por la que el desvío no tiene un signo único. El motor propio paga
        siempre ``cupon/frecuencia``; QuantLib escala por los días efectivamente
        devengados, así que los períodos con 29 de febrero pagan más.
        """
        ql.Settings.instance().evaluationDate = ql.Date(3, 8, 2026)
        bono = _bono_quantlib(0.1325, 7, 1, 100.0)
        montos = sorted({round(cf.amount(), 6) for cf in bono.cashflows()})

        assert 13.25 in montos  # períodos normales
        assert pytest.approx(13.25 * 366 / 365, abs=1e-6) in montos  # períodos bisiestos

    def test_sin_bisiestos_de_por_medio_el_desvio_casi_desaparece(self) -> None:
        """Contraprueba de la atribución: a 1 año no hay 29 de febrero en el camino."""
        a_un_anio = next(
            r
            for r in comparar_bono(CURVA, cupon=0.10, plazo_anios=1, frecuencia=1)
            if r.magnitud == "valor_presente"
        )
        a_siete_anios = next(
            r for r in comparar_bono(CURVA) if r.magnitud == "valor_presente"
        )
        assert a_un_anio.desvio < 1e-5
        assert a_un_anio.desvio < a_siete_anios.desvio

    def test_el_desvio_del_bono_se_mantiene_acotado(self) -> None:
        """Sea cual sea el signo, la magnitud tiene que quedar en el orden esperado."""
        for plazo in (1, 5, 7, 10):
            vp = next(
                r
                for r in comparar_bono(CURVA, cupon=0.10, plazo_anios=plazo)
                if r.magnitud == "valor_presente"
            )
            assert vp.desvio < 1e-4, str(vp)


class TestForward:
    def test_coincide_con_la_construccion_por_factores_de_descuento(self) -> None:
        for resultado in comparar_forward(CURVA, spot=SPOT, i_usd=SOFR):
            assert resultado.pasa, str(resultado)

    def test_la_coincidencia_es_a_precision_de_maquina(self) -> None:
        for resultado in comparar_forward(CURVA, spot=SPOT, i_usd=SOFR):
            assert resultado.desvio < 1e-12, str(resultado)


class TestResultadoComparacion:
    def test_detecta_una_discrepancia_real(self) -> None:
        """El comparador tiene que ser capaz de fallar, no solo de aprobar."""
        resultado = comparar_factores_descuento(CURVA)[0]
        falso = type(resultado)(
            magnitud=resultado.magnitud,
            detalle=resultado.detalle,
            propio=resultado.quantlib + 10 * TOLERANCIA_DF,
            quantlib=resultado.quantlib,
            tolerancia=TOLERANCIA_DF,
            relativa=False,
        )
        assert not falso.pasa
        assert "FUERA DE TOLERANCIA" in str(falso)
