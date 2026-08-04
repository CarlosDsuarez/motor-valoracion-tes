"""Pricer de forwards USD/COP por paridad cubierta de tasas de interés.

Modelo
------
Sin fricciones ni riesgo de contraparte, el forward queda fijado por no arbitraje::

    F = S0 * K_cop(d) / K_usd(d)

donde ``K`` es el factor de capitalización de cada pata a ``d`` días. Si valiera otra
cosa, se podría fondear en una moneda, cambiar al contado, invertir en la otra y cubrir
el retorno, cerrando una ganancia sin riesgo.

Convenciones: el punto delicado
-------------------------------
La curva COP de este motor entrega tasas **efectivas anuales base ACT/365**; SOFR se
cotiza **simple base ACT/360**. Mezclar bases es la fuente de error más común en una
mesa, así que la convención de cada pata es un parámetro explícito, nunca un supuesto
implícito:

``ConvencionTasa.EA_365``
    ``K_cop = (1 + i) ** (d/365)``. Consistente con cómo se publica la curva COP.
    Es el valor por defecto.

``ConvencionTasa.SIMPLE_360``
    ``K_cop = 1 + i * d/360``. Es la fórmula lineal del enunciado clásico de paridad
    cubierta. Aplicada al **mismo número** de tasa efectiva anual, da un precio
    distinto; :func:`comparar_convenciones` cuantifica la brecha.

La pata USD usa siempre ``SIMPLE_360``, que es la convención real de SOFR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import numpy as np

from motor_tes.config import ConvencionTasa
from motor_tes.curva_nss import UN_BP, NSSParams, tasa_cero_cupon

__all__ = [
    "ResultadoForward",
    "factor_capitalizacion",
    "tasa_cop_del_plazo",
    "pricer_forward",
    "comparar_convenciones",
]

#: Días por año de cada convención, para llevar un plazo en días a fracción de año.
_BASE_ANUAL: Final[dict[ConvencionTasa, float]] = {
    ConvencionTasa.EA_365: 365.0,
    ConvencionTasa.SIMPLE_360: 360.0,
}


# ---------------------------------------------------------------------------
# Factores de capitalización
# ---------------------------------------------------------------------------


def factor_capitalizacion(
    tasa: float, plazo_dias: float, convencion: ConvencionTasa
) -> float:
    """Factor por el que crece una unidad de moneda en ``plazo_dias``.

    Args:
        tasa: Tasa en decimal (0.1210 = 12.10%).
        plazo_dias: Plazo en días calendario. No negativo.
        convencion: Cómo se compone la tasa. Ver :class:`~motor_tes.config.ConvencionTasa`.

    Returns:
        Factor de capitalización. Vale exactamente 1.0 cuando ``plazo_dias`` es 0.

    Raises:
        ValueError: Si el plazo es negativo o el factor resulta no positivo (tasa
            suficientemente negativa como para aniquilar el principal).
    """
    if plazo_dias < 0:
        raise ValueError(f"plazo_dias debe ser no negativo; recibí {plazo_dias}")
    if plazo_dias == 0:
        return 1.0

    if convencion is ConvencionTasa.EA_365:
        base = 1.0 + tasa
        if base <= 0.0:
            raise ValueError(
                f"1 + tasa = {base} no es positivo: la capitalización efectiva anual "
                "no está definida."
            )
        factor = base ** (plazo_dias / _BASE_ANUAL[ConvencionTasa.EA_365])
    else:
        factor = 1.0 + tasa * plazo_dias / _BASE_ANUAL[ConvencionTasa.SIMPLE_360]

    if factor <= 0.0:
        raise ValueError(
            f"El factor de capitalización resultó {factor}, no positivo. "
            f"Revisá la tasa ({tasa}) y el plazo ({plazo_dias} días)."
        )
    return float(factor)


def tasa_cop_del_plazo(plazo_dias: float, params: NSSParams) -> float:
    """Lee de la curva calibrada la tasa COP correspondiente al plazo del forward.

    El plazo se convierte a años bajo ACT/365, que es la base de la curva.

    Args:
        plazo_dias: Plazo del forward, en días calendario.
        params: Parámetros calibrados de la curva COP.

    Returns:
        Tasa cero cupón efectiva anual en decimal.
    """
    return float(tasa_cero_cupon(plazo_dias / 365.0, params))


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoForward:
    """Precio forward y sus sensibilidades.

    Attributes:
        precio: Tasa forward USD/COP, en pesos por dólar.
        spot: Spot usado, en pesos por dólar.
        plazo_dias: Plazo del contrato, en días calendario.
        puntos_forward: ``precio - spot``, la devaluación implícita en pesos. Es lo que
            cotiza una mesa local.
        devaluacion_anualizada: Devaluación implícita expresada como tasa efectiva anual.
        i_cop: Tasa COP usada, en decimal.
        i_usd: Tasa USD usada, en decimal.
        convencion_cop: Convención aplicada a la pata COP.
        convencion_usd: Convención aplicada a la pata USD.
        factor_cop: Factor de capitalización de la pata COP.
        factor_usd: Factor de capitalización de la pata USD.
        extrapolado: ``True`` si el plazo excede el último nodo con el que se calibró
            la curva, de modo que la tasa COP proviene de extrapolar la forma
            paramétrica y no de interpolar entre datos observados. ``None`` si no se
            informó el plazo máximo calibrado.
        sensibilidades: Griegas del contrato. Ver :func:`pricer_forward` para el detalle
            de cada clave y sus unidades.
    """

    precio: float
    spot: float
    plazo_dias: float
    puntos_forward: float
    devaluacion_anualizada: float
    i_cop: float
    i_usd: float
    convencion_cop: ConvencionTasa
    convencion_usd: ConvencionTasa
    factor_cop: float
    factor_usd: float
    extrapolado: bool | None = None
    sensibilidades: dict[str, float] = field(default_factory=dict)

    def resumen(self) -> str:
        """Línea legible para logs y para el reporte de validación."""
        aviso = "  [EXTRAPOLADO]" if self.extrapolado else ""
        return (
            f"F({self.plazo_dias:.0f}d) = {self.precio:,.2f} COP/USD | "
            f"puntos = {self.puntos_forward:+,.2f} | "
            f"deval = {self.devaluacion_anualizada:.2%} E.A. | "
            f"i_COP = {self.i_cop:.4%} ({self.convencion_cop.value}), "
            f"i_USD = {self.i_usd:.4%} ({self.convencion_usd.value}){aviso}"
        )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def pricer_forward(
    S0: float,
    plazo_dias: float,
    i_usd: float,
    params_curva_cop: NSSParams | None = None,
    i_cop: float | None = None,
    convencion_cop: ConvencionTasa = ConvencionTasa.EA_365,
    plazo_max_curva_dias: float | None = None,
) -> ResultadoForward:
    """Valora un forward USD/COP y calcula sus sensibilidades.

    La tasa COP sale de la curva calibrada, interpolada al plazo del contrato, salvo
    que se pase ``i_cop`` explícitamente (útil para tests analíticos y para cotizar
    contra una tasa negociada).

    Sensibilidades devueltas en ``sensibilidades``:

    ``delta_spot``
        ``dF/dS0``. Vale ``factor_cop / factor_usd``: el forward es lineal en el spot,
        de modo que un movimiento de 1 peso en el contado mueve el forward en
        ``delta_spot`` pesos. Se calcula de forma analítica y se contrasta contra
        diferencias finitas.
    ``delta_spot_numerico``
        La misma derivada por diferencias centradas, como control del cálculo analítico.
    ``dv01_cop``
        Cambio del precio forward, en pesos, ante +1 punto básico en la tasa COP.
        Positivo: subir la tasa local encarece el forward.
    ``dv01_usd``
        Cambio del precio forward, en pesos, ante +1 punto básico en la tasa USD.
        Negativo, por simetría de la paridad.
    ``theta_dia``
        Cambio del precio, en pesos, por un día más de plazo, con las tasas fijas.
    ``rho_cop`` / ``rho_usd``
        Las derivadas respecto a cada tasa por unidad (no por punto básico), para quien
        prefiera trabajar en esas unidades.

    Args:
        S0: Spot USD/COP, en pesos por dólar. Estrictamente positivo.
        plazo_dias: Plazo del contrato en días calendario. No negativo.
        i_usd: Tasa USD en decimal, convención simple ACT/360 (SOFR).
        params_curva_cop: Curva COP calibrada. Obligatoria salvo que se pase ``i_cop``.
        i_cop: Tasa COP explícita, en decimal. Si se omite, se lee de la curva.
        convencion_cop: Convención de capitalización de la pata COP.
        plazo_max_curva_dias: Plazo del último nodo con el que se calibró la curva, en
            días. Si se informa y el contrato lo excede, el resultado queda marcado como
            ``extrapolado``. Importa: con la curva IBR automatizada los nodos llegan a
            90 días, así que cotizar a 360 no interpola entre datos observados sino que
            proyecta la forma paramétrica más allá de la evidencia.

    Returns:
        :class:`ResultadoForward` con el precio y el diccionario de griegas.

    Raises:
        ValueError: Si el spot no es positivo, si el plazo es negativo, o si no se
            aportó ni la curva ni una tasa COP explícita.
    """
    if S0 <= 0:
        raise ValueError(f"El spot debe ser positivo; recibí {S0}")
    if plazo_dias < 0:
        raise ValueError(f"plazo_dias debe ser no negativo; recibí {plazo_dias}")
    if i_cop is None:
        if params_curva_cop is None:
            raise ValueError(
                "Hay que aportar params_curva_cop o bien i_cop explícito para "
                "determinar la tasa de la pata COP."
            )
        i_cop = tasa_cop_del_plazo(plazo_dias, params_curva_cop)

    convencion_usd = ConvencionTasa.SIMPLE_360

    factor_cop = factor_capitalizacion(i_cop, plazo_dias, convencion_cop)
    factor_usd = factor_capitalizacion(i_usd, plazo_dias, convencion_usd)
    precio = S0 * factor_cop / factor_usd

    sensibilidades = _sensibilidades(
        S0=S0,
        plazo_dias=plazo_dias,
        i_cop=i_cop,
        i_usd=i_usd,
        convencion_cop=convencion_cop,
        factor_cop=factor_cop,
        factor_usd=factor_usd,
        precio=precio,
    )

    if plazo_dias > 0:
        devaluacion = (precio / S0) ** (365.0 / plazo_dias) - 1.0
    else:
        devaluacion = 0.0

    return ResultadoForward(
        precio=precio,
        spot=S0,
        plazo_dias=plazo_dias,
        puntos_forward=precio - S0,
        devaluacion_anualizada=devaluacion,
        i_cop=i_cop,
        i_usd=i_usd,
        convencion_cop=convencion_cop,
        convencion_usd=convencion_usd,
        factor_cop=factor_cop,
        factor_usd=factor_usd,
        extrapolado=(
            None if plazo_max_curva_dias is None else plazo_dias > plazo_max_curva_dias
        ),
        sensibilidades=sensibilidades,
    )


def _sensibilidades(
    S0: float,
    plazo_dias: float,
    i_cop: float,
    i_usd: float,
    convencion_cop: ConvencionTasa,
    factor_cop: float,
    factor_usd: float,
    precio: float,
) -> dict[str, float]:
    """Calcula las griegas de forma analítica, con control numérico del delta.

    Derivadas, con ``d`` el plazo en días:

    * ``dF/dS0 = K_cop / K_usd``
    * ``dF/di_cop = S0 / K_usd * dK_cop/di_cop``, donde ``dK_cop/di_cop`` es
      ``(d/365)(1+i)**(d/365 - 1)`` en EA_365 y ``d/360`` en SIMPLE_360.
    * ``dF/di_usd = -F * (d/360) / K_usd``
    * ``dF/dd`` combina la derivada temporal de ambos factores.
    """
    delta_spot = factor_cop / factor_usd

    # Control numérico del delta: el forward es lineal en el spot, así que la
    # diferencia centrada debe reproducir la fórmula analítica hasta el redondeo.
    h_spot = max(S0 * 1e-6, 1e-8)
    delta_numerico = (
        (S0 + h_spot) * factor_cop / factor_usd - (S0 - h_spot) * factor_cop / factor_usd
    ) / (2.0 * h_spot)

    if plazo_dias == 0:
        return {
            "delta_spot": delta_spot,
            "delta_spot_numerico": delta_numerico,
            "rho_cop": 0.0,
            "rho_usd": 0.0,
            "dv01_cop": 0.0,
            "dv01_usd": 0.0,
            "theta_dia": 0.0,
        }

    if convencion_cop is ConvencionTasa.EA_365:
        anios = plazo_dias / 365.0
        d_factor_cop = anios * (1.0 + i_cop) ** (anios - 1.0)
        d_factor_cop_dt = factor_cop * np.log1p(i_cop) / 365.0
    else:
        d_factor_cop = plazo_dias / 360.0
        d_factor_cop_dt = i_cop / 360.0

    rho_cop = S0 * d_factor_cop / factor_usd
    rho_usd = -precio * (plazo_dias / 360.0) / factor_usd

    # Theta: derivada respecto al plazo, en pesos por día adicional.
    d_factor_usd_dt = i_usd / 360.0
    theta_dia = S0 * (
        d_factor_cop_dt / factor_usd - factor_cop * d_factor_usd_dt / factor_usd**2
    )

    return {
        "delta_spot": delta_spot,
        "delta_spot_numerico": delta_numerico,
        "rho_cop": rho_cop,
        "rho_usd": rho_usd,
        "dv01_cop": rho_cop * UN_BP,
        "dv01_usd": rho_usd * UN_BP,
        "theta_dia": float(theta_dia),
    }


def comparar_convenciones(
    S0: float,
    plazo_dias: float,
    i_usd: float,
    params_curva_cop: NSSParams | None = None,
    i_cop: float | None = None,
) -> dict[str, float]:
    """Cuantifica la brecha entre las dos convenciones de la pata COP.

    Toma el **mismo número** de tasa COP y lo capitaliza de las dos formas: efectiva
    anual base 365 (consistente con cómo se publica la curva local) y simple base 360
    (la fórmula lineal del enunciado clásico). La diferencia es el costo de mezclar
    bases sin darse cuenta.

    La brecha **no es monótona en el plazo**, porque son dos efectos opuestos: dividir
    por 360 en vez de 365 infla la pata simple, mientras que capitalizar de forma
    compuesta acelera la pata efectiva anual. Medido con TRM 3144.14, SOFR 3.66% y
    tasa COP 12.00%, la fórmula simple **sobrevalora** el forward hasta un máximo de
    unos +24 bps alrededor de los 180-270 días, cruza cero entre 360 y 540 días, y a
    2 años ya lo **subvalora** en ~88 bps. Es decir: el atajo lineal es más caro
    justamente en la ventana donde el forward USD/COP tiene más liquidez.

    Args:
        S0: Spot USD/COP.
        plazo_dias: Plazo en días calendario.
        i_usd: Tasa USD, simple ACT/360.
        params_curva_cop: Curva COP calibrada, si no se pasa ``i_cop``.
        i_cop: Tasa COP explícita, en decimal.

    Returns:
        Diccionario con ``precio_ea_365``, ``precio_simple_360``, la diferencia en
        pesos (``diferencia_cop``) y en puntos básicos del precio (``diferencia_bps``).
    """
    ea = pricer_forward(
        S0, plazo_dias, i_usd, params_curva_cop, i_cop, ConvencionTasa.EA_365
    )
    simple = pricer_forward(
        S0, plazo_dias, i_usd, params_curva_cop, i_cop, ConvencionTasa.SIMPLE_360
    )
    diferencia = simple.precio - ea.precio
    return {
        "precio_ea_365": ea.precio,
        "precio_simple_360": simple.precio,
        "diferencia_cop": diferencia,
        "diferencia_bps": diferencia / ea.precio / UN_BP,
    }
