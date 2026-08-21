"""Tests de instrumentos soberanos y sus convenciones de cotización.

Todo lo de acá corre con instrumentos **sintéticos**, sin tocar datos licenciados: son
propiedades de las convenciones, no comprobaciones contra una pantalla. La verificación
contra las cotizaciones reales de Refinitiv vive en ``test_calibracion_mercado.py``,
marcada ``licenciado`` porque depende de un archivo que no se versiona.

La afirmación central es que el exponente street/ISMA de :func:`tir_bono` colapsa al
compuesto anual entero cuando la liquidación cae justo en una fecha de cupón. Ese es el
caso límite que ancla la convención: si el exponente fuera ACT/365 transcurrido no
coincidiría, y esa discrepancia es el sesgo de −1 bp que la convención ISMA elimina.
"""

from __future__ import annotations

from datetime import date

import pytest

from motor_tes.curva_nss import NSSParams, duracion_macaulay, valor_presente
from motor_tes.instrumentos import (
    NOMINAL,
    Cotizacion,
    InstrumentoCotizado,
    InstrumentoTES,
    TipoInstrumento,
    aniversario,
    cronograma_cupones,
    flujos_descontables,
    interes_corrido,
    precio_bono,
    precio_letra,
    tir_bono,
    tir_letra,
)

#: Todos los precios de este módulo son inventados. Las convenciones se prueban por sus
#: propiedades —ida y vuelta, casos límite—, no contra cotizaciones reales, así que no
#: hace falta ningún dato licenciado acá.
#:
#: Fecha de liquidación de referencia para los casos sintéticos.
LIQUIDACION = date(2026, 8, 19)

#: Curva de referencia, la misma forma realista que usa ``test_curva_nss``.
CURVA = NSSParams(
    beta0=0.11, beta1=-0.02, beta2=0.03, beta3=-0.015, lambda1=1.5, lambda2=6.0
)


def bono(cupon: float, vencimiento: date, ric: str = "TEST=RR") -> InstrumentoTES:
    """Ficha de un bono sintético."""
    return InstrumentoTES(ric, "TEST", TipoInstrumento.BONO, cupon, vencimiento)


def letra(vencimiento: date, ric: str = "TESTB=RR") -> InstrumentoTES:
    """Ficha de una letra sintética."""
    return InstrumentoTES(ric, "TESTB", TipoInstrumento.LETRA, 0.0, vencimiento)


# ---------------------------------------------------------------------------
# Aritmética de fechas
# ---------------------------------------------------------------------------


class TestAniversario:
    def test_desplaza_anios_enteros(self) -> None:
        assert aniversario(date(2026, 8, 19), 5) == date(2031, 8, 19)
        assert aniversario(date(2026, 8, 19), -3) == date(2023, 8, 19)

    def test_el_29_de_febrero_cae_al_28_en_anio_comun(self) -> None:
        assert aniversario(date(2028, 2, 29), 1) == date(2029, 2, 28)

    def test_no_encadena_el_desplazamiento_y_conserva_el_29(self) -> None:
        """Retroceder cuatro años desde un 29-feb devuelve otro 29-feb.

        Si el desplazamiento se calculara encadenando saltos de un año, el primer año
        común convertiría la fecha en 28 y el día 29 se perdería para siempre.
        """
        assert aniversario(date(2032, 2, 29), -4) == date(2028, 2, 29)
        assert aniversario(date(2032, 2, 29), -2) == date(2030, 2, 28)


# ---------------------------------------------------------------------------
# Cronograma y devengo
# ---------------------------------------------------------------------------


class TestCronograma:
    def test_cupones_ascendentes_y_anuales(self) -> None:
        cupones, previo = cronograma_cupones(date(2031, 3, 26), LIQUIDACION)
        assert cupones == [
            date(2027, 3, 26),
            date(2028, 3, 26),
            date(2029, 3, 26),
            date(2030, 3, 26),
            date(2031, 3, 26),
        ]
        assert previo == date(2026, 3, 26)

    def test_el_previo_encierra_la_liquidacion(self) -> None:
        cupones, previo = cronograma_cupones(date(2033, 2, 9), LIQUIDACION)
        assert previo <= LIQUIDACION < cupones[0]

    def test_liquidacion_en_fecha_de_cupon(self) -> None:
        """Con liquidación sobre el cupón, el previo es la propia fecha."""
        cupones, previo = cronograma_cupones(date(2030, 8, 19), LIQUIDACION)
        assert previo == LIQUIDACION
        assert cupones[0] == date(2027, 8, 19)

    def test_rechaza_instrumento_vencido(self) -> None:
        with pytest.raises(ValueError, match="no quedan flujos"):
            cronograma_cupones(date(2020, 1, 1), LIQUIDACION)

    def test_rechaza_vencimiento_igual_a_liquidacion(self) -> None:
        with pytest.raises(ValueError, match="no quedan flujos"):
            cronograma_cupones(LIQUIDACION, LIQUIDACION)


class TestInteresCorrido:
    def test_es_cero_en_la_fecha_de_cupon(self) -> None:
        assert interes_corrido(0.12, LIQUIDACION, LIQUIDACION) == 0.0

    def test_casi_un_cupon_entero_a_tres_dias_del_proximo(self) -> None:
        """Caso del bono a 3 años de la muestra: paga cupón tres días después."""
        _, previo = cronograma_cupones(date(2029, 8, 22), LIQUIDACION)
        corrido = interes_corrido(0.11, LIQUIDACION, previo)
        assert corrido == pytest.approx(11.0 * 362 / 365, rel=1e-14)

    def test_atraviesa_un_ano_bisiesto(self) -> None:
        """Un período que contiene el 29-feb devenga 366 días, no 365."""
        corrido = interes_corrido(0.10, date(2028, 3, 1), date(2027, 3, 1))
        assert corrido == pytest.approx(10.0 * 366 / 365, rel=1e-14)

    def test_rechaza_cupon_previo_posterior(self) -> None:
        with pytest.raises(ValueError, match="no puede ser posterior"):
            interes_corrido(0.12, LIQUIDACION, date(2027, 1, 1))


# ---------------------------------------------------------------------------
# Bonos: convención ISMA
# ---------------------------------------------------------------------------


class TestBono:
    def test_ida_y_vuelta_precio_tir_precio(self) -> None:
        precio = 80.500
        tir = tir_bono(0.07, date(2031, 3, 26), LIQUIDACION, precio)
        assert precio_bono(0.07, date(2031, 3, 26), LIQUIDACION, tir) == pytest.approx(
            precio, rel=1e-12
        )

    def test_en_fecha_de_cupon_el_exponente_ismas_es_entero(self) -> None:
        """Liquidando sobre el cupón, ISMA colapsa al compuesto anual entero.

        Es el ancla de la convención: la fracción ``f`` vale exactamente 1, los
        exponentes quedan 1, 2, 3... y el precio tiene que coincidir con el descuento
        compuesto anual calculado a mano.
        """
        vencimiento, cupon, tir = date(2029, 8, 19), 0.11, 0.12
        precio = precio_bono(cupon, vencimiento, LIQUIDACION, tir)
        a_mano = sum(
            (NOMINAL * cupon + (NOMINAL if k == 3 else 0.0)) / (1.0 + tir) ** k
            for k in (1, 2, 3)
        )
        assert precio == pytest.approx(a_mano, rel=1e-14)

    def test_cotiza_a_la_par_cuando_la_tir_iguala_el_cupon(self) -> None:
        """En fecha de cupón y sin corrido, tir = cupón implica precio 100."""
        precio = precio_bono(0.12, date(2033, 8, 19), LIQUIDACION, 0.12)
        assert precio == pytest.approx(NOMINAL, rel=1e-13)

    def test_el_precio_cae_cuando_sube_la_tir(self) -> None:
        args = (0.07, date(2031, 3, 26), LIQUIDACION)
        assert precio_bono(*args, 0.13) < precio_bono(*args, 0.12)

    def test_un_bono_sin_cupon_es_un_descuento_puro(self) -> None:
        precio = precio_bono(0.0, date(2029, 8, 19), LIQUIDACION, 0.12)
        assert precio == pytest.approx(NOMINAL / 1.12**3, rel=1e-14)

    def test_rechaza_precio_no_positivo(self) -> None:
        with pytest.raises(ValueError, match="precio limpio debe ser positivo"):
            tir_bono(0.07, date(2031, 3, 26), LIQUIDACION, 0.0)

    def test_rechaza_tir_menor_a_menos_cien_por_ciento(self) -> None:
        with pytest.raises(ValueError, match="mayor a -100%"):
            precio_bono(0.07, date(2031, 3, 26), LIQUIDACION, -1.5)

    def test_rechaza_precio_fuera_del_intervalo_de_busqueda(self) -> None:
        with pytest.raises(ValueError, match="cae fuera del intervalo"):
            tir_bono(0.07, date(2031, 3, 26), LIQUIDACION, 1e-6)


# ---------------------------------------------------------------------------
# Letras: interés simple ACT/365
# ---------------------------------------------------------------------------


class TestLetra:
    def test_ida_y_vuelta_precio_tir_precio(self) -> None:
        precio = 92.000
        tir = tir_letra(date(2027, 3, 23), LIQUIDACION, precio)
        assert precio_letra(date(2027, 3, 23), LIQUIDACION, tir) == pytest.approx(
            precio, rel=1e-13
        )

    def test_replica_la_formula_de_interes_simple(self) -> None:
        vencimiento = date(2026, 10, 20)
        dias = (vencimiento - LIQUIDACION).days
        assert precio_letra(vencimiento, LIQUIDACION, 0.10) == pytest.approx(
            NOMINAL / (1.0 + 0.10 * dias / 365.0), rel=1e-15
        )

    def test_cotiza_bajo_la_par_con_tasa_positiva(self) -> None:
        assert precio_letra(date(2027, 3, 23), LIQUIDACION, 0.12) < NOMINAL

    def test_rechaza_letra_vencida(self) -> None:
        with pytest.raises(ValueError, match="no es posterior"):
            tir_letra(date(2020, 1, 1), LIQUIDACION, 99.0)

    def test_rechaza_precio_no_positivo(self) -> None:
        with pytest.raises(ValueError, match="precio debe ser positivo"):
            tir_letra(date(2027, 3, 23), LIQUIDACION, -1.0)


# ---------------------------------------------------------------------------
# Fichas, cotizaciones y la unidad que entra a la calibración
# ---------------------------------------------------------------------------


class TestFichaYCotizacion:
    def test_una_letra_no_puede_traer_cupon(self) -> None:
        with pytest.raises(ValueError, match="es cero cupón"):
            InstrumentoTES("X=RR", "X", TipoInstrumento.LETRA, 0.05, date(2027, 1, 1))

    def test_rechaza_cupon_negativo(self) -> None:
        with pytest.raises(ValueError, match="no puede ser negativo"):
            bono(-0.01, date(2031, 3, 26))

    def test_rechaza_puntas_cruzadas(self) -> None:
        with pytest.raises(ValueError, match="cruzadas"):
            Cotizacion("X=RR", LIQUIDACION, 99.0, 98.0, 0.12, 0.121)

    def test_medio_y_horquilla(self) -> None:
        cot = Cotizacion("X=RR", LIQUIDACION, 80.500, 80.700, 0.12500, 0.12450)
        assert cot.mid == pytest.approx(80.600, rel=1e-14)
        assert cot.mid_yield == pytest.approx(0.124750, rel=1e-14)
        assert cot.medio_spread_bps == pytest.approx(2.50, rel=1e-10)

    def test_sin_punta_vendedora_todo_se_apoya_en_el_bid(self) -> None:
        cot = Cotizacion("X=RR", LIQUIDACION, 92.000, None, 0.12100, None)
        assert cot.mid == 92.000
        assert cot.mid_yield == 0.12100
        assert cot.medio_spread_bps is None


class TestInstrumentoCotizado:
    def test_rechaza_ric_que_no_corresponde(self) -> None:
        with pytest.raises(ValueError, match="no corresponden al mismo instrumento"):
            InstrumentoCotizado(
                bono(0.07, date(2031, 3, 26), ric="A=RR"),
                Cotizacion("B=RR", LIQUIDACION, 82.4, None, 0.12, None),
            )

    def test_rechaza_instrumento_ya_vencido(self) -> None:
        with pytest.raises(ValueError, match="venció"):
            InstrumentoCotizado(
                bono(0.07, date(2020, 1, 1)),
                Cotizacion("TEST=RR", LIQUIDACION, 82.4, None, 0.12, None),
            )

    def test_el_plazo_corre_desde_su_propia_fecha_de_cotizacion(self) -> None:
        """Dos cotizaciones del mismo bono en días distintos dan plazos distintos.

        Es la propiedad que sostiene el tratamiento de la letra a 9 meses de la
        muestra, que cotiza un día antes que el resto.
        """
        ficha = bono(0.07, date(2031, 3, 26))
        hoy = InstrumentoCotizado(
            ficha, Cotizacion("TEST=RR", date(2026, 8, 19), 82.4, None, 0.12, None)
        )
        ayer = InstrumentoCotizado(
            ficha, Cotizacion("TEST=RR", date(2026, 8, 18), 82.4, None, 0.12, None)
        )
        assert ayer.plazo_anios - hoy.plazo_anios == pytest.approx(1 / 365, rel=1e-12)
        assert ayer.liquidacion == date(2026, 8, 18)

    def test_el_precio_sucio_suma_el_corrido(self) -> None:
        cot = InstrumentoCotizado(
            bono(0.07, date(2031, 3, 26)),
            Cotizacion("TEST=RR", LIQUIDACION, 80.500, 80.700, 0.12500, 0.12450),
        )
        assert cot.interes_corrido == pytest.approx(7.0 * 146 / 365, rel=1e-13)
        assert cot.precio_sucio == pytest.approx(cot.cotizacion.mid + cot.interes_corrido)

    def test_una_letra_no_devenga(self) -> None:
        cot = InstrumentoCotizado(
            letra(date(2027, 3, 23)),
            Cotizacion("TESTB=RR", LIQUIDACION, 92.000, None, 0.12100, None),
        )
        assert cot.interes_corrido == 0.0
        assert cot.precio_sucio == cot.cotizacion.mid
        assert cot.es_letra

    def test_los_flujos_de_un_bono_amortizan_al_vencimiento(self) -> None:
        cot = InstrumentoCotizado(
            bono(0.07, date(2031, 3, 26)),
            Cotizacion("TEST=RR", LIQUIDACION, 80.500, 80.700, 0.12500, 0.12450),
        )
        flujos = cot.flujos()
        assert len(flujos) == 5
        assert [f.t for f in flujos] == sorted(f.t for f in flujos)
        assert all(f.monto == pytest.approx(7.0) for f in flujos[:-1])
        assert flujos[-1].monto == pytest.approx(107.0)
        assert flujos[-1].t == pytest.approx(cot.plazo_anios, rel=1e-14)

    def test_una_letra_tiene_un_solo_flujo(self) -> None:
        cot = InstrumentoCotizado(
            letra(date(2027, 3, 23)),
            Cotizacion("TESTB=RR", LIQUIDACION, 92.000, None, 0.12100, None),
        )
        assert cot.flujos() == [
            type(cot.flujos()[0])(t=cot.plazo_anios, monto=NOMINAL)
        ]

    def test_los_flujos_se_descuentan_con_la_curva_del_motor(self) -> None:
        """Los flujos entran sin adaptador a las funciones existentes de la curva."""
        cot = InstrumentoCotizado(
            bono(0.07, date(2031, 3, 26)),
            Cotizacion("TEST=RR", LIQUIDACION, 80.500, 80.700, 0.12500, 0.12450),
        )
        flujos = cot.flujos()
        assert valor_presente(flujos, CURVA) > 0.0
        assert 0.0 < duracion_macaulay(flujos, CURVA) < cot.plazo_anios

    def test_la_tir_usa_la_convencion_del_tipo(self) -> None:
        b = InstrumentoCotizado(
            bono(0.07, date(2031, 3, 26)),
            Cotizacion("TEST=RR", LIQUIDACION, 80.500, 80.700, 0.12500, 0.12450),
        )
        assert b.tir(80.500) == pytest.approx(
            tir_bono(0.07, date(2031, 3, 26), LIQUIDACION, 80.500), rel=1e-14
        )
        el = InstrumentoCotizado(
            letra(date(2027, 3, 23)),
            Cotizacion("TESTB=RR", LIQUIDACION, 92.000, None, 0.12100, None),
        )
        assert el.tir() == pytest.approx(
            tir_letra(date(2027, 3, 23), LIQUIDACION, 92.000), rel=1e-14
        )

    def test_flujos_descontables_respeta_el_orden_de_entrada(self) -> None:
        canasta = [
            InstrumentoCotizado(
                letra(date(2027, 3, 23)),
                Cotizacion("TESTB=RR", LIQUIDACION, 92.000, None, 0.12100, None),
            ),
            InstrumentoCotizado(
                bono(0.07, date(2031, 3, 26)),
                Cotizacion("TEST=RR", LIQUIDACION, 80.500, None, 0.12500, None),
            ),
        ]
        flujos = flujos_descontables(canasta)
        assert [len(f) for f in flujos] == [1, 5]
