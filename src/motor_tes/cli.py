"""Línea de comandos del motor.

Cinco subcomandos que cubren el ciclo completo::

    python -m motor_tes.cli fetch      # descarga las fuentes y registra procedencia
    python -m motor_tes.cli calibrate  # calibra las curvas y muestra los residuales
    python -m motor_tes.cli curvas     # contrasta la curva de fondeo con la de mercado
    python -m motor_tes.cli validate   # genera el reporte de validación con gráficos
    python -m motor_tes.cli excel      # exporta el libro para las UDFs de VBA

Todos los comandos fallan de forma ruidosa si una fuente no está disponible. No hay
modo "seguir con datos de ejemplo": es preferible que el pipeline se detenga a que
produzca números que parezcan reales sin serlo.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Final
from pathlib import Path

import numpy as np
import pandas as pd

from motor_tes import config, data_fetch
from motor_tes.config import SerieSuameca
from motor_tes.calibracion_mercado import (
    ResultadoCalibracionMercado,
    calibrar_desde_precios,
    comparar_curvas,
)
from motor_tes.curva_nss import (
    NSSParams,
    ResultadoCalibracion,
    calibrar_nss,
    chequeo_no_arbitraje,
    duracion_modificada,
    dv01,
    flujos_bono,
    tasa_cero_cupon,
    valor_presente,
)
from motor_tes.export_excel import BONO_EJEMPLO, PLAZOS_FORWARD_DIAS, exportar_libro
from motor_tes.instrumentos import InstrumentoCotizado
from motor_tes.pricer_forward import comparar_convenciones, pricer_forward

__all__ = ["EstadoMercado", "FUENTES_CURVA", "construir_estado", "main"]


#: Fuentes de curva que sabe construir :func:`construir_estado`.
FUENTES_CURVA: Final[tuple[str, ...]] = ("ambas", "mercado", "banrep")

#: Plazos donde se contrastan las dos curvas. Arrancan en 0.5 años porque por debajo la
#: de mercado extrapola fuera del rango de sus instrumentos.
PLAZOS_COMPARACION: Final[tuple[float, ...]] = (
    0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 25.0, 30.0,
)


@dataclass(frozen=True)
class EstadoMercado:
    """Fotografía del mercado con la que se valora todo en una corrida.

    El motor mantiene **dos curvas COP** y cada una trabaja en su dominio:

    ``calibracion`` (IBR + nodos TES del Banco de la República)
        Curva de fondeo. Arranca en el overnight, así que es la base correcta para la
        paridad cubierta y para los forwards USD/COP de plazo corto.
    ``calibracion_mercado`` (precios de instrumentos soberanos)
        Curva de descuento soberana, ajustada contra cotizaciones de 0.44 a 31 años.
        Es la que valora bonos y la que se exporta a Excel y a las UDFs de VBA.

    Tenerlas separadas no es redundancia: es lo que resuelve la vieja limitación de
    mezclar una curva interbancaria con una soberana en un solo objeto. Usar la de
    mercado para forwards a 30 días sería un error grande —extrapolada por debajo de
    su instrumento más corto se desvía cientos de puntos básicos—, y usar la de fondeo
    para descontar un TES a 20 años arrastraría el spread interbancario.

    Attributes:
        nodos: Nodos de la curva de fondeo, con ``plazo_anios``, ``tasa`` y ``fuente``.
        calibracion: Calibración sobre esos nodos. ``None`` si no se construyó.
        canasta: Instrumentos cotizados usados en la curva de mercado.
        calibracion_mercado: Calibración contra precios. ``None`` si no se construyó.
        spot: TRM contado, en pesos por dólar.
        sofr: SOFR vigente, en decimal.
        fecha: Fecha de la curva: la del insumo **más viejo**. Una curva es tan fresca
            como el dato más rancio que la alimenta, y reportar el más nuevo escondería
            justamente la desalineación que hay que vigilar.
        fecha_insumo_reciente: La del insumo más nuevo. Junto con ``fecha`` deja la
            dispersión a la vista.
        fecha_spot: Fecha de la TRM usada.
        fecha_sofr: Fecha de la publicación de SOFR usada.

    Las fechas se guardan por separado a propósito: no tienen por qué coincidir. La TRM
    se publica todos los días hábiles, el SOFR lo publica la Fed con su propio
    calendario, y las cotizaciones pueden traer rezagos por instrumento.
    """

    nodos: pd.DataFrame | None
    calibracion: ResultadoCalibracion | None
    canasta: tuple[InstrumentoCotizado, ...] | None
    calibracion_mercado: ResultadoCalibracionMercado | None
    spot: float
    sofr: float
    fecha: date
    fecha_insumo_reciente: date
    fecha_spot: date
    fecha_sofr: date

    @property
    def antiguedad_dias(self) -> int:
        """Días entre el insumo más viejo de la curva y el más nuevo."""
        return (self.fecha_insumo_reciente - self.fecha).days

    @property
    def fecha_nodos(self) -> date | None:
        """Fecha del nodo más viejo de la curva de fondeo.

        Es la que envejece sin avisar: los nodos IBR se refrescan por API todos los
        días, pero los nodos TES salen de un export manual que se queda quieto hasta
        que alguien lo vuelve a bajar. Reportar el máximo escondería exactamente eso.
        """
        if self.nodos is None:
            return None
        return pd.Timestamp(self.nodos["fecha"].min()).date()

    @property
    def params_fondeo(self) -> NSSParams:
        """Curva para paridad cubierta y forwards: la de fondeo IBR + TES.

        Raises:
            ValueError: Si no se construyó la curva de fondeo.
        """
        if self.calibracion is None:
            raise ValueError(
                "No se construyó la curva de fondeo. Volvé a correr con "
                "fuente_curva='ambas' o 'banrep'."
            )
        return self.calibracion.params

    @property
    def params_descuento(self) -> NSSParams:
        """Curva para descontar soberanos, Excel y VBA: la de mercado.

        Raises:
            ValueError: Si no se construyó la curva de mercado.
        """
        if self.calibracion_mercado is None:
            raise ValueError(
                "No se construyó la curva de mercado. Volvé a correr con "
                "fuente_curva='ambas' o 'mercado'."
            )
        return self.calibracion_mercado.params

    @property
    def plazo_max_automatico_dias(self) -> float:
        """Último plazo, en días, que proviene de una fuente automatizada (IBR)."""
        if self.nodos is None:
            return 0.0
        automaticos = self.nodos[self.nodos["fuente"] == "IBR"]
        if automaticos.empty:
            return 0.0
        return float(automaticos["plazo_anios"].max() * 365.0)

    @property
    def rango_observado_dias(self) -> tuple[float, float] | None:
        """Plazos, en días, del instrumento ajustado más corto y del más largo.

        Fuera de ese rango la curva de mercado extrapola, y hacia abajo lo hace muy
        mal: por debajo de su instrumento más corto se dispara. Cualquier valoración
        fuera del rango debería marcarse.
        """
        if self.calibracion_mercado is None:
            return None
        plazos = [r.plazo_anios for r in self.calibracion_mercado.ajustados]
        return (min(plazos) * 365.0, max(plazos) * 365.0)


def construir_estado(
    svensson: bool | None = None,
    fuente_curva: str = "ambas",
    registrar: bool = True,
) -> EstadoMercado:
    """Descarga las fuentes, arma los nodos y calibra las curvas pedidas.

    Args:
        svensson: ``True`` para calibrar los 6 parámetros. Por defecto depende de la
            curva: la de mercado usa Svensson, porque con doce instrumentos quedan 6
            grados de libertad; la de fondeo usa Nelson-Siegel de 4, porque con seis
            nodos Svensson interpolaría.
        fuente_curva: ``"ambas"``, ``"mercado"`` o ``"banrep"``.
        registrar: Si es ``True``, cada fuente anota su procedencia en el manifest.

    Returns:
        El :class:`EstadoMercado` de la corrida.

    Raises:
        ValueError: Si ``fuente_curva`` no es una de :data:`FUENTES_CURVA`.
        data_fetch.ErrorFuenteDatos: Si falta alguna fuente pedida. El pipeline se
            detiene: no hay modo de seguir con datos simulados.
    """
    if fuente_curva not in FUENTES_CURVA:
        raise ValueError(
            f"fuente_curva debe ser una de {FUENTES_CURVA}; recibí {fuente_curva!r}."
        )

    fechas_insumos: list[date] = []
    nodos = calibracion = canasta = calibracion_mercado = None

    if fuente_curva in ("ambas", "banrep"):
        nodos = data_fetch.construir_nodos_curva_cop(registrar=registrar)
        calibracion = calibrar_nss(
            nodos["plazo_anios"],
            nodos["tasa"],
            svensson=False if svensson is None else svensson,
        )
        fechas_insumos.extend(
            pd.Timestamp(f).date() for f in nodos["fecha"].tolist()
        )

    if fuente_curva in ("ambas", "mercado"):
        instrumentos = data_fetch.construir_instrumentos_cotizados(registrar=registrar)
        canasta = tuple(instrumentos)
        calibracion_mercado = calibrar_desde_precios(
            instrumentos, svensson=True if svensson is None else svensson
        )
        fechas_insumos.extend(c.liquidacion for c in instrumentos)

    serie_trm = data_fetch.fetch_serie_suameca(
        SerieSuameca.TRM, cant_datos=1, registrar=registrar
    )
    serie_sofr = data_fetch.fetch_sofr(n=5, registrar=registrar)

    return EstadoMercado(
        nodos=nodos,
        calibracion=calibracion,
        canasta=canasta,
        calibracion_mercado=calibracion_mercado,
        spot=float(serie_trm["valor"].iloc[-1]),
        sofr=float(serie_sofr["tasa"].iloc[-1]),
        fecha=min(fechas_insumos),
        fecha_insumo_reciente=max(fechas_insumos),
        fecha_spot=pd.Timestamp(serie_trm.index[-1]).date(),
        fecha_sofr=pd.Timestamp(serie_sofr.index[-1]).date(),
    )


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------


def cmd_fetch(_: argparse.Namespace) -> int:
    """Descarga todas las fuentes y muestra qué se obtuvo de cada una."""
    print("Descargando fuentes...\n")

    trm = data_fetch.fetch_serie_suameca(SerieSuameca.TRM, cant_datos=2000)
    print(
        f"  TRM              {len(trm):>6} obs  "
        f"{trm.index.min().date()} -> {trm.index.max().date()}  "
        f"último {trm['valor'].iloc[-1]:,.2f}"
    )

    sofr = data_fetch.fetch_sofr(n=30)
    print(
        f"  SOFR (NY Fed)    {len(sofr):>6} obs  último "
        f"{sofr['tasa'].iloc[-1]:.4%} @ {sofr.index[-1].date()}"
    )

    nodos_ibr = data_fetch.construir_nodos_ibr()
    print(f"  IBR              {len(nodos_ibr):>6} nodos hasta 90 días")

    try:
        tes = data_fetch.cargar_serie_tes_manual()
        print(
            f"  TES cero cupón   {len(tes):>6} obs  "
            f"{tes.index.min().date()} -> {tes.index.max().date()}  "
            f"tenores {list(tes.columns)}  [manual]"
        )
    except data_fetch.FuenteManualAusenteError as exc:
        print(f"\n  TES cero cupón: AUSENTE\n{exc}\n", file=sys.stderr)
        return 1

    print(f"\nProcedencia registrada en {config.RUTA_MANIFEST}")
    for clave, registro in data_fetch.leer_manifest()["fuentes"].items():
        print(
            f"  {clave:34s} {registro['origen']:14s} "
            f"filas={registro['filas']:>6}  sha256={registro['sha256'][:12]}"
        )
    return 0


def _imprimir_curva_fondeo(estado: EstadoMercado) -> None:
    """Detalle nodo por nodo de la curva de fondeo IBR + TES."""
    calibracion = estado.calibracion
    assert calibracion is not None and estado.nodos is not None
    print("Curva de fondeo (IBR + nodos TES del Banco de la República)")
    print(f"  {calibracion.resumen()}\n")
    print(
        f"  {'plazo':>9}  {'fuente':<7} {'mercado':>10} {'ajustado':>10} "
        f"{'residual':>11}  {'fecha':>10}"
    )
    for plazo, fuente, mercado, ajustado, residual, fecha in zip(
        calibracion.plazos,
        estado.nodos["fuente"],
        calibracion.tasas_mercado,
        calibracion.tasas_ajustadas,
        calibracion.residuales_bps,
        estado.nodos["fecha"],
    ):
        print(
            f"  {plazo:>8.4f}a  {fuente:<7} {mercado:>9.4%} {ajustado:>10.4%} "
            f"{residual:>+8.2f} bps  {pd.Timestamp(fecha).date()}"
        )
    print(f"\n  Parámetros: {np.round(calibracion.params.to_array(), 8).tolist()}")
    print(f"  {chequeo_no_arbitraje(calibracion.params, t_max=10.0).resumen()}")
    if calibracion.es_interpolacion:
        print(
            "\n  AVISO: 0 grados de libertad. El modelo interpola en vez de ajustar, "
            "así que el RMSE no es evidencia de calidad."
        )


def _imprimir_curva_mercado(estado: EstadoMercado) -> None:
    """Detalle instrumento por instrumento de la curva de mercado."""
    calibracion = estado.calibracion_mercado
    assert calibracion is not None
    print("Curva de mercado (precios de instrumentos soberanos)")
    print(f"  {calibracion.resumen()}\n")
    print(
        f"  {'instr':<6} {'plazo':>9} {'residual':>11} {'½ horquilla':>13} "
        f"{'dentro':>7}  ajustado"
    )
    for r in calibracion.residuos:
        horquilla = "—" if r.medio_spread_bps is None else f"{r.medio_spread_bps:.2f} bps"
        dentro = "—" if r.dentro_de_spread is None else ("sí" if r.dentro_de_spread else "no")
        print(
            f"  {r.etiqueta:<6} {r.plazo_anios:>8.3f}a {r.residuo_bps:>+8.2f} bps "
            f"{horquilla:>13} {dentro:>7}  {'sí' if r.ajustado else 'NO'}"
        )
    print(f"\n  Parámetros: {np.round(calibracion.params.to_array(), 8).tolist()}")
    print(f"  {chequeo_no_arbitraje(calibracion.params, t_max=30.0).resumen()}")

    rango = estado.rango_observado_dias
    if rango is not None:
        print(
            f"\n  Rango observado: {rango[0]:.0f} a {rango[1]:.0f} días. Fuera de ahí la "
            f"curva extrapola, y por debajo del extremo corto se dispara: no usarla "
            f"para descontar plazos menores a {rango[0]:.0f} días."
        )
    if calibracion.no_ajustados:
        etiquetas = ", ".join(r.etiqueta for r in calibracion.no_ajustados)
        print(
            f"  Fuera del ajuste por plazo ({etiquetas}): cotizan en el segmento de "
            f"dinero, que no se conecta de forma suave con la curva de bonos."
        )


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Calibra la o las curvas y muestra el diagnóstico en detalle."""
    estado = construir_estado(svensson=args.svensson, fuente_curva=args.fuente)

    print(f"Insumos del {estado.fecha} al {estado.fecha_insumo_reciente} ", end="")
    print(f"(antigüedad máxima: {estado.antiguedad_dias} días)\n")

    if estado.calibracion is not None:
        _imprimir_curva_fondeo(estado)
        print()
    if estado.calibracion_mercado is not None:
        _imprimir_curva_mercado(estado)
    return 0


def cmd_curvas(args: argparse.Namespace) -> int:
    """Compara la curva de fondeo con la de mercado plazo por plazo."""
    estado = construir_estado(svensson=args.svensson, fuente_curva="ambas")
    assert estado.calibracion is not None and estado.calibracion_mercado is not None

    print(f"Comparación de curvas COP — insumos al {estado.fecha}\n")
    print(f"  fondeo  : {estado.calibracion.resumen()}")
    print(f"  mercado : {estado.calibracion_mercado.resumen()}\n")

    tabla = comparar_curvas(
        estado.calibracion.params,
        estado.calibracion_mercado.params,
        PLAZOS_COMPARACION,
        nombre_referencia="fondeo",
        nombre_contraste="mercado",
    )
    print(f"  {'plazo':>8} {'fondeo':>10} {'mercado':>10} {'diferencia':>13}")
    for fila in tabla.itertuples(index=False):
        print(
            f"  {fila.plazo_anios:>7.2f}a {fila.fondeo:>9.4%} {fila.mercado:>10.4%} "
            f"{fila.diferencia_bps:>+9.2f} bps"
        )

    dif = tabla["diferencia_bps"].to_numpy()
    print(
        f"\n  Diferencia media {dif.mean():+.2f} bps, desviación estándar "
        f"{dif.std(ddof=1):.2f} bps, rango [{dif.min():+.2f}, {dif.max():+.2f}]."
    )
    return 0


def cmd_excel(args: argparse.Namespace) -> int:
    """Exporta el libro que consumen las UDFs de VBA.

    Va la curva de **mercado**: es la que descuenta soberanos, que es lo que hacen las
    UDFs del libro. La de fondeo queda para los forwards, que se calculan en Python.
    """
    estado = construir_estado(svensson=args.svensson, fuente_curva="ambas")
    assert estado.calibracion_mercado is not None
    ruta = exportar_libro(
        estado.calibracion_mercado,
        None,  # la hoja de insumos lleva agregados: el libro .xlsm se versiona
        estado.spot,
        estado.sofr,
        estado.fecha,
        params_fondeo=estado.params_fondeo,
    )
    print(f"Libro generado: {ruta}")
    print("Para incorporar las macros:  python excel/build_excel.py")
    return 0


# ---------------------------------------------------------------------------
# Reporte de validación
# ---------------------------------------------------------------------------


def _generar_figuras(estado: EstadoMercado) -> list[Path]:
    """Dibuja curva ajustada, residuales y forwards. Devuelve las rutas escritas."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    config.DIR_FIGURAS.mkdir(parents=True, exist_ok=True)
    calibracion = estado.calibracion
    grilla = np.linspace(1 / 365, 10.0, 400)
    ajustada = np.asarray(tasa_cero_cupon(grilla, calibracion.params))
    rutas: list[Path] = []

    figura, ejes = plt.subplots(figsize=(9, 5))
    ejes.plot(grilla, ajustada * 100, label="Curva calibrada", linewidth=1.8)
    for fuente, marcador in (("IBR", "o"), ("TES", "s")):
        mascara = estado.nodos["fuente"] == fuente
        ejes.scatter(
            estado.nodos.loc[mascara, "plazo_anios"],
            estado.nodos.loc[mascara, "tasa"] * 100,
            marker=marcador,
            s=70,
            zorder=5,
            label=f"Nodos {fuente}",
        )
    ejes.set_xlabel("Plazo (años)")
    ejes.set_ylabel("Tasa cero cupón E.A. (%)")
    ejes.set_title(f"Curva COP calibrada vs. nodos de mercado — {estado.fecha}")
    ejes.legend()
    ejes.grid(alpha=0.3)
    ruta = config.DIR_FIGURAS / "curva_vs_mercado.png"
    figura.tight_layout()
    figura.savefig(ruta, dpi=140)
    plt.close(figura)
    rutas.append(ruta)

    figura, ejes = plt.subplots(figsize=(9, 3.5))
    ejes.bar(range(len(calibracion.plazos)), calibracion.residuales_bps, color="#1F3864")
    ejes.set_xticks(range(len(calibracion.plazos)))
    ejes.set_xticklabels([f"{p:.3g}a" for p in calibracion.plazos])
    ejes.axhline(0, color="black", linewidth=0.8)
    ejes.set_ylabel("Residual (bps)")
    ejes.set_title("Residual por nodo")
    ejes.grid(alpha=0.3, axis="y")
    ruta = config.DIR_FIGURAS / "residuales.png"
    figura.tight_layout()
    figura.savefig(ruta, dpi=140)
    plt.close(figura)
    rutas.append(ruta)

    diagnostico = chequeo_no_arbitraje(calibracion.params, t_max=10.0)
    figura, ejes = plt.subplots(figsize=(9, 4))
    ejes.plot(diagnostico.plazos, diagnostico.forwards * 100, linewidth=1.5)
    ejes.axhline(0, color="red", linewidth=0.8, linestyle="--")
    ejes.set_xlabel("Plazo (años)")
    ejes.set_ylabel("Forward instantánea E.A. (%)")
    ejes.set_title("Forwards instantáneas — diagnóstico de no arbitraje")
    ejes.grid(alpha=0.3)
    ruta = config.DIR_FIGURAS / "forwards_instantaneas.png"
    figura.tight_layout()
    figura.savefig(ruta, dpi=140)
    plt.close(figura)
    rutas.append(ruta)

    if estado.calibracion_mercado is not None and calibracion is not None:
        corto = estado.rango_observado_dias[0] / 365.0
        grilla_larga = np.linspace(corto, 30.0, 500)
        figura, ejes = plt.subplots(
            2, 1, figsize=(9, 7), height_ratios=(2, 1), sharex=True
        )
        ejes[0].plot(
            grilla_larga,
            np.asarray(tasa_cero_cupon(grilla_larga, calibracion.params)) * 100,
            label=f"Fondeo IBR + TES Banrep ({estado.fecha_nodos})",
            linewidth=1.8,
        )
        ejes[0].plot(
            grilla_larga,
            np.asarray(
                tasa_cero_cupon(grilla_larga, estado.calibracion_mercado.params)
            )
            * 100,
            label=f"Mercado ({estado.calibracion_mercado.fecha_valoracion})",
            linewidth=1.8,
        )
        # Deliberadamente sin los puntos de TIR cotizada: esta figura se versiona y de
        # un scatter se leen los yields instrumento por instrumento, que es el dato
        # licenciado. Las dos curvas ajustadas son resultado del modelo y sí pueden ir.
        ejes[0].set_ylabel("Tasa E.A. (%)")
        ejes[0].set_title("Curva de fondeo vs. curva de mercado")
        ejes[0].legend()
        ejes[0].grid(alpha=0.3)

        diferencia = comparar_curvas(
            calibracion.params,
            estado.calibracion_mercado.params,
            grilla_larga,
            nombre_referencia="fondeo",
            nombre_contraste="mercado",
        )
        ejes[1].plot(
            grilla_larga,
            diferencia["diferencia_bps"],
            color="#1F3864",
            linewidth=1.6,
        )
        ejes[1].axhline(0, color="black", linewidth=0.8)
        ejes[1].set_xlabel("Plazo (años)")
        ejes[1].set_ylabel("Fondeo − mercado (bps)")
        ejes[1].grid(alpha=0.3)
        ruta = config.DIR_FIGURAS / "curvas_fondeo_vs_mercado.png"
        figura.tight_layout()
        figura.savefig(ruta, dpi=140)
        plt.close(figura)
        rutas.append(ruta)

    return rutas


def _tabla_markdown(df: pd.DataFrame) -> str:
    """Convierte un ``DataFrame`` en tabla markdown, sin dependencias extra."""
    encabezado = "| " + " | ".join(str(c) for c in df.columns) + " |"
    separador = "|" + "|".join("---" for _ in df.columns) + "|"
    filas = [
        "| " + " | ".join(str(v) for v in registro) + " |"
        for _, registro in df.iterrows()
    ]
    return "\n".join([encabezado, separador, *filas])


def cmd_validate(args: argparse.Namespace) -> int:
    """Genera ``validation/reporte_validacion.md`` con gráficos y benchmark."""
    estado = construir_estado(svensson=args.svensson, fuente_curva="ambas")
    calibracion = estado.calibracion
    mercado = estado.calibracion_mercado
    assert calibracion is not None and mercado is not None
    figuras = _generar_figuras(estado)
    diagnostico = chequeo_no_arbitraje(calibracion.params, t_max=10.0)

    partes: list[str] = []
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    partes.append(
        f"# Reporte de validación\n\n"
        f"Generado {ahora}\n\n"
        f"| Insumo | Valor | Fecha del dato |\n"
        f"|---|---|---|\n"
        f"| Curva de fondeo (IBR + TES Banrep) | nodo más viejo | {estado.fecha_nodos} |\n"
        f"| Curva de mercado (precios) | insumo más viejo | {mercado.fecha_mas_antigua} |\n"
        f"| TRM contado | {estado.spot:,.2f} COP/USD | {estado.fecha_spot} |\n"
        f"| SOFR | {estado.sofr:.4%} | {estado.fecha_sofr} |\n\n"
        f"**Fecha de la corrida: {estado.fecha}.** Es la del insumo más viejo de todos, "
        f"no la del más nuevo: una curva es tan fresca como el dato más rancio que la "
        f"alimenta. El insumo más reciente es del {estado.fecha_insumo_reciente}, o sea "
        f"una dispersión de {estado.antiguedad_dias} días.\n\n"
        "Las fechas no tienen por qué coincidir: cada fuente tiene su propio calendario "
        "de publicación. Se muestran por separado para que cualquier desalineación "
        "quede a la vista en vez de esconderse bajo una única fecha de reporte.\n"
    )

    partes.append("## Procedencia de los datos\n")
    procedencia = pd.DataFrame(
        [
            {
                "fuente": clave,
                "origen": registro["origen"],
                "licencia": registro.get("licencia", "abierta"),
                "filas": registro["filas"],
                "sha256": registro["sha256"][:16],
                "descargado": registro["timestamp_utc"],
            }
            for clave, registro in data_fetch.leer_manifest()["fuentes"].items()
        ]
    )
    partes.append(_tabla_markdown(procedencia) + "\n")
    partes.append(
        "`origen = manual_export` marca lo que aportó una persona porque no existe "
        "API que lo entregue. Todo lo demás se descargó automáticamente.\n\n"
        "`licencia = restringida` marca una fuente que **no se versiona**: los precios "
        "vienen de un proveedor comercial y este repositorio es público. De esas "
        "fuentes se registran el SHA256 y la cantidad de filas —así la procedencia "
        "sigue siendo verificable— pero no la ruta, porque el archivo se queda "
        "afuera del repositorio.\n"
    )

    partes.append("## Calibración de la curva de fondeo\n")
    partes.append(f"```\n{calibracion.resumen()}\n```\n")
    nodos_tabla = pd.DataFrame(
        {
            "plazo_anios": np.round(calibracion.plazos, 6),
            "fuente": estado.nodos["fuente"].to_numpy(),
            "tasa_mercado": [f"{v:.4%}" for v in calibracion.tasas_mercado],
            "tasa_ajustada": [f"{v:.4%}" for v in calibracion.tasas_ajustadas],
            "residual_bps": np.round(calibracion.residuales_bps, 3),
        }
    )
    partes.append(_tabla_markdown(nodos_tabla) + "\n")
    partes.append(f"![curva](figs/{figuras[0].name})\n")
    partes.append(f"![residuales](figs/{figuras[1].name})\n")

    partes.append("## Diagnóstico de no arbitraje\n")
    partes.append(f"```\n{diagnostico.resumen()}\n```\n")
    partes.append(f"![forwards](figs/{figuras[2].name})\n")

    partes.append("## Calibración contra precios de mercado\n")
    partes.append(f"```\n{mercado.resumen()}\n```\n")
    rango = estado.rango_observado_dias
    horquillas = [
        r.medio_spread_bps for r in mercado.ajustados if r.medio_spread_bps is not None
    ]
    agregados = pd.DataFrame(
        [
            {"métrica": "instrumentos ajustados", "valor": len(mercado.ajustados)},
            {"métrica": "grados de libertad", "valor": mercado.grados_libertad},
            {"métrica": "RMSE (bps de tasa)", "valor": round(mercado.rmse_bps, 3)},
            {
                "métrica": "máx residual absoluto (bps)",
                "valor": round(mercado.max_residuo_bps, 3),
            },
            {"métrica": "RMSE (unidades de precio)", "valor": round(mercado.rmse_precio, 4)},
            {
                "métrica": "media horquilla mediana (bps)",
                "valor": round(float(np.median(horquillas)), 2),
            },
            {
                "métrica": "ajustados dentro de la horquilla",
                "valor": f"{mercado.n_dentro_de_spread} de {len(mercado.ajustados)}",
            },
            {
                "métrica": "rango observado (días)",
                "valor": f"{rango[0]:.0f} a {rango[1]:.0f}",
            },
            {
                "métrica": "dispersión de fechas (días)",
                "valor": mercado.antiguedad_maxima_dias,
            },
        ]
    )
    partes.append(_tabla_markdown(agregados) + "\n")
    partes.append(
        "El residual de cada instrumento es el error de precio **dividido por su "
        "duración**, no el error de precio crudo. Los errores de precio escalan con la "
        "duración, así que minimizar precio a secas le entregaría la calibración a la "
        "parte larga: medido sobre esta muestra, ponderando por precio los instrumentos "
        "cortos quedan con errores de 130 a 190 bps contra unos 60 ponderando por "
        "duración. El cociente queda en unidades de tasa, de modo que este RMSE es "
        "comparable con el de la curva de fondeo.\n\n"
        "Van agregados y no la tabla instrumento por instrumento: el residual de cada "
        "uno, combinado con los parámetros publicados de la curva, permite reconstruir "
        "el precio observado, y eso equivaldría a republicar la cotización "
        "licenciada.\n"
    )
    if mercado.no_ajustados:
        cortos = mercado.no_ajustados
        etiquetas = ", ".join(r.etiqueta for r in cortos)
        peor = max(abs(r.residuo_bps) for r in cortos)
        menor = min(abs(r.residuo_bps) for r in cortos)
        partes.append(
            f"**Tramo corto fuera del ajuste.** {len(cortos)} instrumentos "
            f"({etiquetas}), de {min(r.plazo_anios for r in cortos) * 365:.0f} a "
            f"{max(r.plazo_anios for r in cortos) * 365:.0f} días, quedan fuera del "
            f"objetivo. Contra la curva calibrada se desvían entre {menor:.0f} y "
            f"{peor:.0f} bps, y esa magnitud es justamente la razón: cotizan en el "
            f"segmento de dinero, que no se conecta de forma suave con la curva de "
            f"bonos. Una NSS no tiene la flexibilidad para atravesar el quiebre, y "
            f"forzarla degrada el ajuste en toda la curva, no solo en el tramo corto.\n\n"
            f"Como consecuencia, **la curva de mercado no debe usarse por debajo de "
            f"{rango[0]:.0f} días**: ahí extrapola y se dispara. Para ese tramo está la "
            f"curva de fondeo, que arranca en el overnight.\n"
        )

    partes.append("## Curva de fondeo vs. curva de mercado\n")
    comparacion_curvas = comparar_curvas(
        calibracion.params,
        mercado.params,
        PLAZOS_COMPARACION,
        nombre_referencia="fondeo",
        nombre_contraste="mercado",
    )
    partes.append(
        _tabla_markdown(
            pd.DataFrame(
                {
                    "plazo_anios": comparacion_curvas["plazo_anios"],
                    "fondeo": [f"{v:.4%}" for v in comparacion_curvas["fondeo"]],
                    "mercado": [f"{v:.4%}" for v in comparacion_curvas["mercado"]],
                    "diferencia_bps": np.round(comparacion_curvas["diferencia_bps"], 2),
                }
            )
        )
        + "\n"
    )
    belly = comparacion_curvas[
        (comparacion_curvas["plazo_anios"] >= 3.0)
        & (comparacion_curvas["plazo_anios"] <= 20.0)
    ]["diferencia_bps"]
    partes.append(
        f"Entre 3 y 20 años la curva de fondeo queda **{belly.mean():+.1f} bps** por "
        f"encima de la de mercado, con desviación estándar de {belly.std(ddof=1):.1f} "
        f"bps. Un desplazamiento casi paralelo a lo largo de diecisiete años de curva no "
        f"es un error de modelo: es una diferencia de nivel en el insumo. Los nodos TES "
        f"que alimentan la curva de fondeo son del {estado.fecha_nodos} y las "
        f"cotizaciones son del {mercado.fecha_valoracion}, o sea "
        f"{(mercado.fecha_valoracion - estado.fecha_nodos).days} días de diferencia.\n\n"
        f"En el extremo corto de la tabla el signo se invierte y la magnitud crece: ahí "
        f"la curva de mercado está sostenida por sus dos instrumentos más cortos y "
        f"empieza a curvarse fuerte. No es comparable con el resto.\n"
    )
    if len(figuras) > 3:
        partes.append(f"![curvas](figs/{figuras[3].name})\n")

    partes.append("## Comparación NS vs. Svensson\n")
    comparacion = []
    for svensson in (False, True):
        alterna = calibrar_nss(
            estado.nodos["plazo_anios"], estado.nodos["tasa"], svensson=svensson
        )
        suavidad = chequeo_no_arbitraje(alterna.params, t_max=10.0).max_curvatura
        comparacion.append(
            {
                "modelo": "Svensson (6p)" if svensson else "Nelson-Siegel (4p)",
                "grados_libertad": alterna.grados_libertad,
                "rmse_bps": round(alterna.rmse_bps, 4),
                "max_curvatura": round(suavidad, 4),
            }
        )
    partes.append(_tabla_markdown(pd.DataFrame(comparacion)) + "\n")
    partes.append(
        "Con 6 nodos, Svensson queda sin grados de libertad: el RMSE nulo está "
        "garantizado por construcción y la curva pierde suavidad. Por eso el modelo "
        "por defecto es Nelson-Siegel.\n"
    )

    partes.append("## Benchmark contra QuantLib\n")
    try:
        from motor_tes import benchmark_quantlib as bench

        cupon, plazo_bono, frecuencia = BONO_EJEMPLO
        resultados = (
            bench.comparar_factores_descuento(calibracion.params)
            + bench.comparar_bono(
                calibracion.params,
                cupon=cupon,
                plazo_anios=int(plazo_bono),
                frecuencia=frecuencia,
            )
            + bench.comparar_forward(calibracion.params, estado.spot, estado.sofr)
        )
        tabla = pd.DataFrame(
            [
                {
                    "magnitud": r.magnitud,
                    "detalle": r.detalle,
                    "propio": f"{r.propio:.10g}",
                    "quantlib": f"{r.quantlib:.10g}",
                    "desvío": f"{r.desvio:.3g}",
                    "estado": "OK" if r.pasa else "FUERA DE TOLERANCIA",
                }
                for r in resultados
            ]
        )
        partes.append(_tabla_markdown(tabla) + "\n")
        partes.append(
            "Los factores de descuento y los forwards coinciden a precisión de máquina. "
            "El desvío del bono proviene de que este motor ubica los flujos en fracciones "
            "de año exactas mientras QuantLib usa calendario real: los cupones de períodos "
            "bisiestos valen 366/365 y los flujos caen unos días más tarde. Son dos "
            "efectos de signo opuesto, y por eso el signo neto cambia con el plazo.\n"
        )
    except ImportError:
        partes.append("QuantLib no está instalado: benchmark omitido.\n")

    partes.append("## Forwards USD/COP\n")
    filas_forward = []
    for dias in PLAZOS_FORWARD_DIAS:
        resultado = pricer_forward(
            estado.spot,
            dias,
            i_usd=estado.sofr,
            params_curva_cop=calibracion.params,
            plazo_max_curva_dias=estado.plazo_max_automatico_dias,
        )
        brecha = comparar_convenciones(
            estado.spot, dias, i_usd=estado.sofr, params_curva_cop=calibracion.params
        )
        filas_forward.append(
            {
                "plazo_dias": dias,
                "forward": f"{resultado.precio:,.2f}",
                "puntos": f"{resultado.puntos_forward:+,.2f}",
                "devaluacion_ea": f"{resultado.devaluacion_anualizada:.2%}",
                "dv01_cop": round(resultado.sensibilidades["dv01_cop"], 4),
                "dv01_usd": round(resultado.sensibilidades["dv01_usd"], 4),
                "theta_dia": round(resultado.sensibilidades["theta_dia"], 4),
                "brecha_conv_bps": round(brecha["diferencia_bps"], 2),
                "extrapolado": "sí" if resultado.extrapolado else "no",
            }
        )
    partes.append(_tabla_markdown(pd.DataFrame(filas_forward)) + "\n")
    partes.append(
        "`extrapolado = sí` marca los plazos que exceden el último nodo automatizado "
        f"({estado.plazo_max_automatico_dias:.0f} días de IBR): ahí la tasa COP sale de "
        "proyectar la forma paramétrica, no de interpolar entre datos observados.\n"
    )

    partes.append("## Riesgo del bono de referencia\n")
    cupon, plazo_bono, frecuencia = BONO_EJEMPLO
    flujos = flujos_bono(cupon, plazo_bono, frecuencia)
    partes.append(
        f"Bono cupón {cupon:.2%}, {plazo_bono:.0f} años, {frecuencia} pago(s) por año, "
        f"nominal 100.\n\n"
        f"- Valor presente: **{valor_presente(flujos, mercado.params):.4f}**\n"
        f"- DV01: **{dv01(flujos, mercado.params):.6f}** por 100 de nominal\n"
        f"- Duración modificada: "
        f"**{duracion_modificada(flujos, mercado.params):.4f}** años\n\n"
        "Este bono es ilustrativo. No hay fuente pública gratuita de reference data de "
        "TES, así que cupón y vencimiento deben verificarse contra Infovalmer o la BVC "
        "antes de usarse para valorar una posición real.\n"
    )

    partes.append("## Suite de pruebas\n")
    proceso = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
        cwd=config.RAIZ_PROYECTO,
        capture_output=True,
        text=True,
    )
    salida = (proceso.stdout or proceso.stderr).strip().splitlines()
    partes.append("```\n" + "\n".join(salida[-12:]) + "\n```\n")

    config.DIR_VALIDACION.mkdir(parents=True, exist_ok=True)
    ruta = config.DIR_VALIDACION / "reporte_validacion.md"
    ruta.write_text("\n".join(partes), encoding="utf-8")

    # El detalle instrumento por instrumento va aparte y no se versiona: junto con los
    # parámetros publicados de la curva permite reconstruir el precio observado.
    config.DIR_VALIDACION_PRIVADA.mkdir(parents=True, exist_ok=True)
    ruta_privada = config.DIR_VALIDACION_PRIVADA / "residuales_mercado.md"
    detalle = mercado.tabla()
    ruta_privada.write_text(
        "# Residuales por instrumento — NO VERSIONAR\n\n"
        f"Generado {ahora}. Curva de mercado al {mercado.fecha_valoracion}.\n\n"
        "Este archivo queda fuera del repositorio a propósito: combinado con los "
        "parámetros de la curva reconstruye el precio de cada instrumento, así que "
        "publicarlo equivaldría a republicar la cotización licenciada.\n\n"
        + _tabla_markdown(
            pd.DataFrame(
                {
                    "ric": detalle["ric"],
                    "etiqueta": detalle["etiqueta"],
                    "plazo_anios": np.round(detalle["plazo_anios"], 4),
                    "duracion": np.round(detalle["duracion"], 4),
                    "residuo_bps": np.round(detalle["residuo_bps"], 3),
                    "medio_spread_bps": detalle["medio_spread_bps"],
                    "dentro_de_spread": detalle["dentro_de_spread"],
                    "ajustado": detalle["ajustado"],
                }
            )
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Reporte generado: {ruta}")
    for figura in figuras:
        print(f"  figura: {figura}")
    return 0 if proceso.returncode == 0 else 1


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Procesa argumentos y despacha al subcomando correspondiente."""
    parser = argparse.ArgumentParser(
        prog="motor_tes",
        description="Motor de valoración de renta fija local y forwards USD/COP.",
    )
    subcomandos = parser.add_subparsers(dest="comando", required=True)

    def _agregar(
        nombre: str,
        ayuda: str,
        funcion,
        con_svensson: bool = True,
        con_fuente: bool = False,
    ) -> None:
        sub = subcomandos.add_parser(nombre, help=ayuda)
        if con_svensson:
            # default=None, no False: sin la bandera cada curva usa el modelo que le
            # corresponde —Svensson para la de mercado, que tiene 12 instrumentos, y
            # Nelson-Siegel para la de fondeo, que con 6 nodos interpolaría—. Pasarla
            # fuerza Svensson en las dos.
            sub.add_argument(
                "--svensson",
                action="store_true",
                default=None,
                help=(
                    "Forzar los 6 parámetros de Svensson en todas las curvas. Sin la "
                    "bandera, cada curva usa el modelo que sus datos soportan."
                ),
            )
        if con_fuente:
            sub.add_argument(
                "--fuente",
                choices=FUENTES_CURVA,
                default="ambas",
                help=(
                    "Qué curvas construir: 'mercado' (precios de instrumentos), "
                    "'banrep' (IBR + nodos publicados) o 'ambas' (defecto)."
                ),
            )
        sub.set_defaults(funcion=funcion)

    _agregar("fetch", "Descarga las fuentes y registra procedencia", cmd_fetch, False)
    _agregar(
        "calibrate", "Calibra las curvas y muestra residuales", cmd_calibrate,
        con_fuente=True,
    )
    _agregar("curvas", "Compara la curva de fondeo con la de mercado", cmd_curvas)
    _agregar("validate", "Genera el reporte de validación", cmd_validate)
    _agregar("excel", "Exporta el libro para las UDFs de VBA", cmd_excel)

    args = parser.parse_args(argv)
    try:
        return int(args.funcion(args))
    except data_fetch.ErrorFuenteDatos as exc:
        print(f"\nFALLÓ: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
