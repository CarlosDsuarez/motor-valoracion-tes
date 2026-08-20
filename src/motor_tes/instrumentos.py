"""Instrumentos soberanos COP y las convenciones de mercado que los cotizan.

Este módulo describe **qué es** cada instrumento y **cómo lo cotiza el mercado**. No
sabe nada de la curva: convierte precio en tasa y tasa en precio bajo la convención
que corresponde a cada tipo, y arma el cronograma de flujos. Quien descuenta contra
la curva NSS es :mod:`motor_tes.calibracion_mercado`.

Dos convenciones, una por tipo
------------------------------
**Letras (T-BILL).** Interés simple ACT/365 sobre el descuento::

    y = (nominal / P - 1) * 365 / dias

Verificado contra las cotizaciones LSEG Refinitiv del 2026-08-19: reproduce los
yields publicados de CO1MT, CO3MT, CO6MT y CO1YT con error menor a 0.1 bp.

**Bonos (T-BOND).** Compuesto anual con exponente *street* / ISMA. El cupón ``j``
(contando desde 0 para el próximo) se descuenta a ``j + f``, donde::

    f = (dias al proximo cupon) / (dias del periodo de cupon vigente)

Este es el punto fino. Usar ACT/365 transcurrido —es decir, ``t = dias/365``— deja
un sesgo sistemático de −1.07 bp promedio contra los yields de Refinitiv. Con el
exponente ISMA el error máximo sobre los diez bonos de la muestra baja a **0.02 bp**.
El devengo se calcula ACT/365; se probó también ACT/ACT dentro del período y la
diferencia es indistinguible a esta precisión, así que lo que discrimina es el
exponente, no el devengo.

Cupones anuales en el aniversario del vencimiento, sin calendario de días hábiles
—misma simplificación que :func:`motor_tes.curva_nss.flujos_bono`, declarada en el
README y cuantificada por el benchmark contra QuantLib.

No confundir con el descuento por curva
---------------------------------------
La convención ISMA sirve **solo** para traducir precio ↔ yield cotizado, que es como
habla la pantalla. El descuento contra la curva cero cupón sigue siendo
``DF(t) = (1 + z(t)) ** -t`` con ``t`` en años ACT/365, igual que en el resto del
motor. Son dos cosas distintas y mezclarlas es el error clásico.

Convenciones de unidades
------------------------
* Cupones y tasas en **decimal** (0.1325 = 13.25%), como en todo el motor.
* Precios por 100 de nominal, limpios salvo que digan ``sucio``.
* Fechas como :class:`datetime.date`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Final, Sequence

from scipy.optimize import brentq

from motor_tes.curva_nss import UN_BP, FlujoCaja

__all__ = [
    "TipoInstrumento",
    "InstrumentoTES",
    "Cotizacion",
    "InstrumentoCotizado",
    "aniversario",
    "cronograma_cupones",
    "interes_corrido",
    "precio_bono",
    "tir_bono",
    "precio_letra",
    "tir_letra",
    "flujos_descontables",
]

#: Base de días de la curva y del devengo COP.
BASE_ANUAL: Final[float] = 365.0

#: Nominal de referencia: los precios se cotizan por 100 de facial.
NOMINAL: Final[float] = 100.0

#: Cotas del solver de TIR, en decimal. Cubren desde tasas nulas hasta 200% anual.
_TIR_MINIMA: Final[float] = 1e-9
_TIR_MAXIMA: Final[float] = 2.0


class TipoInstrumento(str, Enum):
    """Tipo de instrumento, que determina la convención de cotización."""

    LETRA = "LETRA"
    BONO = "BONO"


# ---------------------------------------------------------------------------
# Aritmética de fechas y cronograma
# ---------------------------------------------------------------------------


def aniversario(referencia: date, anios: int) -> date:
    """Desplaza ``referencia`` un número entero de años.

    El 29 de febrero no existe en años comunes; en ese caso cae al 28. El
    desplazamiento se calcula **siempre desde la fecha original**, nunca encadenando
    saltos de un año: encadenar haría que un vencimiento 29-feb perdiera el día 29
    de forma permanente en el primer año común que atraviese.

    Args:
        referencia: Fecha base.
        anios: Años a sumar; negativo para retroceder.

    Returns:
        La fecha desplazada.
    """
    try:
        return referencia.replace(year=referencia.year + anios)
    except ValueError:
        return referencia.replace(year=referencia.year + anios, day=28)


def cronograma_cupones(vencimiento: date, liquidacion: date) -> tuple[list[date], date]:
    """Fechas de cupón pendientes y el cupón inmediatamente anterior.

    Los TES tasa fija en pesos pagan cupón anual en el aniversario del vencimiento.
    El cronograma se construye hacia atrás desde el vencimiento, sin ajuste por días
    hábiles.

    Args:
        vencimiento: Fecha de vencimiento del instrumento.
        liquidacion: Fecha de liquidación desde la que se valora.

    Returns:
        Par ``(cupones, previo)``: las fechas de cupón estrictamente posteriores a
        ``liquidacion`` en orden ascendente, y la fecha del cupón anterior o igual a
        ``liquidacion`` desde la que corre el devengo.

    Raises:
        ValueError: Si el instrumento ya venció en ``liquidacion``.
    """
    if vencimiento <= liquidacion:
        raise ValueError(
            f"El instrumento vence el {vencimiento}, que no es posterior a la fecha "
            f"de liquidación {liquidacion}: no quedan flujos por descontar."
        )

    pendientes: list[date] = []
    k = 0
    fecha = vencimiento
    while fecha > liquidacion:
        pendientes.append(fecha)
        k += 1
        fecha = aniversario(vencimiento, -k)

    pendientes.reverse()
    return pendientes, fecha


def interes_corrido(
    cupon: float, liquidacion: date, cupon_previo: date, nominal: float = NOMINAL
) -> float:
    """Interés devengado desde el último cupón, base ACT/365.

    Args:
        cupon: Tasa cupón anual en decimal.
        liquidacion: Fecha de liquidación.
        cupon_previo: Fecha del último cupón pagado (o de emisión).
        nominal: Valor facial sobre el que devenga.

    Returns:
        Interés corrido en unidades de precio.

    Raises:
        ValueError: Si ``cupon_previo`` es posterior a ``liquidacion``.
    """
    dias = (liquidacion - cupon_previo).days
    if dias < 0:
        raise ValueError(
            f"El cupón previo ({cupon_previo}) no puede ser posterior a la "
            f"liquidación ({liquidacion})."
        )
    return nominal * cupon * dias / BASE_ANUAL


# ---------------------------------------------------------------------------
# Bonos: convención street / ISMA
# ---------------------------------------------------------------------------


def precio_bono(
    cupon: float,
    vencimiento: date,
    liquidacion: date,
    tir: float,
    nominal: float = NOMINAL,
) -> float:
    """Precio limpio de un bono bullet dada su TIR, convención street/ISMA.

    Args:
        cupon: Tasa cupón anual en decimal.
        vencimiento: Fecha de vencimiento.
        liquidacion: Fecha de liquidación.
        tir: Tasa interna de retorno anual en decimal.
        nominal: Valor facial.

    Returns:
        Precio limpio por ``nominal`` de facial.

    Raises:
        ValueError: Si el bono ya venció o si ``tir`` es menor o igual a -100%.
    """
    if tir <= -1.0:
        raise ValueError(f"La TIR debe ser mayor a -100%; recibí {tir}")

    cupones, previo = cronograma_cupones(vencimiento, liquidacion)
    proximo = cupones[0]
    dias_periodo = (proximo - previo).days
    fraccion = (proximo - liquidacion).days / dias_periodo

    cupon_periodico = nominal * cupon
    sucio = 0.0
    for j, fecha in enumerate(cupones):
        monto = cupon_periodico + (nominal if fecha == vencimiento else 0.0)
        sucio += monto / (1.0 + tir) ** (j + fraccion)

    return sucio - interes_corrido(cupon, liquidacion, previo, nominal)


def tir_bono(
    cupon: float,
    vencimiento: date,
    liquidacion: date,
    precio_limpio: float,
    nominal: float = NOMINAL,
) -> float:
    """TIR de un bono bullet dado su precio limpio, convención street/ISMA.

    Invierte :func:`precio_bono` por Brent sobre un intervalo acotado. Es la función
    que reproduce los yields publicados por Refinitiv dentro de 0.02 bp.

    Args:
        cupon: Tasa cupón anual en decimal.
        vencimiento: Fecha de vencimiento.
        liquidacion: Fecha de liquidación.
        precio_limpio: Precio limpio observado por ``nominal`` de facial.
        nominal: Valor facial.

    Returns:
        TIR anual en decimal.

    Raises:
        ValueError: Si el precio no es positivo, si el bono ya venció, o si la TIR
            implícita cae fuera del intervalo ``(0, 200%]``.
    """
    if precio_limpio <= 0.0:
        raise ValueError(f"El precio limpio debe ser positivo; recibí {precio_limpio}")

    def brecha(tir: float) -> float:
        return precio_bono(cupon, vencimiento, liquidacion, tir, nominal) - precio_limpio

    if brecha(_TIR_MINIMA) * brecha(_TIR_MAXIMA) > 0.0:
        raise ValueError(
            f"La TIR implícita del precio {precio_limpio} cae fuera del intervalo "
            f"[{_TIR_MINIMA:.0e}, {_TIR_MAXIMA:.0%}]. Revisá que el precio esté por "
            f"{nominal} de nominal y el cupón en decimal."
        )
    return float(brentq(brecha, _TIR_MINIMA, _TIR_MAXIMA, xtol=1e-14, rtol=1e-15))


# ---------------------------------------------------------------------------
# Letras: interés simple ACT/365
# ---------------------------------------------------------------------------


def precio_letra(
    vencimiento: date, liquidacion: date, tir: float, nominal: float = NOMINAL
) -> float:
    """Precio de una letra cero cupón dada su tasa, interés simple ACT/365.

    Args:
        vencimiento: Fecha de vencimiento.
        liquidacion: Fecha de liquidación.
        tir: Tasa simple anual en decimal.
        nominal: Valor facial.

    Returns:
        Precio por ``nominal`` de facial.

    Raises:
        ValueError: Si la letra ya venció o si el factor de descuento no es positivo.
    """
    dias = (vencimiento - liquidacion).days
    if dias <= 0:
        raise ValueError(
            f"La letra vence el {vencimiento}, que no es posterior a la fecha de "
            f"liquidación {liquidacion}."
        )
    factor = 1.0 + tir * dias / BASE_ANUAL
    if factor <= 0.0:
        raise ValueError(f"La tasa {tir} implica un factor de descuento no positivo.")
    return nominal / factor


def tir_letra(
    vencimiento: date, liquidacion: date, precio: float, nominal: float = NOMINAL
) -> float:
    """Tasa simple ACT/365 de una letra cero cupón dado su precio.

    Args:
        vencimiento: Fecha de vencimiento.
        liquidacion: Fecha de liquidación.
        precio: Precio observado por ``nominal`` de facial.
        nominal: Valor facial.

    Returns:
        Tasa simple anual en decimal.

    Raises:
        ValueError: Si el precio no es positivo o si la letra ya venció.
    """
    if precio <= 0.0:
        raise ValueError(f"El precio debe ser positivo; recibí {precio}")
    dias = (vencimiento - liquidacion).days
    if dias <= 0:
        raise ValueError(
            f"La letra vence el {vencimiento}, que no es posterior a la fecha de "
            f"liquidación {liquidacion}."
        )
    return (nominal / precio - 1.0) * BASE_ANUAL / dias


# ---------------------------------------------------------------------------
# Instrumento y cotización
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentoTES:
    """Ficha de referencia de un instrumento soberano COP.

    Son hechos públicos de un emisor soberano: identificador, cupón y vencimiento.
    A diferencia de los precios, esta información sí se versiona en el repositorio.

    Attributes:
        ric: Identificador del instrumento en la pantalla (por ejemplo ``CO5YT=RR``).
        etiqueta: Nombre corto para reportes (por ejemplo ``5Y``).
        tipo: :class:`TipoInstrumento`, que fija la convención de cotización.
        cupon: Tasa cupón anual en decimal. Cero para las letras.
        vencimiento: Fecha de vencimiento.
    """

    ric: str
    etiqueta: str
    tipo: TipoInstrumento
    cupon: float
    vencimiento: date

    def __post_init__(self) -> None:
        if not self.ric:
            raise ValueError("El RIC no puede estar vacío.")
        if self.cupon < 0.0:
            raise ValueError(f"{self.ric}: el cupón no puede ser negativo ({self.cupon}).")
        if self.tipo is TipoInstrumento.LETRA and self.cupon != 0.0:
            raise ValueError(
                f"{self.ric}: una letra es cero cupón, pero trae cupón {self.cupon}."
            )


@dataclass(frozen=True)
class Cotizacion:
    """Precios y tasas observados en pantalla para un instrumento.

    El lado *ask* es opcional: en la muestra de referencia hay instrumentos con punta
    compradora únicamente. La ``fecha`` es la de esa cotización en particular y no
    tiene por qué coincidir con la del resto de la muestra; el motor descuenta cada
    instrumento desde su propia fecha justamente para no perder esa distinción.

    Attributes:
        ric: Identificador que la liga a un :class:`InstrumentoTES`.
        fecha: Fecha de la cotización.
        bid: Precio de compra, limpio, por 100 de nominal.
        ask: Precio de venta, limpio. ``None`` si no hay punta vendedora.
        bid_yield: Tasa del lado compra, en decimal.
        ask_yield: Tasa del lado venta, en decimal. ``None`` si no hay punta.
    """

    ric: str
    fecha: date
    bid: float
    ask: float | None
    bid_yield: float
    ask_yield: float | None

    def __post_init__(self) -> None:
        if self.bid <= 0.0:
            raise ValueError(f"{self.ric}: el bid debe ser positivo ({self.bid}).")
        if self.ask is not None and self.ask <= 0.0:
            raise ValueError(f"{self.ric}: el ask debe ser positivo ({self.ask}).")
        if self.ask is not None and self.ask < self.bid:
            raise ValueError(
                f"{self.ric}: el ask ({self.ask}) no puede ser menor al bid "
                f"({self.bid}); las puntas están cruzadas."
            )

    @property
    def mid(self) -> float:
        """Precio medio; el bid solo si no hay punta vendedora."""
        return self.bid if self.ask is None else 0.5 * (self.bid + self.ask)

    @property
    def mid_yield(self) -> float:
        """Tasa media; la del bid solo si no hay punta vendedora."""
        if self.ask_yield is None:
            return self.bid_yield
        return 0.5 * (self.bid_yield + self.ask_yield)

    @property
    def medio_spread_bps(self) -> float | None:
        """Media horquilla en bps de tasa, o ``None`` si falta la punta vendedora.

        Un precio más alto implica una tasa más baja, así que la punta vendedora
        cotiza por debajo de la compradora en tasa.
        """
        if self.ask_yield is None:
            return None
        return 0.5 * (self.bid_yield - self.ask_yield) / UN_BP


@dataclass(frozen=True)
class InstrumentoCotizado:
    """Un instrumento con su cotización: la unidad que entra a la calibración.

    Attributes:
        instrumento: Ficha de referencia.
        cotizacion: Precios observados y la fecha en que se observaron.
    """

    instrumento: InstrumentoTES
    cotizacion: Cotizacion

    def __post_init__(self) -> None:
        if self.instrumento.ric != self.cotizacion.ric:
            raise ValueError(
                f"La cotización es de {self.cotizacion.ric} pero la ficha es de "
                f"{self.instrumento.ric}: no corresponden al mismo instrumento."
            )
        if self.instrumento.vencimiento <= self.cotizacion.fecha:
            raise ValueError(
                f"{self.instrumento.ric} venció el {self.instrumento.vencimiento}, "
                f"antes de la fecha de cotización {self.cotizacion.fecha}."
            )

    @property
    def ric(self) -> str:
        """Identificador del instrumento."""
        return self.instrumento.ric

    @property
    def etiqueta(self) -> str:
        """Nombre corto para reportes."""
        return self.instrumento.etiqueta

    @property
    def liquidacion(self) -> date:
        """Fecha desde la que se descuenta: la de su propia cotización."""
        return self.cotizacion.fecha

    @property
    def plazo_anios(self) -> float:
        """Plazo hasta el vencimiento en años ACT/365, desde su fecha de cotización."""
        dias = (self.instrumento.vencimiento - self.liquidacion).days
        return dias / BASE_ANUAL

    @property
    def es_letra(self) -> bool:
        """``True`` si cotiza por interés simple ACT/365."""
        return self.instrumento.tipo is TipoInstrumento.LETRA

    @property
    def interes_corrido(self) -> float:
        """Interés devengado a la fecha de liquidación. Cero para las letras."""
        if self.es_letra:
            return 0.0
        _, previo = cronograma_cupones(self.instrumento.vencimiento, self.liquidacion)
        return interes_corrido(self.instrumento.cupon, self.liquidacion, previo)

    @property
    def precio_sucio(self) -> float:
        """Precio medio más interés corrido: lo que descuenta la curva."""
        return self.cotizacion.mid + self.interes_corrido

    def tir(self, precio_limpio: float | None = None) -> float:
        """TIR bajo la convención del tipo de instrumento.

        Args:
            precio_limpio: Precio a invertir. Por defecto el medio de la cotización.

        Returns:
            Tasa anual en decimal: simple ACT/365 para letras, compuesta ISMA para
            bonos.
        """
        precio = self.cotizacion.mid if precio_limpio is None else precio_limpio
        if self.es_letra:
            return tir_letra(self.instrumento.vencimiento, self.liquidacion, precio)
        return tir_bono(
            self.instrumento.cupon,
            self.instrumento.vencimiento,
            self.liquidacion,
            precio,
        )

    def flujos(self) -> list[FlujoCaja]:
        """Flujos pendientes con el plazo en años ACT/365 desde su liquidación.

        Es el puente hacia el descuento por curva: a partir de acá el instrumento se
        valora con :func:`motor_tes.curva_nss.valor_presente` como cualquier otro
        conjunto de flujos.

        Returns:
            Lista de :class:`FlujoCaja` ordenada por plazo ascendente.
        """
        vencimiento = self.instrumento.vencimiento
        if self.es_letra:
            return [FlujoCaja(t=self.plazo_anios, monto=NOMINAL)]

        cupones, _ = cronograma_cupones(vencimiento, self.liquidacion)
        cupon_periodico = NOMINAL * self.instrumento.cupon
        return [
            FlujoCaja(
                t=(fecha - self.liquidacion).days / BASE_ANUAL,
                monto=cupon_periodico + (NOMINAL if fecha == vencimiento else 0.0),
            )
            for fecha in cupones
        ]


def flujos_descontables(cotizados: Sequence[InstrumentoCotizado]) -> list[list[FlujoCaja]]:
    """Flujos de una canasta de instrumentos, en el mismo orden que la entrada.

    Args:
        cotizados: Instrumentos con cotización.

    Returns:
        Una lista de flujos por instrumento.
    """
    return [cotizado.flujos() for cotizado in cotizados]
