"""Tests del pricer de forwards USD/COP.

Estrategia: casos con solución analítica conocida (tasas iguales, plazo cero,
diferencial calculado a mano) más contraste de cada griega analítica contra
diferencias finitas. Nada depende de la red.
"""

from __future__ import annotations

import numpy as np
import pytest

from motor_tes.config import ConvencionTasa
from motor_tes.curva_nss import UN_BP, NSSParams, tasa_cero_cupon
from motor_tes.pricer_forward import (
    comparar_convenciones,
    factor_capitalizacion,
    pricer_forward,
    tasa_cop_del_plazo,
)

#: Spot de referencia: TRM del 2026-08-03 verificada contra SUAMECA y datos.gov.co.
SPOT = 3144.14

#: SOFR del 2026-07-31 verificado contra la API de la Fed de Nueva York.
SOFR = 0.0366

#: Curva COP de prueba con nivel ~11% y forma realista.
CURVA_COP = NSSParams(0.11, -0.02, 0.03, -0.015, 1.5, 6.0)


# ---------------------------------------------------------------------------
# Factores de capitalización
# ---------------------------------------------------------------------------


class TestFactorCapitalizacion:
    def test_plazo_cero_no_capitaliza(self) -> None:
        for convencion in ConvencionTasa:
            assert factor_capitalizacion(0.12, 0, convencion) == 1.0

    def test_simple_360_es_lineal(self) -> None:
        assert factor_capitalizacion(
            0.12, 180, ConvencionTasa.SIMPLE_360
        ) == pytest.approx(1.0 + 0.12 * 180 / 360)

    def test_ea_365_capitaliza_de_forma_compuesta(self) -> None:
        assert factor_capitalizacion(0.12, 365, ConvencionTasa.EA_365) == pytest.approx(
            1.12
        )
        assert factor_capitalizacion(0.12, 730, ConvencionTasa.EA_365) == pytest.approx(
            1.12**2
        )

    def test_compuesto_supera_al_simple_mas_alla_del_ano(self) -> None:
        """Con la misma tasa nominal, capitalizar compuesto rinde más a plazos largos."""
        compuesto = factor_capitalizacion(0.12, 730, ConvencionTasa.EA_365)
        simple = factor_capitalizacion(0.12, 730, ConvencionTasa.SIMPLE_360)
        assert compuesto > simple

    def test_rechaza_plazo_negativo(self) -> None:
        with pytest.raises(ValueError, match="no negativo"):
            factor_capitalizacion(0.12, -1, ConvencionTasa.EA_365)

    def test_rechaza_tasa_que_aniquila_el_principal(self) -> None:
        with pytest.raises(ValueError, match="no es positivo"):
            factor_capitalizacion(-1.5, 365, ConvencionTasa.EA_365)
        with pytest.raises(ValueError, match="no positivo"):
            factor_capitalizacion(-3.0, 180, ConvencionTasa.SIMPLE_360)


# ---------------------------------------------------------------------------
# Paridad cubierta: casos analíticos
# ---------------------------------------------------------------------------


class TestParidadCubierta:
    def test_tasas_iguales_dejan_el_forward_en_el_spot(self) -> None:
        """Sin diferencial de tasas no hay puntos forward, en cualquier plazo."""
        for dias in (30, 90, 180, 360, 720):
            res = pricer_forward(
                SPOT,
                dias,
                i_usd=0.05,
                i_cop=0.05,
                convencion_cop=ConvencionTasa.SIMPLE_360,
            )
            assert res.precio == pytest.approx(SPOT, rel=1e-14)
            assert res.puntos_forward == pytest.approx(0.0, abs=1e-10)

    def test_plazo_cero_devuelve_el_spot(self) -> None:
        res = pricer_forward(SPOT, 0, i_usd=SOFR, i_cop=0.12)
        assert res.precio == pytest.approx(SPOT)
        assert res.puntos_forward == pytest.approx(0.0)
        assert res.devaluacion_anualizada == 0.0

    def test_diferencial_conocido_verificado_a_mano(self) -> None:
        """F = 3000 * (1 + 0.12*180/360) / (1 + 0.04*180/360) = 3000*1.06/1.02."""
        res = pricer_forward(
            3000.0,
            180,
            i_usd=0.04,
            i_cop=0.12,
            convencion_cop=ConvencionTasa.SIMPLE_360,
        )
        assert res.precio == pytest.approx(3000.0 * 1.06 / 1.02, rel=1e-14)
        assert res.factor_cop == pytest.approx(1.06)
        assert res.factor_usd == pytest.approx(1.02)

    def test_tasa_local_mayor_implica_forward_sobre_el_spot(self) -> None:
        """Colombia paga más que USD: el peso se deprecia a futuro (devaluación > 0)."""
        res = pricer_forward(SPOT, 180, i_usd=SOFR, params_curva_cop=CURVA_COP)
        assert res.precio > SPOT
        assert res.puntos_forward > 0
        assert res.devaluacion_anualizada > 0

    def test_el_precio_crece_con_el_diferencial(self) -> None:
        precios = [
            pricer_forward(SPOT, 180, i_usd=SOFR, i_cop=i).precio
            for i in (0.06, 0.09, 0.12, 0.15)
        ]
        assert precios == sorted(precios)

    def test_devaluacion_anualizada_reconstruye_el_precio(self) -> None:
        res = pricer_forward(SPOT, 270, i_usd=SOFR, params_curva_cop=CURVA_COP)
        reconstruido = SPOT * (1 + res.devaluacion_anualizada) ** (270 / 365)
        assert reconstruido == pytest.approx(res.precio, rel=1e-12)

    def test_toma_la_tasa_de_la_curva_al_plazo_correcto(self) -> None:
        res = pricer_forward(SPOT, 180, i_usd=SOFR, params_curva_cop=CURVA_COP)
        assert res.i_cop == pytest.approx(float(tasa_cero_cupon(180 / 365, CURVA_COP)))

    def test_tasas_negativas_no_rompen_el_pricer(self) -> None:
        """Escenario tipo zona euro: la pata extranjera puede cotizar negativa."""
        res = pricer_forward(SPOT, 180, i_usd=-0.005, i_cop=0.12)
        assert res.precio > SPOT
        assert np.isfinite(res.precio)

    @pytest.mark.parametrize(
        "spot, dias, patron",
        [(0.0, 90, "positivo"), (-100.0, 90, "positivo"), (SPOT, -1, "no negativo")],
    )
    def test_valida_los_insumos(self, spot, dias, patron) -> None:
        with pytest.raises(ValueError, match=patron):
            pricer_forward(spot, dias, i_usd=SOFR, i_cop=0.12)

    def test_exige_curva_o_tasa_cop_explicita(self) -> None:
        with pytest.raises(ValueError, match="params_curva_cop o bien i_cop"):
            pricer_forward(SPOT, 90, i_usd=SOFR)


# ---------------------------------------------------------------------------
# Griegas
# ---------------------------------------------------------------------------


class TestSensibilidades:
    def test_delta_analitico_coincide_con_el_numerico(self) -> None:
        res = pricer_forward(SPOT, 180, i_usd=SOFR, params_curva_cop=CURVA_COP)
        assert res.sensibilidades["delta_spot"] == pytest.approx(
            res.sensibilidades["delta_spot_numerico"], rel=1e-8
        )

    def test_delta_es_cercano_a_uno_pero_no_exactamente_uno(self) -> None:
        """El delta es K_cop/K_usd: ~1 porque el diferencial es chico, no por definición."""
        res = pricer_forward(SPOT, 180, i_usd=SOFR, params_curva_cop=CURVA_COP)
        delta = res.sensibilidades["delta_spot"]
        assert 1.0 < delta < 1.10
        assert delta == pytest.approx(res.factor_cop / res.factor_usd)
        assert delta == pytest.approx(res.precio / res.spot)

    def test_dv01_cop_reproduce_un_shock_real_de_un_punto_basico(self) -> None:
        base = pricer_forward(SPOT, 180, i_usd=SOFR, i_cop=0.12)
        shockeado = pricer_forward(SPOT, 180, i_usd=SOFR, i_cop=0.12 + UN_BP)
        assert shockeado.precio - base.precio == pytest.approx(
            base.sensibilidades["dv01_cop"], rel=1e-4
        )

    def test_dv01_usd_reproduce_un_shock_real_de_un_punto_basico(self) -> None:
        base = pricer_forward(SPOT, 180, i_usd=SOFR, i_cop=0.12)
        shockeado = pricer_forward(SPOT, 180, i_usd=SOFR + UN_BP, i_cop=0.12)
        assert shockeado.precio - base.precio == pytest.approx(
            base.sensibilidades["dv01_usd"], rel=1e-4
        )

    def test_los_dv01_tienen_signos_opuestos(self) -> None:
        """Subir la tasa local encarece el forward; subir la externa lo abarata."""
        res = pricer_forward(SPOT, 180, i_usd=SOFR, i_cop=0.12)
        assert res.sensibilidades["dv01_cop"] > 0
        assert res.sensibilidades["dv01_usd"] < 0

    def test_theta_reproduce_el_paso_de_un_dia(self) -> None:
        base = pricer_forward(SPOT, 180, i_usd=SOFR, i_cop=0.12)
        manana = pricer_forward(SPOT, 181, i_usd=SOFR, i_cop=0.12)
        assert manana.precio - base.precio == pytest.approx(
            base.sensibilidades["theta_dia"], rel=1e-3
        )

    def test_theta_es_positivo_cuando_la_tasa_local_es_mayor(self) -> None:
        res = pricer_forward(SPOT, 180, i_usd=SOFR, i_cop=0.12)
        assert res.sensibilidades["theta_dia"] > 0

    def test_los_dv01_crecen_con_el_plazo(self) -> None:
        corto = pricer_forward(SPOT, 30, i_usd=SOFR, i_cop=0.12)
        largo = pricer_forward(SPOT, 360, i_usd=SOFR, i_cop=0.12)
        assert corto.sensibilidades["dv01_cop"] < largo.sensibilidades["dv01_cop"]
        assert abs(corto.sensibilidades["dv01_usd"]) < abs(
            largo.sensibilidades["dv01_usd"]
        )

    def test_sin_plazo_no_hay_riesgo_de_tasa(self) -> None:
        res = pricer_forward(SPOT, 0, i_usd=SOFR, i_cop=0.12)
        assert res.sensibilidades["dv01_cop"] == 0.0
        assert res.sensibilidades["dv01_usd"] == 0.0
        assert res.sensibilidades["theta_dia"] == 0.0
        assert res.sensibilidades["delta_spot"] == pytest.approx(1.0)

    def test_griegas_en_convencion_simple_tambien_son_correctas(self) -> None:
        base = pricer_forward(
            SPOT, 180, i_usd=SOFR, i_cop=0.12, convencion_cop=ConvencionTasa.SIMPLE_360
        )
        shockeado = pricer_forward(
            SPOT,
            180,
            i_usd=SOFR,
            i_cop=0.12 + UN_BP,
            convencion_cop=ConvencionTasa.SIMPLE_360,
        )
        assert shockeado.precio - base.precio == pytest.approx(
            base.sensibilidades["dv01_cop"], rel=1e-6
        )


# ---------------------------------------------------------------------------
# Convenciones
# ---------------------------------------------------------------------------


class TestConvenciones:
    def test_las_dos_convenciones_coinciden_a_un_dia(self) -> None:
        """A plazo muy corto la capitalización compuesta y la simple casi no difieren."""
        cmp = comparar_convenciones(SPOT, 1, i_usd=SOFR, i_cop=0.12)
        assert abs(cmp["diferencia_bps"]) < 5.0

    def test_la_brecha_crece_en_la_ventana_money_market(self) -> None:
        """Hasta ~6 meses domina el desajuste de base: la brecha crece con el plazo."""
        brechas = [
            comparar_convenciones(SPOT, d, i_usd=SOFR, i_cop=0.12)["diferencia_bps"]
            for d in (30, 60, 90, 120, 180)
        ]
        assert brechas == sorted(brechas)
        assert all(b > 0 for b in brechas)

    def test_la_brecha_es_material_en_los_plazos_liquidos(self) -> None:
        """Mezclar bases sin querer cuesta decenas de puntos básicos, no redondeo."""
        for dias in (90, 180, 360):
            cmp = comparar_convenciones(SPOT, dias, i_usd=SOFR, i_cop=0.12)
            assert cmp["diferencia_bps"] > 10.0
            assert cmp["precio_simple_360"] != cmp["precio_ea_365"]

    def test_la_brecha_cambia_de_signo_en_plazos_largos(self) -> None:
        """Más allá del año la capitalización compuesta se impone al desajuste de base.

        Son dos efectos opuestos: dividir por 360 en vez de 365 infla la pata simple,
        pero capitalizar de forma compuesta acelera la pata efectiva anual. El primero
        manda en el corto plazo; el segundo termina ganando. Por eso la brecha no es
        monótona: sube hasta ~6-9 meses, cruza cero después del año y luego se hace
        fuertemente negativa.
        """
        corto = comparar_convenciones(SPOT, 180, i_usd=SOFR, i_cop=0.12)["diferencia_bps"]
        largo = comparar_convenciones(SPOT, 730, i_usd=SOFR, i_cop=0.12)["diferencia_bps"]
        assert corto > 0 > largo

    def test_la_brecha_no_es_monotona(self) -> None:
        """Fija la no monotonía para que nadie la 'corrija' asumiendo lo contrario."""
        brechas = [
            comparar_convenciones(SPOT, d, i_usd=SOFR, i_cop=0.12)["diferencia_bps"]
            for d in (30, 90, 180, 270, 360, 540)
        ]
        assert brechas != sorted(brechas)
        assert max(brechas) == pytest.approx(max(brechas[:4]))  # el pico está antes del año

    def test_marca_cuando_el_plazo_excede_los_nodos_calibrados(self) -> None:
        """Cotizar más allá del último nodo es extrapolar, y el motor debe decirlo."""
        dentro = pricer_forward(
            SPOT, 90, i_usd=SOFR, params_curva_cop=CURVA_COP, plazo_max_curva_dias=90
        )
        fuera = pricer_forward(
            SPOT, 360, i_usd=SOFR, params_curva_cop=CURVA_COP, plazo_max_curva_dias=90
        )
        assert dentro.extrapolado is False
        assert fuera.extrapolado is True
        assert "EXTRAPOLADO" in fuera.resumen()
        assert "EXTRAPOLADO" not in dentro.resumen()

    def test_sin_informar_el_plazo_maximo_no_se_afirma_nada(self) -> None:
        res = pricer_forward(SPOT, 360, i_usd=SOFR, params_curva_cop=CURVA_COP)
        assert res.extrapolado is None
        assert "EXTRAPOLADO" not in res.resumen()

    def test_tasa_cop_del_plazo_usa_base_365(self) -> None:
        assert tasa_cop_del_plazo(365, CURVA_COP) == pytest.approx(
            float(tasa_cero_cupon(1.0, CURVA_COP))
        )
