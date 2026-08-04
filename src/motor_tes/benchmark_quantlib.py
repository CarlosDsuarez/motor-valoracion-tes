"""Validación cruzada del motor propio contra QuantLib.

Por qué
-------
Un motor de valoración escrito desde cero puede estar internamente consistente y aun
así equivocarse: basta una convención de conteo de días mal aplicada o un exponente
invertido para que todos los tests propios pasen y el precio sea incorrecto. QuantLib
es una implementación independiente y ampliamente auditada, así que sirve de árbitro.

Qué se compara
--------------
1. **Factores de descuento** de la curva NSS contra una ``ql.ZeroCurve`` alimentada con
   las mismas tasas cero cupón.
2. **Valor presente, DV01 y duración** de un bono TES a tasa fija.
3. **Precio forward USD/COP** contra la construcción equivalente por factores de
   descuento de dos curvas.

Alineación de convenciones
--------------------------
Para que la comparación sea informativa hay que eliminar las diferencias espurias:

* La grilla se define en **días enteros** desde la fecha de evaluación, y el mismo
  conjunto de días se usa de los dos lados. Así ``t = dias/365`` es idéntico para ambos.
* QuantLib usa ``Actual365Fixed`` con capitalización ``Compounded`` y frecuencia
  ``Annual``, que es exactamente ``DF = (1+z)^-t`` del motor propio.
* Los flujos del bono se construyen sin calendario de días hábiles de los dos lados
  (``ql.NullCalendar`` y convención ``Unadjusted``), porque el motor propio tampoco
  ajusta por días no hábiles.

Lo que quede después de eso es discrepancia real, y se reporta con su magnitud en vez
de esconderse bajo una tolerancia generosa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import QuantLib as ql

from motor_tes.curva_nss import (
    UN_BP,
    FlujoCaja,
    NSSParams,
    duracion_macaulay,
    dv01,
    factor_descuento,
    flujos_bono,
    tasa_cero_cupon,
    valor_presente,
)

__all__ = [
    "ResultadoComparacion",
    "TOLERANCIA_DF",
    "TOLERANCIA_RELATIVA_DV01",
    "comparar_factores_descuento",
    "comparar_bono",
    "comparar_forward",
]

#: Desvío absoluto máximo aceptable entre factores de descuento.
TOLERANCIA_DF: Final[float] = 1e-8

#: Desvío relativo máximo aceptable en DV01 y valor presente.
TOLERANCIA_RELATIVA_DV01: Final[float] = 5e-3

#: Fecha de evaluación de referencia. Arbitraria: la comparación es sobre plazos
#: relativos, no sobre fechas de calendario.
_FECHA_EVAL: Final[ql.Date] = ql.Date(3, 8, 2026)

_DIA_BASE: Final[ql.DayCounter] = ql.Actual365Fixed()
_CALENDARIO: Final[ql.Calendar] = ql.NullCalendar()


@dataclass(frozen=True)
class ResultadoComparacion:
    """Contraste de una magnitud entre el motor propio y QuantLib.

    Attributes:
        magnitud: Qué se comparó (``"factor_descuento"``, ``"dv01"``, ...).
        detalle: Identificador del caso (plazo, bono, etc.).
        propio: Valor que produce este motor.
        quantlib: Valor que produce QuantLib.
        tolerancia: Umbral aplicado.
        relativa: ``True`` si la tolerancia es relativa; ``False`` si es absoluta.
    """

    magnitud: str
    detalle: str
    propio: float
    quantlib: float
    tolerancia: float
    relativa: bool

    @property
    def diferencia(self) -> float:
        """Diferencia con signo: propio menos QuantLib."""
        return self.propio - self.quantlib

    @property
    def desvio(self) -> float:
        """Desvío en las unidades de la tolerancia (relativo o absoluto)."""
        if not self.relativa:
            return abs(self.diferencia)
        if self.quantlib == 0.0:
            return abs(self.diferencia)
        return abs(self.diferencia / self.quantlib)

    @property
    def pasa(self) -> bool:
        """``True`` si el desvío está dentro de la tolerancia."""
        return self.desvio <= self.tolerancia

    def __str__(self) -> str:
        estado = "OK" if self.pasa else "FUERA DE TOLERANCIA"
        unidad = "rel" if self.relativa else "abs"
        return (
            f"[{estado}] {self.magnitud} ({self.detalle}): "
            f"propio={self.propio:.10g} quantlib={self.quantlib:.10g} "
            f"desvío={self.desvio:.3g} {unidad} (tol={self.tolerancia:g})"
        )


def _curva_quantlib(
    params: NSSParams, dias_grilla: np.ndarray, shift: float = 0.0
) -> ql.YieldTermStructureHandle:
    """Construye una ``ql.ZeroCurve`` con las tasas cero cupón del motor propio.

    Args:
        params: Parámetros calibrados.
        dias_grilla: Plazos de los nodos de la curva, en días enteros desde la fecha
            de evaluación. Deben estar ordenados y ser positivos.
        shift: Desplazamiento paralelo aplicado a todas las tasas, en decimal.

    Returns:
        Handle a la estructura temporal, lista para descontar.
    """
    fechas = [_FECHA_EVAL] + [_FECHA_EVAL + int(d) for d in dias_grilla]
    tasas_grilla = np.asarray(tasa_cero_cupon(dias_grilla / 365.0, params)) + shift
    # El primer nodo replica la tasa instantánea; QuantLib exige un punto en t=0.
    tasas = [float(params.tasa_instantanea + shift)] + [float(t) for t in tasas_grilla]

    curva = ql.ZeroCurve(
        fechas, tasas, _DIA_BASE, _CALENDARIO, ql.Linear(), ql.Compounded, ql.Annual
    )
    curva.enableExtrapolation()
    return ql.YieldTermStructureHandle(curva)


def comparar_factores_descuento(
    params: NSSParams, dias: tuple[int, ...] = (30, 90, 180, 360, 720, 1825, 3650)
) -> list[ResultadoComparacion]:
    """Contrasta ``DF(t)`` del motor propio contra la curva equivalente de QuantLib.

    Args:
        params: Parámetros calibrados.
        dias: Plazos a comparar, en días enteros.

    Returns:
        Una comparación por plazo.
    """
    grilla = np.array(dias, dtype=float)
    handle = _curva_quantlib(params, grilla)

    resultados = []
    for d in dias:
        propio = float(factor_descuento(d / 365.0, params))
        quantlib = handle.discount(_FECHA_EVAL + int(d))
        resultados.append(
            ResultadoComparacion(
                magnitud="factor_descuento",
                detalle=f"{d}d",
                propio=propio,
                quantlib=quantlib,
                tolerancia=TOLERANCIA_DF,
                relativa=False,
            )
        )
    return resultados


def _bono_quantlib(
    cupon: float,
    plazo_anios: int,
    frecuencia: int,
    nominal: float,
) -> ql.FixedRateBond:
    """Arma el bono en QuantLib sin ajuste por días hábiles.

    El motor propio construye los flujos en años exactos hacia atrás desde el
    vencimiento, sin calendario. Se replica esa convención (``NullCalendar`` y
    ``Unadjusted``) para que la diferencia que quede no sea de calendario.
    """
    vencimiento = _FECHA_EVAL + ql.Period(plazo_anios, ql.Years)
    cronograma = ql.Schedule(
        _FECHA_EVAL,
        vencimiento,
        ql.Period(ql.Annual if frecuencia == 1 else ql.Semiannual),
        _CALENDARIO,
        ql.Unadjusted,
        ql.Unadjusted,
        ql.DateGeneration.Backward,
        False,
    )
    return ql.FixedRateBond(0, nominal, cronograma, [cupon], _DIA_BASE)


def comparar_bono(
    params: NSSParams,
    cupon: float = 0.1325,
    plazo_anios: int = 7,
    frecuencia: int = 1,
    nominal: float = 100.0,
) -> list[ResultadoComparacion]:
    """Contrasta valor presente, DV01 y duración de un bono TES a tasa fija.

    El DV01 se calcula de los dos lados como desplazamiento paralelo de la curva cero
    cupón, no como derivada respecto a una TIR única: así ambos miden lo mismo.

    Discrepancia residual y su causa
    --------------------------------
    Los factores de descuento coinciden a precisión de máquina (1e-16), pero el bono
    deja un desvío chico: ~4e-5 relativo en valor presente, ~6e-4 en DV01 y ~1.5e-3 en
    duración. No es error de fórmula. El origen es que el motor propio trabaja con
    **fracciones de año exactas** y QuantLib con **fechas de calendario reales**, y eso
    se manifiesta en dos efectos de signo opuesto:

    1. **Cupones más grandes en años bisiestos.** Con ``Actual365Fixed``, QuantLib paga
       el cupón proporcional a los días devengados. Un bono de cupón 13.25% con fecha
       de evaluación 2026-08-03 paga ``13.286301 = 13.25 * 366/365`` en los períodos
       que contienen un 29 de febrero (2027->2028 y 2031->2032) y 13.25 en el resto.
       El motor propio paga siempre 13.25. Esto **sube** el valor presente de QuantLib.
    2. **Flujos más tardíos.** Del 2026-08-03 al 2033-08-03 hay 2557 días, es decir
       ``t = 7.005479`` años ACT/365 en lugar de 7.0. Cada flujo se descuenta un poco
       más. Esto **baja** el valor presente de QuantLib.

    El signo neto depende del plazo y del cupón: a 7 años gana el primer efecto (el
    valor presente de QuantLib queda por encima) y a 10 años gana el segundo. Un bono a
    1 año no cruza ningún 29 de febrero y ahí el desvío cae a ~2e-6, lo que confirma la
    atribución. Cerrar la brecha exigiría que el motor propio manejara fechas de
    calendario en lugar de fracciones de año: es la extensión natural hacia producción
    y está declarada como limitación en el README.

    Args:
        params: Parámetros calibrados de la curva de descuento.
        cupon: Tasa cupón anual en decimal.
        plazo_anios: Años hasta el vencimiento. Entero, para que el cronograma de
            QuantLib caiga en fechas aniversario exactas.
        frecuencia: Pagos por año (1 o 2).
        nominal: Valor facial.

    Returns:
        Comparaciones de ``valor_presente``, ``dv01`` y ``duracion_macaulay``.
    """
    flujos: list[FlujoCaja] = flujos_bono(cupon, float(plazo_anios), frecuencia, nominal)

    # Grilla densa, no una por flujo. ``ql.ZeroCurve`` interpola linealmente entre sus
    # nodos, así que una grilla rala introduciría error de interpolación que se
    # confundiría con el efecto que queremos medir. Con paso de 30 días la curva de
    # QuantLib reproduce la NSS exacta hasta el orden de 1e-9, y lo único que queda es
    # la diferencia de fechas de flujo (días bisiestos).
    dias_max = int(max(f.t for f in flujos) * 366) + 60
    dias_grilla = np.arange(30.0, dias_max + 30.0, 30.0)

    # QuantLib liquida y devenga respecto a su fecha de evaluación global. Sin fijarla
    # usaría la fecha real del sistema y el bono quedaría desalineado con la curva.
    ql.Settings.instance().evaluationDate = _FECHA_EVAL

    bono = _bono_quantlib(cupon, plazo_anios, frecuencia, nominal)
    medio_bp = UN_BP / 2.0

    def _pv_quantlib(shift: float) -> float:
        motor = ql.DiscountingBondEngine(_curva_quantlib(params, dias_grilla, shift))
        bono.setPricingEngine(motor)
        return bono.NPV()

    dv01_ql = _pv_quantlib(-medio_bp) - _pv_quantlib(+medio_bp)
    pv_ql = _pv_quantlib(0.0)  # deja el motor sin desplazar para el cálculo de la TIR

    # Duración de Macaulay de QuantLib, evaluada sobre el rendimiento equivalente que
    # reproduce el precio que da la curva.
    tir = bono.bondYield(_DIA_BASE, ql.Compounded, ql.Annual)
    tasa_ql = ql.InterestRate(tir, _DIA_BASE, ql.Compounded, ql.Annual)
    duracion_ql = ql.BondFunctions.duration(bono, tasa_ql, ql.Duration.Macaulay)

    etiqueta = f"cupón {cupon:.2%}, {plazo_anios}a, {frecuencia}x/año"
    return [
        ResultadoComparacion(
            magnitud="valor_presente",
            detalle=etiqueta,
            propio=valor_presente(flujos, params),
            quantlib=pv_ql,
            tolerancia=TOLERANCIA_RELATIVA_DV01,
            relativa=True,
        ),
        ResultadoComparacion(
            magnitud="dv01",
            detalle=etiqueta,
            propio=dv01(flujos, params),
            quantlib=dv01_ql,
            tolerancia=TOLERANCIA_RELATIVA_DV01,
            relativa=True,
        ),
        ResultadoComparacion(
            magnitud="duracion_macaulay",
            detalle=etiqueta,
            propio=duracion_macaulay(flujos, params),
            quantlib=duracion_ql,
            tolerancia=TOLERANCIA_RELATIVA_DV01,
            relativa=True,
        ),
    ]


def comparar_forward(
    params: NSSParams,
    spot: float,
    i_usd: float,
    dias: tuple[int, ...] = (30, 90, 180, 360, 720),
) -> list[ResultadoComparacion]:
    """Contrasta el precio forward contra su construcción por factores de descuento.

    Implementación independiente de la paridad cubierta: en vez de multiplicar factores
    de capitalización, se descuenta con dos curvas::

        F = S0 * DF_usd(T) / DF_cop(T)

    Es algebraicamente lo mismo que ``S0 * K_cop / K_usd``, pero pasa por otro camino
    de código y por la interpolación de QuantLib, así que un error de exponente o de
    base se manifiesta como diferencia.

    Args:
        params: Curva COP calibrada.
        spot: Spot USD/COP.
        i_usd: Tasa USD, simple ACT/360.
        dias: Plazos a comparar, en días enteros.

    Returns:
        Una comparación por plazo.
    """
    from motor_tes.pricer_forward import pricer_forward

    grilla = np.array(dias, dtype=float)
    curva_cop = _curva_quantlib(params, grilla)

    resultados = []
    for d in dias:
        fecha = _FECHA_EVAL + int(d)
        df_cop = curva_cop.discount(fecha)
        # La pata USD es simple ACT/360, así que su factor de descuento es 1/(1+i*d/360).
        df_usd = 1.0 / (1.0 + i_usd * d / 360.0)
        forward_ql = spot * df_usd / df_cop

        propio = pricer_forward(spot, d, i_usd=i_usd, params_curva_cop=params).precio
        resultados.append(
            ResultadoComparacion(
                magnitud="forward_usdcop",
                detalle=f"{d}d",
                propio=propio,
                quantlib=forward_ql,
                tolerancia=1e-6,
                relativa=True,
            )
        )
    return resultados
