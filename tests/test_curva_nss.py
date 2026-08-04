"""Tests de la curva Nelson-Siegel-Svensson.

La prueba central es la **recuperación de parámetros sintéticos**: se genera una curva
con parámetros conocidos, se calibra sobre esos puntos y se verifica que el ajuste
reproduce la curva original. Es la única forma honesta de validar la calibración, dado
que los nodos publicados por el Banco de la República ya vienen suavizados con
Nelson-Siegel y un RMSE bajo contra ellos no probaría gran cosa.
"""

from __future__ import annotations

import numpy as np
import pytest

from motor_tes.config import RMSE_MAXIMO_BPS
from motor_tes.curva_nss import (
    UN_BP,
    DiagnosticoCurva,
    FlujoCaja,
    NSSParams,
    calibrar_nss,
    chequeo_no_arbitraje,
    duracion_macaulay,
    duracion_modificada,
    dv01,
    factor_descuento,
    flujos_bono,
    tasa_cero_cupon,
    tasa_forward,
    tasa_forward_instantanea,
    valor_presente,
)

#: Curva de referencia con forma realista para el mercado COP: nivel ~11%, pendiente
#: negativa de corto plazo y dos jorobas de curvatura.
CURVA_VERDADERA = NSSParams(
    beta0=0.11, beta1=-0.02, beta2=0.03, beta3=-0.015, lambda1=1.5, lambda2=6.0
)

#: Nodos típicos de una curva soberana publicada.
PLAZOS_NODOS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0])


@pytest.fixture
def tasas_verdaderas() -> np.ndarray:
    """Tasas que genera :data:`CURVA_VERDADERA` en los nodos de referencia."""
    return np.asarray(tasa_cero_cupon(PLAZOS_NODOS, CURVA_VERDADERA))


# ---------------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------------


class TestNSSParams:
    def test_rechaza_lambda_no_positivo(self) -> None:
        with pytest.raises(ValueError, match="lambda1 y lambda2 deben ser positivos"):
            NSSParams(0.1, 0.0, 0.0, 0.0, 0.0, 1.0)
        with pytest.raises(ValueError, match="lambda1 y lambda2 deben ser positivos"):
            NSSParams(0.1, 0.0, 0.0, 0.0, 1.0, -2.0)

    def test_ida_y_vuelta_por_array(self) -> None:
        assert NSSParams.from_array(CURVA_VERDADERA.to_array()) == CURVA_VERDADERA

    def test_tasa_instantanea_es_beta0_mas_beta1(self) -> None:
        assert CURVA_VERDADERA.tasa_instantanea == pytest.approx(0.09)

    def test_permutar_los_pares_beta_lambda_cambia_la_curva(self) -> None:
        """No hay simetría de permutación: beta1 también se apoya en lambda1.

        Es tentador suponer que intercambiar (beta2, lambda1) con (beta3, lambda2) deja
        la curva intacta, pero el término de pendiente comparte lambda1, así que el
        intercambio la mueve. Este test fija esa propiedad para que nadie reintroduzca
        una "canonicalización" por permutación creyéndola inocua.
        """
        permutado = NSSParams(0.11, -0.02, -0.015, 0.03, 6.0, 1.5)
        assert not CURVA_VERDADERA.curva_equivalente_a(permutado)

    def test_curva_equivalente_a_reconoce_la_misma_curva(self) -> None:
        assert CURVA_VERDADERA.curva_equivalente_a(CURVA_VERDADERA)
        casi_igual = NSSParams(0.11 + 1e-9, -0.02, 0.03, -0.015, 1.5, 6.0)
        assert CURVA_VERDADERA.curva_equivalente_a(casi_igual)


# ---------------------------------------------------------------------------
# Funciones de curva
# ---------------------------------------------------------------------------


class TestFuncionesCurva:
    def test_limite_en_cero_es_beta0_mas_beta1(self) -> None:
        """En t=0 los factores de carga son 0/0; la expansión de Taylor lo resuelve."""
        assert tasa_cero_cupon(0.0, CURVA_VERDADERA) == pytest.approx(
            CURVA_VERDADERA.tasa_instantanea, abs=1e-12
        )

    def test_continuidad_al_acercarse_a_cero(self) -> None:
        plazos = np.array([1e-12, 1e-9, 1e-7, 1e-5, 1e-3])
        tasas = np.asarray(tasa_cero_cupon(plazos, CURVA_VERDADERA))
        assert np.all(np.isfinite(tasas))
        assert tasas[0] == pytest.approx(CURVA_VERDADERA.tasa_instantanea, abs=1e-9)

    def test_curva_plana_devuelve_beta0_en_todo_plazo(self) -> None:
        plana = NSSParams(0.10, 0.0, 0.0, 0.0, 1.0, 1.0)
        plazos = np.array([0.1, 1.0, 5.0, 30.0])
        np.testing.assert_allclose(
            np.asarray(tasa_cero_cupon(plazos, plana)), 0.10, atol=1e-15
        )

    def test_rechaza_plazos_negativos(self) -> None:
        with pytest.raises(ValueError, match="no negativos"):
            tasa_cero_cupon(-1.0, CURVA_VERDADERA)

    def test_escalar_devuelve_escalar_y_vector_devuelve_vector(self) -> None:
        assert isinstance(tasa_cero_cupon(3.0, CURVA_VERDADERA), float)
        assert np.asarray(tasa_cero_cupon([1.0, 2.0], CURVA_VERDADERA)).shape == (2,)

    def test_factor_descuento_en_cero_es_uno(self) -> None:
        assert factor_descuento(0.0, CURVA_VERDADERA) == pytest.approx(1.0, abs=1e-15)

    def test_factor_descuento_es_consistente_con_la_tasa(self) -> None:
        """DF(t) debe ser exactamente (1 + z(t))**-t, sin atajos."""
        for t in (0.5, 2.0, 7.0, 20.0):
            z = float(tasa_cero_cupon(t, CURVA_VERDADERA))
            assert float(factor_descuento(t, CURVA_VERDADERA)) == pytest.approx(
                (1.0 + z) ** (-t), rel=1e-14
            )

    def test_tasas_negativas_no_rompen_el_descuento(self) -> None:
        """Curva con tasas negativas: el motor debe seguir descontando (DF > 1)."""
        negativa = NSSParams(-0.005, -0.010, 0.0, 0.0, 2.0, 5.0)
        assert float(tasa_cero_cupon(3.0, negativa)) < 0.0
        assert float(factor_descuento(3.0, negativa)) > 1.0

    def test_descuento_indefinido_si_el_factor_es_no_positivo(self) -> None:
        patologica = NSSParams(-1.5, 0.0, 0.0, 0.0, 2.0, 5.0)
        with pytest.raises(ValueError, match=r"1 \+ z\(t\) <= 0"):
            factor_descuento(3.0, patologica)


class TestForwards:
    def test_forward_desde_cero_es_la_spot(self) -> None:
        for t in (0.5, 3.0, 10.0):
            assert tasa_forward(0.0, t, CURVA_VERDADERA) == pytest.approx(
                float(tasa_cero_cupon(t, CURVA_VERDADERA)), rel=1e-12
            )

    def test_forward_de_plazo_nulo_es_la_instantanea(self) -> None:
        assert tasa_forward(5.0, 5.0, CURVA_VERDADERA) == pytest.approx(
            tasa_forward_instantanea(5.0, CURVA_VERDADERA), rel=1e-6
        )

    def test_composicion_de_forwards_no_deja_arbitraje(self) -> None:
        """Capitalizar 0->3 y 3->7 debe dar lo mismo que 0->7."""
        f_03 = tasa_forward(0.0, 3.0, CURVA_VERDADERA)
        f_37 = tasa_forward(3.0, 7.0, CURVA_VERDADERA)
        f_07 = tasa_forward(0.0, 7.0, CURVA_VERDADERA)
        assert (1 + f_03) ** 3 * (1 + f_37) ** 4 == pytest.approx(
            (1 + f_07) ** 7, rel=1e-12
        )

    def test_rechaza_intervalo_invertido(self) -> None:
        with pytest.raises(ValueError, match="mayor o igual"):
            tasa_forward(5.0, 2.0, CURVA_VERDADERA)


# ---------------------------------------------------------------------------
# Calibración
# ---------------------------------------------------------------------------


class TestCalibracion:
    def test_recupera_una_curva_sintetica_conocida(self, tasas_verdaderas) -> None:
        """Prueba central: calibrar sobre puntos generados con parámetros conocidos."""
        res = calibrar_nss(PLAZOS_NODOS, tasas_verdaderas)

        assert res.exito
        assert res.rmse_bps < RMSE_MAXIMO_BPS

        # Se compara la CURVA, no el vector de parámetros: los pares (b2,L1) y (b3,L2)
        # son intercambiables, así que dos vectores distintos pueden ser la misma curva
        # y comparar parámetros crudos daría falsos negativos.
        grilla = np.linspace(0.01, 20.0, 500)
        desvio_bps = (
            np.max(
                np.abs(
                    np.asarray(tasa_cero_cupon(grilla, res.params))
                    - np.asarray(tasa_cero_cupon(grilla, CURVA_VERDADERA))
                )
            )
            / UN_BP
        )
        assert desvio_bps < RMSE_MAXIMO_BPS

    def test_residuales_consistentes_con_el_rmse(self, tasas_verdaderas) -> None:
        res = calibrar_nss(PLAZOS_NODOS, tasas_verdaderas)
        assert res.rmse_bps == pytest.approx(
            float(np.sqrt(np.mean(res.residuales_bps**2))), rel=1e-12
        )
        np.testing.assert_allclose(
            res.residuales_bps,
            (res.tasas_ajustadas - res.tasas_mercado) / UN_BP,
            atol=1e-9,
        )

    def test_multistart_prueba_varios_arranques(self, tasas_verdaderas) -> None:
        """El multi-start existe porque la superficie tiene mínimos locales en lambda."""
        res = calibrar_nss(PLAZOS_NODOS, tasas_verdaderas)
        assert res.n_arranques > 1

    def test_nelson_siegel_de_cuatro_parametros(self) -> None:
        ns_verdadero = NSSParams(0.11, -0.02, 0.03, 0.0, 2.0, 2.0)
        tasas = np.asarray(tasa_cero_cupon(PLAZOS_NODOS, ns_verdadero))
        res = calibrar_nss(PLAZOS_NODOS, tasas, svensson=False)

        assert not res.svensson
        assert res.params.beta3 == 0.0
        assert res.rmse_bps < RMSE_MAXIMO_BPS

    def test_rechaza_sistema_subdeterminado(self) -> None:
        """Menos nodos que parámetros: el ajuste sería exacto pero arbitrario."""
        plazos = np.array([0.25, 1.0, 3.0])
        tasas = np.asarray(tasa_cero_cupon(plazos, CURVA_VERDADERA))
        with pytest.raises(ValueError, match="subdeterminado"):
            calibrar_nss(plazos, tasas, svensson=False)

        # Con la excepción explícita sí procede.
        res = calibrar_nss(plazos, tasas, svensson=False, permitir_subdeterminado=True)
        assert res.rmse_bps < RMSE_MAXIMO_BPS

    def test_los_pesos_privilegian_los_nodos_ponderados(self) -> None:
        """Un nodo con peso alto debe quedar mejor ajustado que sin ponderar."""
        tasas = np.asarray(tasa_cero_cupon(PLAZOS_NODOS, CURVA_VERDADERA)).copy()
        tasas[0] += 0.004  # ruido de 40 bps en el nodo corto

        sin_peso = calibrar_nss(PLAZOS_NODOS, tasas)
        pesos = np.ones_like(PLAZOS_NODOS)
        pesos[0] = 1000.0
        con_peso = calibrar_nss(PLAZOS_NODOS, tasas, pesos=pesos)

        assert abs(con_peso.residuales_bps[0]) < abs(sin_peso.residuales_bps[0])

    @pytest.mark.parametrize(
        "plazos, tasas, patron",
        [
            ([1.0], [0.10], "al menos 2 nodos"),
            ([1.0, 2.0, 3.0], [0.10, 0.11], "igual largo"),
            ([0.0, 1.0, 2.0, 3.0, 4.0, 5.0], [0.1] * 6, "estrictamente positivos"),
            ([1.0, 1.0, 2.0, 3.0, 4.0, 5.0], [0.1] * 6, "plazos repetidos"),
        ],
    )
    def test_valida_los_insumos(self, plazos, tasas, patron) -> None:
        with pytest.raises(ValueError, match=patron):
            calibrar_nss(plazos, tasas)

    def test_calibra_una_curva_con_tasas_negativas(self) -> None:
        negativa = NSSParams(0.002, -0.012, 0.005, 0.0, 2.0, 2.0)
        tasas = np.asarray(tasa_cero_cupon(PLAZOS_NODOS, negativa))
        assert np.any(tasas < 0)
        res = calibrar_nss(PLAZOS_NODOS, tasas, svensson=False)
        assert res.rmse_bps < RMSE_MAXIMO_BPS


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------


class TestNoArbitraje:
    def test_curva_sana_pasa_el_chequeo(self) -> None:
        diag = chequeo_no_arbitraje(CURVA_VERDADERA, t_max=15.0)
        assert isinstance(diag, DiagnosticoCurva)
        assert diag.sin_arbitraje
        assert diag.descuentos_monotonos
        assert diag.forwards_negativas.size == 0

    def test_detecta_forwards_negativas(self) -> None:
        """Curvatura muy negativa: hunde la curva y produce forwards implícitas < 0.

        Una curva meramente decreciente NO alcanza: la forward instantánea tiende a
        ``beta0``, así que con ``beta0 >= 0`` nunca cruza a negativo. Hace falta una
        joroba de curvatura fuerte.
        """
        patologica = NSSParams(0.02, 0.05, -0.30, 0.0, 0.8, 5.0)
        diag = chequeo_no_arbitraje(patologica, t_max=10.0)
        assert not diag.sin_arbitraje
        assert diag.forwards_negativas.size > 0
        assert not diag.descuentos_monotonos

    def test_curva_decreciente_con_beta0_positivo_no_es_patologica(self) -> None:
        """Contraejemplo que documenta el límite del diagnóstico anterior."""
        decreciente = NSSParams(0.01, 0.25, 0.0, 0.0, 0.5, 5.0)
        assert chequeo_no_arbitraje(decreciente, t_max=10.0).sin_arbitraje


# ---------------------------------------------------------------------------
# Riesgo de bonos
# ---------------------------------------------------------------------------


class TestRiesgoBono:
    def test_estructura_de_flujos(self) -> None:
        flujos = flujos_bono(cupon=0.10, plazo_anios=3.0, frecuencia=1, nominal=100.0)
        assert [f.t for f in flujos] == pytest.approx([1.0, 2.0, 3.0])
        assert [f.monto for f in flujos] == pytest.approx([10.0, 10.0, 110.0])

    def test_flujos_semestrales(self) -> None:
        flujos = flujos_bono(cupon=0.08, plazo_anios=2.0, frecuencia=2)
        assert len(flujos) == 4
        assert flujos[-1].monto == pytest.approx(104.0)

    @pytest.mark.parametrize("plazo, frecuencia", [(0.0, 1), (-1.0, 1), (5.0, 0)])
    def test_rechaza_definiciones_invalidas(self, plazo, frecuencia) -> None:
        with pytest.raises(ValueError):
            flujos_bono(cupon=0.10, plazo_anios=plazo, frecuencia=frecuencia)

    def test_bono_a_la_par_cuando_el_cupon_iguala_la_curva_plana(self) -> None:
        """Con curva plana al 10%, un bono con cupón 10% debe valer el nominal."""
        plana = NSSParams(0.10, 0.0, 0.0, 0.0, 1.0, 1.0)
        flujos = flujos_bono(cupon=0.10, plazo_anios=5.0)
        assert valor_presente(flujos, plana) == pytest.approx(100.0, rel=1e-12)

    def test_bono_cupon_cero_replica_el_factor_de_descuento(self) -> None:
        cero = [FlujoCaja(t=7.0, monto=100.0)]
        esperado = 100.0 * float(factor_descuento(7.0, CURVA_VERDADERA))
        assert valor_presente(cero, CURVA_VERDADERA) == pytest.approx(esperado, rel=1e-14)

    def test_duracion_de_un_cupon_cero_es_su_plazo(self) -> None:
        cero = [FlujoCaja(t=7.0, monto=100.0)]
        assert duracion_macaulay(cero, CURVA_VERDADERA) == pytest.approx(7.0, rel=1e-12)

    def test_duracion_modificada_es_menor_que_macaulay(self) -> None:
        flujos = flujos_bono(cupon=0.1325, plazo_anios=7.0)
        d_mod = duracion_modificada(flujos, CURVA_VERDADERA)
        assert 0 < d_mod < duracion_macaulay(flujos, CURVA_VERDADERA)

    def test_dv01_coincide_con_la_definicion_de_duracion_modificada(self) -> None:
        flujos = flujos_bono(cupon=0.1325, plazo_anios=7.0)
        vp = valor_presente(flujos, CURVA_VERDADERA)
        assert duracion_modificada(flujos, CURVA_VERDADERA) == pytest.approx(
            dv01(flujos, CURVA_VERDADERA) / (vp * UN_BP), rel=1e-12
        )

    def test_dv01_es_positivo_y_crece_con_el_plazo(self) -> None:
        corto = flujos_bono(cupon=0.10, plazo_anios=2.0)
        largo = flujos_bono(cupon=0.10, plazo_anios=10.0)
        assert 0 < dv01(corto, CURVA_VERDADERA) < dv01(largo, CURVA_VERDADERA)

    def test_dv01_aproxima_el_cambio_de_valor_ante_un_shock_real(self) -> None:
        """El DV01 debe predecir el cambio real de valor ante un shock de 1 bp."""
        flujos = flujos_bono(cupon=0.1325, plazo_anios=7.0)
        vp_base = valor_presente(flujos, CURVA_VERDADERA)
        vp_shock = valor_presente(flujos, CURVA_VERDADERA, shift=UN_BP)
        assert (vp_base - vp_shock) == pytest.approx(
            dv01(flujos, CURVA_VERDADERA), rel=1e-3
        )

    def test_el_error_lineal_es_convexidad_y_se_achica_con_el_shock(self) -> None:
        """El DV01 es una aproximación de primer orden: el residuo es convexidad.

        Si el desvío relativo crece proporcionalmente al tamaño del shock, el residuo
        es de segundo orden (convexidad) y no un error de cálculo.
        """
        flujos = flujos_bono(cupon=0.1325, plazo_anios=7.0)
        vp_base = valor_presente(flujos, CURVA_VERDADERA)
        sensibilidad = dv01(flujos, CURVA_VERDADERA)

        desvios = []
        for n_bps in (1, 5, 10):
            real = vp_base - valor_presente(flujos, CURVA_VERDADERA, shift=n_bps * UN_BP)
            lineal = n_bps * sensibilidad
            desvios.append(abs(real - lineal) / lineal)

        assert desvios[0] < desvios[1] < desvios[2]  # crece con el tamaño del shock
        assert desvios[-1] < 0.01  # y sigue siendo chico: 10 bps mueve <1%

    def test_valor_presente_rechaza_flujos_vacios(self) -> None:
        with pytest.raises(ValueError, match="No hay flujos"):
            valor_presente([], CURVA_VERDADERA)
