"""Curva cero cupón paramétrica Nelson-Siegel-Svensson y métricas de riesgo.

Modelo
------
La tasa cero cupón a plazo ``t`` (años, ACT/365) es::

    z(t) = b0
         + b1 * f1(t/L1)
         + b2 * f2(t/L1)
         + b3 * f2(t/L2)

con los factores de carga::

    f1(x) = (1 - exp(-x)) / x          (nivel -> pendiente)
    f2(x) = f1(x) - exp(-x)            (curvatura)

Interpretación de mesa: ``b0`` es el nivel de largo plazo, ``b1`` la pendiente
(``z(0) = b0 + b1`` es la tasa instantánea), ``b2`` y ``b3`` dos jorobas de curvatura
ubicadas por ``L1`` y ``L2``. Svensson (1994) agrega el segundo término de curvatura
sobre el Nelson-Siegel (1987) original para capturar curvas con doble joroba.

Con ``svensson=False`` el modelo colapsa a Nelson-Siegel de 4 parámetros
(``b3 = 0``), que es la metodología que el propio Banco de la República usa para
publicar su curva cero cupón de TES.

Convenciones
------------
* Plazos en años bajo **ACT/365**.
* Tasas **efectivas anuales** en decimal (0.1210 = 12.10%).
* Descuento: ``DF(t) = (1 + z(t)) ** -t``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final, Iterable, Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.optimize import OptimizeResult, least_squares

from motor_tes.config import UMBRAL_TAYLOR

__all__ = [
    "NSSParams",
    "ResultadoCalibracion",
    "DiagnosticoCurva",
    "FlujoCaja",
    "tasa_cero_cupon",
    "factor_descuento",
    "tasa_forward",
    "tasa_forward_instantanea",
    "calibrar_nss",
    "chequeo_no_arbitraje",
    "flujos_bono",
    "valor_presente",
    "duracion_macaulay",
    "duracion_modificada",
    "dv01",
]

#: Un punto básico, en decimal.
UN_BP: Final[float] = 1e-4


# ---------------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NSSParams:
    """Los seis parámetros de Nelson-Siegel-Svensson.

    Attributes:
        beta0: Nivel asintótico de largo plazo, en decimal.
        beta1: Componente de pendiente. ``beta0 + beta1`` es la tasa instantánea.
        beta2: Primera curvatura, ubicada por ``lambda1``.
        beta3: Segunda curvatura, ubicada por ``lambda2``. Cero en Nelson-Siegel puro.
        lambda1: Escala temporal de la pendiente y la primera curvatura, en años.
        lambda2: Escala temporal de la segunda curvatura, en años.
    """

    beta0: float
    beta1: float
    beta2: float
    beta3: float
    lambda1: float
    lambda2: float

    def __post_init__(self) -> None:
        if self.lambda1 <= 0 or self.lambda2 <= 0:
            raise ValueError(
                f"lambda1 y lambda2 deben ser positivos; recibí "
                f"lambda1={self.lambda1}, lambda2={self.lambda2}"
            )

    @property
    def es_svensson(self) -> bool:
        """``True`` si el término de segunda curvatura está activo."""
        return self.beta3 != 0.0

    @property
    def tasa_instantanea(self) -> float:
        """Tasa cero cupón en el límite ``t -> 0``, igual a ``beta0 + beta1``."""
        return self.beta0 + self.beta1

    def to_array(self) -> NDArray[np.float64]:
        """Devuelve los parámetros como vector ``[b0, b1, b2, b3, L1, L2]``."""
        return np.array(
            [self.beta0, self.beta1, self.beta2, self.beta3, self.lambda1, self.lambda2],
            dtype=float,
        )

    @classmethod
    def from_array(cls, vector: ArrayLike) -> NSSParams:
        """Reconstruye desde el vector ``[b0, b1, b2, b3, L1, L2]``."""
        b0, b1, b2, b3, l1, l2 = (float(v) for v in np.asarray(vector, dtype=float))
        return cls(b0, b1, b2, b3, l1, l2)

    def curva_equivalente_a(
        self, otro: NSSParams, t_max: float = 20.0, tol_bps: float = 1.0
    ) -> bool:
        """Indica si dos juegos de parámetros describen prácticamente la misma curva.

        NSS está **débilmente identificado**: la superficie objetivo tiene direcciones
        casi planas en ``lambda1`` y ``lambda2``, de modo que vectores de parámetros
        visiblemente distintos pueden generar curvas indistinguibles a nivel de mercado.

        Nótese que no existe una simetría exacta de permutación entre ``(beta2, lambda1)``
        y ``(beta3, lambda2)``: ``beta1`` también se apoya en ``lambda1``, así que
        intercambiar los pares cambia el término de pendiente y por tanto la curva. Por
        eso la comparación correcta entre dos calibraciones es sobre la **curva**, no
        sobre el vector de parámetros.

        Args:
            otro: Parámetros a comparar.
            t_max: Plazo máximo de la grilla de comparación, en años.
            tol_bps: Desvío máximo tolerado en cualquier punto de la grilla, en bps.

        Returns:
            ``True`` si el desvío máximo entre ambas curvas es menor que ``tol_bps``.
        """
        grilla = np.linspace(0.0, t_max, 500)
        desvio = np.max(
            np.abs(
                np.asarray(tasa_cero_cupon(grilla, self))
                - np.asarray(tasa_cero_cupon(grilla, otro))
            )
        )
        return bool(desvio / UN_BP < tol_bps)


# ---------------------------------------------------------------------------
# Factores de carga y funciones de curva
# ---------------------------------------------------------------------------


def _factores_carga(t: NDArray[np.float64], lam: float) -> tuple[NDArray, NDArray]:
    """Calcula ``f1(t/lam)`` y ``f2(t/lam)`` con manejo estable de ``t -> 0``.

    En ``t = 0`` ambos factores son 0/0. Por debajo de
    :data:`~motor_tes.config.UMBRAL_TAYLOR` se usa la expansión::

        f1(x) = 1 - x/2 + x**2/6
        f2(x) =     x/2 - x**2/3

    de modo que ``f1(0) = 1`` y ``f2(0) = 0``, y por tanto ``z(0) = b0 + b1``.

    Args:
        t: Plazos en años, no negativos.
        lam: Escala temporal, estrictamente positiva.

    Returns:
        Tupla ``(f1, f2)`` con la misma forma que ``t``.
    """
    x = t / lam
    pequeno = x < UMBRAL_TAYLOR

    x_seguro = np.where(pequeno, 1.0, x)  # evita dividir por cero en la rama exacta
    exp_neg = np.exp(-x_seguro)
    f1_exacto = (1.0 - exp_neg) / x_seguro
    f2_exacto = f1_exacto - exp_neg

    f1_taylor = 1.0 - x / 2.0 + x**2 / 6.0
    f2_taylor = x / 2.0 - x**2 / 3.0

    return np.where(pequeno, f1_taylor, f1_exacto), np.where(pequeno, f2_taylor, f2_exacto)


def tasa_cero_cupon(t: ArrayLike, params: NSSParams) -> NDArray[np.float64] | float:
    """Tasa cero cupón efectiva anual para el plazo ``t``.

    Args:
        t: Plazo o plazos en años (ACT/365). Debe ser no negativo.
        params: Parámetros calibrados de la curva.

    Returns:
        Tasa en decimal. Escalar si ``t`` es escalar, ``ndarray`` si es vector.

    Raises:
        ValueError: Si algún plazo es negativo.
    """
    t_arr = np.asarray(t, dtype=float)
    if np.any(t_arr < 0):
        raise ValueError("Los plazos deben ser no negativos.")

    f1_l1, f2_l1 = _factores_carga(t_arr, params.lambda1)
    _, f2_l2 = _factores_carga(t_arr, params.lambda2)

    z = params.beta0 + params.beta1 * f1_l1 + params.beta2 * f2_l1 + params.beta3 * f2_l2
    return float(z) if t_arr.ndim == 0 else z


def factor_descuento(t: ArrayLike, params: NSSParams) -> NDArray[np.float64] | float:
    """Factor de descuento ``DF(t) = (1 + z(t)) ** -t``, con ``t`` en años ACT/365.

    Args:
        t: Plazo o plazos en años.
        params: Parámetros calibrados.

    Returns:
        Factor de descuento. ``DF(0) = 1`` por construcción.

    Raises:
        ValueError: Si la tasa implica un factor de capitalización no positivo
            (``1 + z(t) <= 0``), caso en que el descuento no está definido.
    """
    t_arr = np.asarray(t, dtype=float)
    z = np.asarray(tasa_cero_cupon(t_arr, params), dtype=float)
    factor = 1.0 + z
    if np.any(factor <= 0.0):
        raise ValueError(
            "1 + z(t) <= 0 para algún plazo: el descuento con capitalización efectiva "
            "anual no está definido. Revisá la calibración."
        )
    df = factor ** (-t_arr)
    return float(df) if t_arr.ndim == 0 else df


def tasa_forward(t1: float, t2: float, params: NSSParams) -> float:
    """Tasa forward efectiva anual implícita entre ``t1`` y ``t2``.

    Se obtiene por no arbitraje entre los factores de descuento::

        (1 + f) ** (t2 - t1) = DF(t1) / DF(t2)

    Args:
        t1: Inicio del período forward, en años. No negativo.
        t2: Fin del período forward, en años. Debe cumplir ``t2 >= t1``.
        params: Parámetros calibrados.

    Returns:
        Tasa forward en decimal. Si ``t2 == t1`` devuelve la forward instantánea
        en ``t1``, que es el límite de la expresión anterior.

    Raises:
        ValueError: Si ``t2 < t1`` o si algún plazo es negativo.
    """
    if t1 < 0 or t2 < 0:
        raise ValueError("Los plazos deben ser no negativos.")
    if t2 < t1:
        raise ValueError(f"t2 ({t2}) debe ser mayor o igual que t1 ({t1}).")
    if np.isclose(t2, t1):
        return tasa_forward_instantanea(t1, params)

    df1 = float(factor_descuento(t1, params))
    df2 = float(factor_descuento(t2, params))
    return (df1 / df2) ** (1.0 / (t2 - t1)) - 1.0


def tasa_forward_instantanea(t: float, params: NSSParams, h: float = 1e-6) -> float:
    """Tasa forward instantánea en ``t``, por diferencias centradas sobre ``ln DF``.

    Sirve de diagnóstico de no arbitraje: una curva bien comportada no debería
    producir forwards instantáneas negativas de forma sostenida.

    Args:
        t: Plazo en años, no negativo.
        params: Parámetros calibrados.
        h: Paso de la diferencia finita, en años.

    Returns:
        Tasa forward instantánea expresada como efectiva anual equivalente.
    """
    t = max(float(t), 0.0)
    t_izq, t_der = max(t - h, 0.0), t + h
    ln_df_izq = np.log(float(factor_descuento(t_izq, params)))
    ln_df_der = np.log(float(factor_descuento(t_der, params)))
    f_continua = -(ln_df_der - ln_df_izq) / (t_der - t_izq)
    return float(np.expm1(f_continua))  # de capitalización continua a efectiva anual


# ---------------------------------------------------------------------------
# Calibración
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoCalibracion:
    """Salida de :func:`calibrar_nss`, con todo lo necesario para auditar el ajuste.

    Attributes:
        params: Parámetros calibrados. Ojo: NSS está débilmente identificado en las
            escalas temporales, así que para comparar dos calibraciones hay que usar
            :meth:`NSSParams.curva_equivalente_a`, no el vector de parámetros.
        rmse_bps: Raíz del error cuadrático medio contra los nodos, en puntos básicos.
        residuales_bps: Residual por nodo (ajustado menos mercado), en puntos básicos.
        plazos: Plazos de los nodos usados, en años.
        tasas_mercado: Tasas observadas en cada nodo, en decimal.
        tasas_ajustadas: Tasas que produce la curva calibrada en cada nodo.
        exito: ``True`` si el optimizador reportó convergencia.
        n_evaluaciones: Evaluaciones de la función objetivo del mejor arranque.
        n_arranques: Cuántos puntos de arranque se probaron en el multi-start.
        svensson: ``True`` si se calibraron 6 parámetros; ``False`` si 4 (Nelson-Siegel).
    """

    params: NSSParams
    rmse_bps: float
    residuales_bps: NDArray[np.float64]
    plazos: NDArray[np.float64]
    tasas_mercado: NDArray[np.float64]
    tasas_ajustadas: NDArray[np.float64]
    exito: bool
    n_evaluaciones: int
    n_arranques: int
    svensson: bool

    @property
    def n_parametros(self) -> int:
        """Parámetros libres del modelo: 6 en Svensson, 4 en Nelson-Siegel."""
        return 6 if self.svensson else 4

    @property
    def grados_libertad(self) -> int:
        """Nodos menos parámetros libres.

        Cero o menos significa que el modelo **interpola** en vez de ajustar: el RMSE
        va a dar prácticamente nulo por construcción, así que deja de ser evidencia de
        calidad. Con los 6 nodos que hoy produce el motor (3 de IBR y 3 de TES),
        Svensson queda con 0 grados de libertad y Nelson-Siegel con 2.
        """
        return len(self.plazos) - self.n_parametros

    @property
    def es_interpolacion(self) -> bool:
        """``True`` si no sobran nodos y el ajuste perfecto está garantizado de antemano."""
        return self.grados_libertad <= 0

    def resumen(self) -> str:
        """Línea de resumen legible, para logs y para el reporte de validación."""
        modelo = "NSS (6p)" if self.svensson else "NS (4p)"
        aviso = (
            "  [INTERPOLA: 0 grados de libertad, RMSE no informativo]"
            if self.es_interpolacion
            else ""
        )
        return (
            f"{modelo} | RMSE={self.rmse_bps:.3f} bps | "
            f"max|residual|={np.max(np.abs(self.residuales_bps)):.3f} bps | "
            f"nodos={len(self.plazos)} | gl={self.grados_libertad} | "
            f"convergió={self.exito}{aviso}"
        )


#: Rejilla de arranque para las escalas temporales, en años. Cubre el corto plazo
#: (donde manda el money market) y el largo (donde manda la parte soberana).
_SEMILLAS_LAMBDA: Final[tuple[float, ...]] = (0.5, 1.0, 2.0, 3.5, 5.0, 8.0, 12.0)


def _ajustar_multistart(
    residuales: Callable[[NDArray[np.float64]], NDArray[np.float64]],
    svensson: bool,
    nivel_inicial: float,
    pendiente_inicial: float,
    semillas_lambda: Iterable[float] | None = None,
    n_observaciones: int | None = None,
    nombre_observaciones: str = "observaciones",
) -> tuple[NSSParams, OptimizeResult, int]:
    """Corre el multi-start de NSS sobre una función de residuales cualquiera.

    La superficie objetivo de NSS tiene **mínimos locales en las escalas temporales**:
    arrancar de un único punto es el error clásico de esta calibración y produce
    ajustes que parecen razonables pero no son el óptimo. Por eso se barre una rejilla
    de ``(lambda1, lambda2)`` y se conserva el arranque con menor suma de residuales al
    cuadrado.

    Vive separado de :func:`calibrar_nss` porque el motor calibra de dos maneras: sobre
    tasas publicadas y sobre precios de instrumentos. Las dos comparten cotas, semillas
    y canonicalización, y tenerlas en un solo lugar evita que se separen con el tiempo.

    Args:
        residuales: Función que recibe el vector de parámetros —seis con Svensson,
            cuatro con Nelson-Siegel— y devuelve el vector de residuales a minimizar.
        svensson: ``True`` para calibrar los 6 parámetros; ``False`` fija ``beta3 = 0``.
        nivel_inicial: Semilla de ``beta0``, típicamente la tasa del plazo más largo.
        pendiente_inicial: Semilla de ``beta1``, típicamente la tasa más corta menos el
            nivel.
        semillas_lambda: Rejilla de arranque para las escalas temporales. Por defecto
            :data:`_SEMILLAS_LAMBDA`.
        n_observaciones: Cantidad de observaciones, solo para el mensaje de error.
        nombre_observaciones: Cómo llamarlas en ese mensaje.

    Returns:
        Terna ``(params, ajuste, n_arranques)`` con los parámetros canónicos, el
        resultado crudo de ``least_squares`` del mejor arranque y cuántos se probaron.

    Raises:
        RuntimeError: Si ningún arranque converge.
    """
    # Cotas amplias pero acotadas: las tasas nominales COP pueden ser altas, pero
    # |beta| > 1 (100%) o lambda > 30 años no describen una curva de mercado.
    if svensson:
        lo = np.array([-1.0, -1.0, -1.0, -1.0, 0.05, 0.05])
        hi = np.array([1.0, 1.0, 1.0, 1.0, 30.0, 30.0])
    else:
        lo = np.array([-1.0, -1.0, -1.0, 0.05])
        hi = np.array([1.0, 1.0, 1.0, 30.0])

    semillas = tuple(semillas_lambda) if semillas_lambda is not None else _SEMILLAS_LAMBDA
    arranques: list[NDArray[np.float64]] = []
    for l1 in semillas:
        if svensson:
            for l2 in semillas:
                if l2 <= l1:
                    continue  # canonicalizamos exigiendo lambda1 < lambda2
                arranques.append(
                    np.array([nivel_inicial, pendiente_inicial, 0.0, 0.0, l1, l2])
                )
        else:
            arranques.append(np.array([nivel_inicial, pendiente_inicial, 0.0, l1]))

    mejor = None
    mejor_ssr = np.inf
    for x0 in arranques:
        x0_acotado = np.clip(x0, lo, hi)
        try:
            ajuste = least_squares(
                residuales, x0_acotado, bounds=(lo, hi), method="trf", max_nfev=5000
            )
        except (ValueError, np.linalg.LinAlgError):
            continue
        ssr = float(np.sum(ajuste.fun**2))
        if ssr < mejor_ssr:
            mejor, mejor_ssr = ajuste, ssr

    if mejor is None:
        sobre = (
            f" sobre {n_observaciones} {nombre_observaciones}"
            if n_observaciones is not None
            else ""
        )
        raise RuntimeError(
            f"Ningún arranque del multi-start convergió{sobre}. "
            "Revisá que las tasas estén en decimal y los plazos en años."
        )

    if svensson:
        params = NSSParams.from_array(mejor.x)
    else:
        b0, b1, b2, l1 = mejor.x
        params = NSSParams(float(b0), float(b1), float(b2), 0.0, float(l1), float(l1))

    return params, mejor, len(arranques)


def calibrar_nss(
    plazos: ArrayLike,
    tasas: ArrayLike,
    pesos: ArrayLike | None = None,
    svensson: bool = True,
    semillas_lambda: Iterable[float] | None = None,
    permitir_subdeterminado: bool = False,
) -> ResultadoCalibracion:
    """Calibra la curva por mínimos cuadrados no lineales sobre los nodos observados.

    La superficie objetivo de NSS tiene **mínimos locales en las escalas temporales**:
    arrancar de un único punto es el error clásico de esta calibración y produce
    ajustes que parecen razonables pero no son el óptimo. Por eso se hace multi-start
    sobre una rejilla de ``(lambda1, lambda2)`` y se conserva el arranque con menor
    suma de residuales al cuadrado.

    Args:
        plazos: Plazos de los nodos, en años (ACT/365). Deben ser positivos y distintos.
        tasas: Tasas cero cupón observadas, en decimal.
        pesos: Ponderadores por nodo. Por defecto todos 1. Útil para dar más peso a los
            nodos líquidos.
        svensson: Si es ``True`` calibra los 6 parámetros. Si es ``False`` fija
            ``beta3 = 0`` y calibra Nelson-Siegel de 4 parámetros, que es lo apropiado
            cuando hay pocos nodos.
        semillas_lambda: Rejilla de arranque para las escalas temporales. Por defecto
            :data:`_SEMILLAS_LAMBDA`.
        permitir_subdeterminado: Si es ``False`` (defecto) se rechaza calibrar con menos
            nodos que parámetros libres, porque el ajuste sería exacto pero arbitrario.

    Returns:
        :class:`ResultadoCalibracion` con parámetros canónicos, RMSE y residuales.

    Raises:
        ValueError: Si los insumos son inconsistentes, si hay menos de 2 nodos, o si
            hay menos nodos que parámetros y ``permitir_subdeterminado`` es ``False``.
        RuntimeError: Si ningún arranque del multi-start converge.
    """
    t = np.asarray(plazos, dtype=float).ravel()
    y = np.asarray(tasas, dtype=float).ravel()
    if t.shape != y.shape:
        raise ValueError(f"plazos y tasas deben tener igual largo: {t.shape} vs {y.shape}")
    if t.size < 2:
        raise ValueError("Se necesitan al menos 2 nodos para calibrar.")
    if np.any(t <= 0):
        raise ValueError("Los plazos de los nodos deben ser estrictamente positivos.")
    if np.unique(t).size != t.size:
        raise ValueError(f"Hay plazos repetidos entre los nodos: {t.tolist()}")

    w = np.ones_like(t) if pesos is None else np.asarray(pesos, dtype=float).ravel()
    if w.shape != t.shape:
        raise ValueError(f"pesos debe tener el largo de plazos: {w.shape} vs {t.shape}")
    if np.any(w < 0):
        raise ValueError("Los pesos no pueden ser negativos.")

    n_params = 6 if svensson else 4
    if t.size < n_params and not permitir_subdeterminado:
        raise ValueError(
            f"{t.size} nodos para {n_params} parámetros: el sistema está "
            f"subdeterminado y el ajuste sería exacto pero arbitrario. "
            f"Usá svensson=False (4 parámetros) o pasá permitir_subdeterminado=True "
            f"si aceptás el riesgo de sobreajuste."
        )

    raiz_w = np.sqrt(w)

    def residuales(vector: NDArray[np.float64]) -> NDArray[np.float64]:
        if svensson:
            b0, b1, b2, b3, l1, l2 = vector
        else:
            b0, b1, b2, l1 = vector
            b3, l2 = 0.0, 1.0  # lambda2 inerte cuando beta3 = 0
        candidato = NSSParams(b0, b1, b2, b3, l1, l2)
        return (np.asarray(tasa_cero_cupon(t, candidato)) - y) * raiz_w

    params, mejor, n_arranques = _ajustar_multistart(
        residuales,
        svensson=svensson,
        nivel_inicial=float(y[np.argmax(t)]),
        pendiente_inicial=float(y[np.argmin(t)] - y[np.argmax(t)]),
        semillas_lambda=semillas_lambda,
        n_observaciones=t.size,
        nombre_observaciones="nodos",
    )

    ajustadas = np.asarray(tasa_cero_cupon(t, params), dtype=float)
    residual_bps = (ajustadas - y) / UN_BP
    rmse_bps = float(np.sqrt(np.mean(residual_bps**2)))

    return ResultadoCalibracion(
        params=params,
        rmse_bps=rmse_bps,
        residuales_bps=residual_bps,
        plazos=t,
        tasas_mercado=y,
        tasas_ajustadas=ajustadas,
        exito=bool(mejor.success),
        n_evaluaciones=int(mejor.nfev),
        n_arranques=n_arranques,
        svensson=svensson,
    )


# ---------------------------------------------------------------------------
# Diagnóstico de forma de la curva
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagnosticoCurva:
    """Chequeo de que la curva calibrada sea utilizable para descontar.

    Attributes:
        plazos: Grilla evaluada, en años.
        forwards: Forward instantánea en cada punto de la grilla, en decimal.
        forwards_negativas: Plazos donde la forward instantánea es negativa.
        descuentos_monotonos: ``True`` si ``DF(t)`` es no creciente en toda la grilla,
            condición necesaria para ausencia de arbitraje con tasas no negativas.
        max_curvatura: Máximo valor absoluto de la segunda derivada discreta de ``z``,
            como medida de suavidad.
        sin_arbitraje: ``True`` si no hay forwards negativas y los descuentos son
            monótonos.
    """

    plazos: NDArray[np.float64]
    forwards: NDArray[np.float64]
    forwards_negativas: NDArray[np.float64]
    descuentos_monotonos: bool
    max_curvatura: float
    sin_arbitraje: bool

    def resumen(self) -> str:
        """Línea de resumen legible para el reporte de validación."""
        estado = "OK" if self.sin_arbitraje else "REVISAR"
        return (
            f"No arbitraje: {estado} | forwards negativas={len(self.forwards_negativas)} "
            f"| DF monótono={self.descuentos_monotonos} "
            f"| max|z''|={self.max_curvatura:.4f}"
        )


def chequeo_no_arbitraje(
    params: NSSParams, t_max: float = 15.0, n_puntos: int = 300
) -> DiagnosticoCurva:
    """Evalúa forma y consistencia de la curva sobre una grilla fina.

    Args:
        params: Parámetros calibrados.
        t_max: Plazo máximo de la grilla, en años.
        n_puntos: Número de puntos de la grilla.

    Returns:
        :class:`DiagnosticoCurva` con las banderas y las series evaluadas.
    """
    grilla = np.linspace(1.0 / 365.0, t_max, n_puntos)
    forwards = np.array([tasa_forward_instantanea(float(t), params) for t in grilla])
    negativas = grilla[forwards < 0.0]

    df = np.asarray(factor_descuento(grilla, params), dtype=float)
    monotonos = bool(np.all(np.diff(df) <= 1e-12))

    z = np.asarray(tasa_cero_cupon(grilla, params), dtype=float)
    segunda = np.diff(z, n=2) / np.diff(grilla)[:-1] ** 2
    max_curvatura = float(np.max(np.abs(segunda))) if segunda.size else 0.0

    return DiagnosticoCurva(
        plazos=grilla,
        forwards=forwards,
        forwards_negativas=negativas,
        descuentos_monotonos=monotonos,
        max_curvatura=max_curvatura,
        sin_arbitraje=bool(negativas.size == 0 and monotonos),
    )


# ---------------------------------------------------------------------------
# Bonos: flujos, valor presente, duración y DV01
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlujoCaja:
    """Un pago futuro.

    Attributes:
        t: Plazo hasta el pago, en años (ACT/365).
        monto: Importe del pago, en unidades de nominal.
    """

    t: float
    monto: float


def flujos_bono(
    cupon: float,
    plazo_anios: float,
    frecuencia: int = 1,
    nominal: float = 100.0,
) -> list[FlujoCaja]:
    """Construye los flujos de un bono bullet a tasa fija.

    Los TES tasa fija en pesos pagan cupón **anual**, de ahí el valor por defecto de
    ``frecuencia``. La grilla de fechas se construye hacia atrás desde el vencimiento,
    en años exactos, sin calendario de días hábiles: es una simplificación deliberada
    que el README declara y que el benchmark contra QuantLib cuantifica.

    Args:
        cupon: Tasa cupón anual en decimal (0.1325 para 13.25%).
        plazo_anios: Años hasta el vencimiento.
        frecuencia: Pagos de cupón por año.
        nominal: Valor facial que se amortiza al vencimiento.

    Returns:
        Lista de :class:`FlujoCaja` ordenada por plazo ascendente.

    Raises:
        ValueError: Si el plazo o la frecuencia no son positivos.
    """
    if plazo_anios <= 0:
        raise ValueError(f"plazo_anios debe ser positivo; recibí {plazo_anios}")
    if frecuencia <= 0:
        raise ValueError(f"frecuencia debe ser positiva; recibí {frecuencia}")

    paso = 1.0 / frecuencia
    cupon_periodico = nominal * cupon / frecuencia

    tiempos: list[float] = []
    t = plazo_anios
    while t > 1e-9:
        tiempos.append(t)
        t -= paso
    tiempos.reverse()

    flujos = [FlujoCaja(t=ti, monto=cupon_periodico) for ti in tiempos]
    ultimo = flujos[-1]
    flujos[-1] = FlujoCaja(t=ultimo.t, monto=ultimo.monto + nominal)
    return flujos


def _desplazar(params: NSSParams, shift: float) -> NSSParams:
    """Devuelve la curva desplazada en paralelo ``shift`` (decimal) sobre ``beta0``."""
    return NSSParams(
        beta0=params.beta0 + shift,
        beta1=params.beta1,
        beta2=params.beta2,
        beta3=params.beta3,
        lambda1=params.lambda1,
        lambda2=params.lambda2,
    )


def valor_presente(
    cashflows: Sequence[FlujoCaja], params: NSSParams, shift: float = 0.0
) -> float:
    """Valor presente de una serie de flujos descontados con la curva.

    Args:
        cashflows: Flujos del instrumento.
        params: Parámetros calibrados de la curva.
        shift: Desplazamiento paralelo de la curva, en decimal (1e-4 = 1 bp). Se aplica
            sobre ``beta0``, que desplaza ``z(t)`` de forma uniforme en todo plazo.

    Returns:
        Valor presente en las mismas unidades que los montos.

    Raises:
        ValueError: Si no hay flujos.
    """
    if not cashflows:
        raise ValueError("No hay flujos que descontar.")
    curva = params if shift == 0.0 else _desplazar(params, shift)
    tiempos = np.array([f.t for f in cashflows], dtype=float)
    montos = np.array([f.monto for f in cashflows], dtype=float)
    return float(np.sum(montos * np.asarray(factor_descuento(tiempos, curva))))


def duracion_macaulay(cashflows: Sequence[FlujoCaja], params: NSSParams) -> float:
    """Duración de Macaulay: plazo promedio ponderado por valor presente, en años.

    Args:
        cashflows: Flujos del instrumento.
        params: Parámetros calibrados.

    Returns:
        Duración en años.
    """
    tiempos = np.array([f.t for f in cashflows], dtype=float)
    montos = np.array([f.monto for f in cashflows], dtype=float)
    vp_flujos = montos * np.asarray(factor_descuento(tiempos, params))
    return float(np.sum(tiempos * vp_flujos) / np.sum(vp_flujos))


def dv01(cashflows: Sequence[FlujoCaja], params: NSSParams) -> float:
    """DV01: cambio de valor presente ante un desplazamiento paralelo de 1 punto básico.

    Se calcula por diferencia centrada de +/- medio punto básico, que cancela el error
    de segundo orden y por tanto es más preciso que una diferencia hacia adelante::

        DV01 = VP(-0.5bp) - VP(+0.5bp)

    Se devuelve con signo positivo para una posición larga en el bono: subir tasas
    resta valor.

    Args:
        cashflows: Flujos del instrumento.
        params: Parámetros calibrados.

    Returns:
        Sensibilidad en unidades de valor presente por punto básico.
    """
    medio_bp = UN_BP / 2.0
    return valor_presente(cashflows, params, -medio_bp) - valor_presente(
        cashflows, params, +medio_bp
    )


def duracion_modificada(cashflows: Sequence[FlujoCaja], params: NSSParams) -> float:
    """Duración modificada implícita en el DV01, en años.

    Definición de mesa: sensibilidad relativa del valor presente a un desplazamiento
    paralelo de la curva::

        D_mod = DV01 / (VP * 1bp)

    A diferencia de ``D_mac / (1 + y)``, no requiere reducir la curva a una TIR única,
    de modo que es consistente con descontar cada flujo a su propia tasa cero cupón.

    Args:
        cashflows: Flujos del instrumento.
        params: Parámetros calibrados.

    Returns:
        Duración modificada en años.

    Raises:
        ValueError: Si el valor presente es cero.
    """
    vp = valor_presente(cashflows, params)
    if vp == 0.0:
        raise ValueError("Valor presente nulo: la duración modificada no está definida.")
    return dv01(cashflows, params) / (vp * UN_BP)
