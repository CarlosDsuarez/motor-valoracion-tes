"""Calibración de la curva cero cupón directamente contra precios de mercado.

Diferencia con :func:`motor_tes.curva_nss.calibrar_nss`
-------------------------------------------------------
``calibrar_nss`` ajusta **tasas contra tasas**: recibe nodos de una curva ya publicada
y busca los parámetros que los reproducen. Sirve, pero cuando esos nodos vienen de una
curva que el emisor ya suavizó con Nelson-Siegel, el ejercicio es parcialmente
circular: se está ajustando un modelo a la salida de ese mismo modelo.

Este módulo ajusta **contra precios de instrumentos individuales**. Cada bono y cada
letra se valora descontando sus flujos con la curva candidata, y lo que se minimiza es
la distancia al precio observado en pantalla. No hay suavizado previo de por medio.

Qué se minimiza y por qué
-------------------------
El residual del instrumento ``i`` es el error de precio **dividido por su duración**::

    r_i = (PV_modelo_i - precio_sucio_i) / D_i

No el error de precio crudo. Los errores de precio escalan con la duración: un mismo
desvío de un punto básico en tasa mueve el precio de un bono a 30 años unas quince
veces más que el de uno a 2 años. Minimizar precio a secas le entrega la calibración a
la parte larga de la curva. Medido sobre la muestra de referencia, ponderando por
precio los instrumentos cortos quedan con errores de 130 a 190 bps, contra unos 60 bps
ponderando por duración.

Dividir por la duración deja el residual en unidades de tasa —es la aproximación de
primer orden del error de yield—, así que el RMSE sigue expresándose en puntos básicos
y es directamente comparable con el de la calibración sobre nodos.

Se ajusta al precio medio. La horquilla no entra al objetivo pero sí al diagnóstico:
un residual menor a la media horquilla significa que la curva pasa por dentro del
mercado, o sea que el desvío es indistinguible del costo de transacción.

Segmentación del tramo corto
----------------------------
Los instrumentos por debajo de :data:`~motor_tes.config.PLAZO_MINIMO_CALIBRACION_ANIOS`
se valoran y se reportan, pero **no entran al objetivo**. No es una conveniencia: en el
mercado COP las letras muy cortas cotizan en un segmento de dinero que no se conecta de
forma suave con la curva de bonos, y una NSS no tiene la flexibilidad para atravesar el
quiebre. Forzarla degrada el ajuste en toda la curva, no solo en el tramo corto. El
campo ``ajustado`` de cada :class:`ResiduoInstrumento` deja el corte a la vista.

Fechas heterogéneas
-------------------
Cada instrumento descuenta desde **su propia** fecha de cotización. Cuando la muestra
mezcla días, la curva resultante supone implícitamente que el mercado no se movió entre
esas fechas; :attr:`ResultadoCalibracionMercado.antiguedad_maxima_dias` cuantifica hasta
dónde llega ese supuesto.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike, NDArray

from motor_tes import config
from motor_tes.curva_nss import (
    UN_BP,
    NSSParams,
    _ajustar_multistart,
    tasa_cero_cupon,
)
from motor_tes.instrumentos import NOMINAL, InstrumentoCotizado

__all__ = [
    "ResiduoInstrumento",
    "ResultadoCalibracionMercado",
    "calibrar_desde_precios",
    "comparar_curvas",
]


@dataclass(frozen=True)
class ResiduoInstrumento:
    """Cómo le fue a un instrumento contra la curva calibrada.

    Attributes:
        ric: Identificador del instrumento.
        etiqueta: Nombre corto para reportes.
        plazo_anios: Plazo al vencimiento en años ACT/365.
        precio_sucio: Precio medio observado más interés corrido.
        precio_modelo: Valor presente de sus flujos bajo la curva calibrada.
        residuo_precio: ``precio_modelo - precio_sucio``, en unidades de precio.
        residuo_bps: El mismo residual dividido por la duración, en bps de tasa. Es la
            magnitud comparable entre instrumentos de plazo distinto.
        duracion: Duración de Macaulay bajo la curva calibrada, en años.
        medio_spread_bps: Media horquilla de tasa, o ``None`` si no hay punta vendedora.
        dentro_de_spread: ``True`` si ``|residuo_bps|`` no supera la media horquilla, o
            sea si la curva pasa por dentro del mercado. ``None`` si no hay horquilla.
        ajustado: ``True`` si el instrumento entró al objetivo de la calibración.
    """

    ric: str
    etiqueta: str
    plazo_anios: float
    precio_sucio: float
    precio_modelo: float
    residuo_precio: float
    residuo_bps: float
    duracion: float
    medio_spread_bps: float | None
    dentro_de_spread: bool | None
    ajustado: bool


@dataclass(frozen=True)
class ResultadoCalibracionMercado:
    """Salida de :func:`calibrar_desde_precios`, con todo lo necesario para auditarla.

    Attributes:
        params: Parámetros calibrados. Para comparar dos calibraciones hay que usar
            :meth:`~motor_tes.curva_nss.NSSParams.curva_equivalente_a` y no el vector
            de parámetros, porque NSS está débilmente identificado en las escalas.
        residuos: Un :class:`ResiduoInstrumento` por instrumento de la canasta, en el
            orden de entrada, incluidos los que no entraron al objetivo.
        rmse_bps: RMSE en bps de tasa, calculado **solo sobre los ajustados**.
        rmse_precio: RMSE en unidades de precio, solo sobre los ajustados.
        svensson: ``True`` si se calibraron 6 parámetros; ``False`` si 4.
        exito: ``True`` si el optimizador reportó convergencia.
        n_evaluaciones: Evaluaciones del objetivo en el mejor arranque.
        n_arranques: Puntos de arranque probados en el multi-start.
        fecha_valoracion: La cotización más reciente de la canasta.
        fecha_mas_antigua: La más vieja. Es la fecha real de la curva: una curva es tan
            fresca como su insumo más viejo.
        antiguedad_maxima_dias: Días entre ambas.
    """

    params: NSSParams
    residuos: tuple[ResiduoInstrumento, ...]
    rmse_bps: float
    rmse_precio: float
    svensson: bool
    exito: bool
    n_evaluaciones: int
    n_arranques: int
    fecha_valoracion: date
    fecha_mas_antigua: date
    antiguedad_maxima_dias: int

    @property
    def n_parametros(self) -> int:
        """Parámetros libres del modelo: 6 en Svensson, 4 en Nelson-Siegel."""
        return 6 if self.svensson else 4

    @property
    def ajustados(self) -> tuple[ResiduoInstrumento, ...]:
        """Los instrumentos que entraron al objetivo."""
        return tuple(r for r in self.residuos if r.ajustado)

    @property
    def no_ajustados(self) -> tuple[ResiduoInstrumento, ...]:
        """Los que quedaron fuera por plazo, valorados y reportados igual."""
        return tuple(r for r in self.residuos if not r.ajustado)

    @property
    def grados_libertad(self) -> int:
        """Instrumentos ajustados menos parámetros libres."""
        return len(self.ajustados) - self.n_parametros

    @property
    def es_interpolacion(self) -> bool:
        """``True`` si no sobran instrumentos y el ajuste perfecto está garantizado."""
        return self.grados_libertad <= 0

    @property
    def plazos(self) -> NDArray[np.float64]:
        """Plazos de los instrumentos ajustados, en años.

        Existe —junto con :attr:`residuales_bps`— para que este resultado se pueda
        consumir en los mismos lugares que
        :class:`~motor_tes.curva_nss.ResultadoCalibracion`: el exportador de Excel y el
        reporte de validación no necesitan saber contra qué se calibró.
        """
        return np.array([r.plazo_anios for r in self.ajustados], dtype=float)

    @property
    def residuales_bps(self) -> NDArray[np.float64]:
        """Residual de cada instrumento ajustado, en bps de tasa."""
        return np.array([r.residuo_bps for r in self.ajustados], dtype=float)

    @property
    def max_residuo_bps(self) -> float:
        """Mayor residual absoluto entre los ajustados, en bps."""
        return max(abs(r.residuo_bps) for r in self.ajustados)

    @property
    def n_dentro_de_spread(self) -> int:
        """Cuántos ajustados caen dentro de la horquilla de mercado."""
        return sum(1 for r in self.ajustados if r.dentro_de_spread)

    def resumen(self) -> str:
        """Línea de resumen legible, para logs y para el reporte de validación."""
        modelo = "NSS (6p)" if self.svensson else "NS (4p)"
        aviso = (
            "  [INTERPOLA: 0 grados de libertad, RMSE no informativo]"
            if self.es_interpolacion
            else ""
        )
        fuera = len(self.no_ajustados)
        cola = f" | fuera del ajuste={fuera}" if fuera else ""
        return (
            f"{modelo} sobre precios | RMSE={self.rmse_bps:.3f} bps | "
            f"max|residual|={self.max_residuo_bps:.3f} bps | "
            f"instrumentos={len(self.ajustados)} | gl={self.grados_libertad} | "
            f"dentro de horquilla={self.n_dentro_de_spread}/{len(self.ajustados)} | "
            f"convergió={self.exito}{cola}{aviso}"
        )

    def tabla(self) -> pd.DataFrame:
        """Residuales instrumento por instrumento.

        Ojo con publicarla: combinada con los parámetros de la curva permite
        reconstruir el precio observado de cada instrumento, así que equivale a
        republicar la cotización. En el reporte versionado van agregados.

        Returns:
            Un ``DataFrame`` con una fila por instrumento.
        """
        return pd.DataFrame(
            {
                "ric": [r.ric for r in self.residuos],
                "etiqueta": [r.etiqueta for r in self.residuos],
                "plazo_anios": [r.plazo_anios for r in self.residuos],
                "duracion": [r.duracion for r in self.residuos],
                "residuo_bps": [r.residuo_bps for r in self.residuos],
                "medio_spread_bps": [r.medio_spread_bps for r in self.residuos],
                "dentro_de_spread": [r.dentro_de_spread for r in self.residuos],
                "ajustado": [r.ajustado for r in self.residuos],
            }
        )


def _flujos_planos(
    cotizados: Sequence[InstrumentoCotizado],
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.intp]]:
    """Aplana los flujos de la canasta para valorarla en una sola pasada.

    Evaluar la curva instrumento por instrumento dentro del objetivo multiplica el
    costo por el número de instrumentos en cada iteración del optimizador. Con un solo
    vector de plazos y ``np.add.reduceat`` para volver a agrupar, la curva se evalúa
    una vez por iteración.

    Args:
        cotizados: Instrumentos con cotización.

    Returns:
        Terna ``(plazos, montos, cortes)``, donde ``cortes`` son los índices de inicio
        de cada instrumento dentro de los vectores planos.
    """
    plazos: list[float] = []
    montos: list[float] = []
    cortes: list[int] = []
    for cotizado in cotizados:
        cortes.append(len(plazos))
        for flujo in cotizado.flujos():
            plazos.append(flujo.t)
            montos.append(flujo.monto)
    return (
        np.asarray(plazos, dtype=float),
        np.asarray(montos, dtype=float),
        np.asarray(cortes, dtype=np.intp),
    )


def calibrar_desde_precios(
    cotizados: Sequence[InstrumentoCotizado],
    svensson: bool = True,
    plazo_minimo_anios: float | None = None,
    semillas_lambda: Iterable[float] | None = None,
) -> ResultadoCalibracionMercado:
    """Calibra la curva minimizando errores de precio ponderados por duración.

    Args:
        cotizados: Canasta de instrumentos con su cotización. Cada uno descuenta desde
            su propia fecha.
        svensson: ``True`` para calibrar los 6 parámetros. Con doce instrumentos
            Svensson queda con 6 grados de libertad, que es la situación en la que la
            extensión de Svensson por fin está justificada.
        plazo_minimo_anios: Plazo por debajo del cual un instrumento se reporta pero no
            entra al objetivo. Por defecto
            :data:`~motor_tes.config.PLAZO_MINIMO_CALIBRACION_ANIOS`.
        semillas_lambda: Rejilla de arranque para las escalas temporales.

    Returns:
        :class:`ResultadoCalibracionMercado` con parámetros, residuales por instrumento
        y las fechas de la muestra.

    Raises:
        ValueError: Si la canasta está vacía, si hay RIC repetidos, si ningún
            instrumento supera el plazo mínimo, o si los que lo superan son menos que
            los parámetros libres del modelo.
        RuntimeError: Si ningún arranque del multi-start converge.
    """
    if not cotizados:
        raise ValueError("La canasta de instrumentos está vacía.")

    rics = [c.ric for c in cotizados]
    if len(set(rics)) != len(rics):
        repetidos = sorted({r for r in rics if rics.count(r) > 1})
        raise ValueError(f"Hay instrumentos repetidos en la canasta: {repetidos}")

    minimo = (
        config.PLAZO_MINIMO_CALIBRACION_ANIOS
        if plazo_minimo_anios is None
        else plazo_minimo_anios
    )
    entra = np.array([c.plazo_anios >= minimo for c in cotizados], dtype=bool)
    n_ajustados = int(entra.sum())
    if n_ajustados == 0:
        raise ValueError(
            f"Ningún instrumento supera el plazo mínimo de {minimo} años: el más largo "
            f"vence en {max(c.plazo_anios for c in cotizados):.3f} años. No hay nada "
            f"que ajustar."
        )

    n_params = 6 if svensson else 4
    if n_ajustados < n_params:
        raise ValueError(
            f"{n_ajustados} instrumentos para {n_params} parámetros: el sistema está "
            f"subdeterminado y el ajuste sería exacto pero arbitrario. Usá "
            f"svensson=False (4 parámetros) o bajá plazo_minimo_anios."
        )

    plazos, montos, cortes = _flujos_planos(cotizados)
    sucios = np.array([c.precio_sucio for c in cotizados], dtype=float)
    montos_por_plazo = montos * plazos

    def _valorar(vector: NDArray[np.float64]) -> tuple[NDArray, NDArray]:
        """Valor presente y duración de Macaulay de cada instrumento."""
        if svensson:
            b0, b1, b2, b3, l1, l2 = vector
        else:
            b0, b1, b2, l1 = vector
            b3, l2 = 0.0, 1.0  # lambda2 inerte cuando beta3 = 0
        candidato = NSSParams(b0, b1, b2, b3, l1, l2)
        descuentos = (1.0 + np.asarray(tasa_cero_cupon(plazos, candidato))) ** -plazos
        pv = np.add.reduceat(montos * descuentos, cortes)
        ponderado = np.add.reduceat(montos_por_plazo * descuentos, cortes)
        return pv, ponderado / pv

    def residuales(vector: NDArray[np.float64]) -> NDArray[np.float64]:
        # El cociente error/duración queda en las unidades del precio, que se cotiza
        # por NOMINAL de facial; dividir por NOMINAL lo lleva a tasa en decimal, que es
        # la unidad en la que el resto del motor mide residuales.
        pv, duracion = _valorar(vector)
        return ((pv - sucios) / duracion / NOMINAL)[entra]

    yields = np.array([c.cotizacion.mid_yield for c in cotizados], dtype=float)
    plazos_instrumento = np.array([c.plazo_anios for c in cotizados], dtype=float)
    yields_ajustados = yields[entra]
    plazos_ajustados = plazos_instrumento[entra]
    nivel_inicial = float(yields_ajustados[np.argmax(plazos_ajustados)])
    pendiente_inicial = float(
        yields_ajustados[np.argmin(plazos_ajustados)] - nivel_inicial
    )

    params, mejor, n_arranques = _ajustar_multistart(
        residuales,
        svensson=svensson,
        nivel_inicial=nivel_inicial,
        pendiente_inicial=pendiente_inicial,
        semillas_lambda=semillas_lambda,
        n_observaciones=n_ajustados,
        nombre_observaciones="instrumentos",
    )

    pv, duracion = _valorar(mejor.x)
    residuo_precio = pv - sucios
    residuo_bps = residuo_precio / duracion / NOMINAL / UN_BP

    residuos: list[ResiduoInstrumento] = []
    for i, cotizado in enumerate(cotizados):
        medio_spread = cotizado.cotizacion.medio_spread_bps
        residuos.append(
            ResiduoInstrumento(
                ric=cotizado.ric,
                etiqueta=cotizado.etiqueta,
                plazo_anios=float(plazos_instrumento[i]),
                precio_sucio=float(sucios[i]),
                precio_modelo=float(pv[i]),
                residuo_precio=float(residuo_precio[i]),
                residuo_bps=float(residuo_bps[i]),
                duracion=float(duracion[i]),
                medio_spread_bps=medio_spread,
                dentro_de_spread=(
                    None
                    if medio_spread is None
                    else bool(abs(residuo_bps[i]) <= medio_spread)
                ),
                ajustado=bool(entra[i]),
            )
        )

    fechas = [c.liquidacion for c in cotizados]
    fecha_valoracion, fecha_mas_antigua = max(fechas), min(fechas)

    return ResultadoCalibracionMercado(
        params=params,
        residuos=tuple(residuos),
        rmse_bps=float(np.sqrt(np.mean(residuo_bps[entra] ** 2))),
        rmse_precio=float(np.sqrt(np.mean(residuo_precio[entra] ** 2))),
        svensson=svensson,
        exito=bool(mejor.success),
        n_evaluaciones=int(mejor.nfev),
        n_arranques=n_arranques,
        fecha_valoracion=fecha_valoracion,
        fecha_mas_antigua=fecha_mas_antigua,
        antiguedad_maxima_dias=(fecha_valoracion - fecha_mas_antigua).days,
    )


def comparar_curvas(
    referencia: NSSParams,
    contraste: NSSParams,
    plazos: ArrayLike,
    nombre_referencia: str = "referencia",
    nombre_contraste: str = "contraste",
) -> pd.DataFrame:
    """Tabula dos curvas y su diferencia por plazo.

    Comparar los vectores de parámetros no sirve: NSS está débilmente identificado en
    las escalas temporales, así que dos parametrizaciones muy distintas pueden describir
    la misma curva. Lo que se compara es la curva evaluada.

    Args:
        referencia: Parámetros de la primera curva.
        contraste: Parámetros de la segunda.
        plazos: Plazos donde evaluar, en años.
        nombre_referencia: Nombre de columna para la primera curva.
        nombre_contraste: Nombre de columna para la segunda.

    Returns:
        Un ``DataFrame`` con el plazo, ambas curvas en decimal y la diferencia
        ``referencia - contraste`` en puntos básicos.

    Raises:
        ValueError: Si no se pasa ningún plazo o si alguno no es positivo.
    """
    t = np.asarray(plazos, dtype=float).ravel()
    if t.size == 0:
        raise ValueError("Hay que pasar al menos un plazo.")
    if np.any(t <= 0):
        raise ValueError("Los plazos deben ser estrictamente positivos.")

    z_ref = np.asarray(tasa_cero_cupon(t, referencia), dtype=float)
    z_con = np.asarray(tasa_cero_cupon(t, contraste), dtype=float)
    return pd.DataFrame(
        {
            "plazo_anios": t,
            nombre_referencia: z_ref,
            nombre_contraste: z_con,
            "diferencia_bps": (z_ref - z_con) / UN_BP,
        }
    )
