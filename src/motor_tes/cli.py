"""Línea de comandos del motor.

Cuatro subcomandos que cubren el ciclo completo::

    python -m motor_tes.cli fetch      # descarga las fuentes y registra procedencia
    python -m motor_tes.cli calibrate  # arma los nodos y calibra la curva
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
from pathlib import Path

import numpy as np
import pandas as pd

from motor_tes import config, data_fetch
from motor_tes.config import SerieSuameca
from motor_tes.curva_nss import (
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
from motor_tes.pricer_forward import comparar_convenciones, pricer_forward

__all__ = ["EstadoMercado", "construir_estado", "main"]


@dataclass(frozen=True)
class EstadoMercado:
    """Fotografía del mercado con la que se valora todo en una corrida.

    Attributes:
        nodos: Nodos de la curva COP, con columnas ``plazo_anios``, ``tasa``, ``fuente``.
        calibracion: Resultado de la calibración sobre esos nodos.
        spot: TRM contado, en pesos por dólar.
        sofr: SOFR vigente, en decimal.
        fecha: Fecha de referencia de la curva, tomada del nodo más reciente.
        fecha_spot: Fecha de la TRM usada.
        fecha_sofr: Fecha de la publicación de SOFR usada.

    Las tres fechas se guardan por separado a propósito: no tienen por qué coincidir.
    La TRM se publica todos los días hábiles, el SOFR lo publica la Fed con su propio
    calendario, y la curva TES depende de la última fecha con los tres tenores. Fundir
    todo bajo una sola fecha ocultaría desalineaciones que importan al valorar.
    """

    nodos: pd.DataFrame
    calibracion: ResultadoCalibracion
    spot: float
    sofr: float
    fecha: date
    fecha_spot: date
    fecha_sofr: date

    @property
    def plazo_max_automatico_dias(self) -> float:
        """Último plazo, en días, que proviene de una fuente automatizada (IBR)."""
        automaticos = self.nodos[self.nodos["fuente"] == "IBR"]
        if automaticos.empty:
            return 0.0
        return float(automaticos["plazo_anios"].max() * 365.0)


def construir_estado(svensson: bool = False, registrar: bool = True) -> EstadoMercado:
    """Descarga las fuentes, arma los nodos y calibra la curva.

    Args:
        svensson: ``True`` para calibrar los 6 parámetros. Por defecto se usa
            Nelson-Siegel de 4, que con los 6 nodos disponibles deja 2 grados de
            libertad en lugar de ninguno.
        registrar: Si es ``True``, cada fuente anota su procedencia en el manifest.

    Returns:
        El :class:`EstadoMercado` de la corrida.
    """
    nodos = data_fetch.construir_nodos_curva_cop(registrar=registrar)
    calibracion = calibrar_nss(nodos["plazo_anios"], nodos["tasa"], svensson=svensson)
    serie_trm = data_fetch.fetch_serie_suameca(
        SerieSuameca.TRM, cant_datos=1, registrar=registrar
    )
    serie_sofr = data_fetch.fetch_sofr(n=5, registrar=registrar)
    return EstadoMercado(
        nodos=nodos,
        calibracion=calibracion,
        spot=float(serie_trm["valor"].iloc[-1]),
        sofr=float(serie_sofr["tasa"].iloc[-1]),
        fecha=pd.Timestamp(nodos["fecha"].max()).date(),
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


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Calibra la curva y muestra el diagnóstico nodo por nodo."""
    estado = construir_estado(svensson=args.svensson)
    calibracion = estado.calibracion

    print(f"Curva COP al {estado.fecha}\n")
    print(f"  {calibracion.resumen()}\n")

    print(
        f"  {'plazo':>9}  {'fuente':<7} {'mercado':>10} {'ajustado':>10} {'residual':>11}"
    )
    for plazo, fuente, mercado, ajustado, residual in zip(
        calibracion.plazos,
        estado.nodos["fuente"],
        calibracion.tasas_mercado,
        calibracion.tasas_ajustadas,
        calibracion.residuales_bps,
    ):
        print(
            f"  {plazo:>8.4f}a  {fuente:<7} {mercado:>9.4%} {ajustado:>10.4%} "
            f"{residual:>+8.2f} bps"
        )

    print(f"\n  Parámetros: {np.round(calibracion.params.to_array(), 8).tolist()}")
    print(f"  {chequeo_no_arbitraje(calibracion.params, t_max=10.0).resumen()}")

    if calibracion.es_interpolacion:
        print(
            "\n  AVISO: 0 grados de libertad. El modelo interpola en vez de ajustar, "
            "así que el RMSE no es evidencia de calidad."
        )
    return 0


def cmd_excel(args: argparse.Namespace) -> int:
    """Exporta el libro que consumen las UDFs de VBA."""
    estado = construir_estado(svensson=args.svensson)
    ruta = exportar_libro(
        estado.calibracion, estado.nodos, estado.spot, estado.sofr, estado.fecha
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
    estado = construir_estado(svensson=args.svensson)
    calibracion = estado.calibracion
    figuras = _generar_figuras(estado)
    diagnostico = chequeo_no_arbitraje(calibracion.params, t_max=10.0)

    partes: list[str] = []
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    partes.append(
        f"# Reporte de validación\n\n"
        f"Generado {ahora}\n\n"
        f"| Insumo | Valor | Fecha del dato |\n"
        f"|---|---|---|\n"
        f"| Curva COP (último nodo) | — | {estado.fecha} |\n"
        f"| TRM contado | {estado.spot:,.2f} COP/USD | {estado.fecha_spot} |\n"
        f"| SOFR | {estado.sofr:.4%} | {estado.fecha_sofr} |\n\n"
        "Las tres fechas pueden no coincidir: cada fuente tiene su propio calendario "
        "de publicación. Se muestran por separado para que cualquier desalineación "
        "quede a la vista en vez de esconderse bajo una única fecha de reporte.\n"
    )

    partes.append("## Procedencia de los datos\n")
    procedencia = pd.DataFrame(
        [
            {
                "fuente": clave,
                "origen": registro["origen"],
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
        "API que lo entregue. Todo lo demás se descargó automáticamente.\n"
    )

    partes.append("## Calibración\n")
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
        f"- Valor presente: **{valor_presente(flujos, calibracion.params):.4f}**\n"
        f"- DV01: **{dv01(flujos, calibracion.params):.6f}** por 100 de nominal\n"
        f"- Duración modificada: "
        f"**{duracion_modificada(flujos, calibracion.params):.4f}** años\n\n"
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

    def _agregar(nombre: str, ayuda: str, funcion, con_svensson: bool = True) -> None:
        sub = subcomandos.add_parser(nombre, help=ayuda)
        if con_svensson:
            sub.add_argument(
                "--svensson",
                action="store_true",
                help=(
                    "Calibrar los 6 parámetros de Svensson en lugar de los 4 de "
                    "Nelson-Siegel. Con 6 nodos deja 0 grados de libertad."
                ),
            )
        sub.set_defaults(funcion=funcion)

    _agregar("fetch", "Descarga las fuentes y registra procedencia", cmd_fetch, False)
    _agregar("calibrate", "Calibra la curva y muestra residuales", cmd_calibrate)
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
