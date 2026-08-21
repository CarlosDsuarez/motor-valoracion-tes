"""Tests de la carga de instrumentos y cotizaciones, y de la licencia del dato.

Dos grupos. El primero usa archivos temporales y verifica el contrato de carga: qué
columnas se exigen, cómo falla el cruce por RIC, y que una fuente restringida quede
registrada en el manifest **sin** ruta de archivo. El segundo, marcado ``licenciado``,
corre contra las cotizaciones reales y se salta solo cuando no están, que es el caso
en cualquier clon del repositorio.

La regla que sostienen estos tests: cuando falta una fuente, el pipeline se detiene con
una excepción tipada. Nunca degrada en silencio a datos simulados.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from motor_tes import config
from motor_tes.calibracion_mercado import calibrar_desde_precios
from motor_tes.data_fetch import (
    EsquemaInesperadoError,
    FuenteLicenciadaAusenteError,
    FuenteManualAusenteError,
    cargar_cotizaciones_tes,
    cargar_instrumentos_tes,
    construir_instrumentos_cotizados,
)
from motor_tes.instrumentos import TipoInstrumento

FICHAS = """ric,etiqueta,tipo,cupon,vencimiento
AA=RR,2Y,BONO,0.06,2028-04-28
BB=RR,1Y,LETRA,0.0,2027-03-23
"""

#: Precios inventados. Ninguna cotización real entra a un archivo versionado: las
#: pruebas contra el dato de mercado leen el CSV licenciado y están marcadas
#: ``licenciado``.
PRECIOS = """ric,fecha,bid,ask,bid_yield,ask_yield
AA=RR,2026-08-19,88.250,88.400,0.12300,0.12100
BB=RR,2026-08-18,92.000,,0.12100,
"""


@pytest.fixture
def fichas(tmp_path: Path) -> Path:
    ruta = tmp_path / "instrumentos.csv"
    ruta.write_text(FICHAS, encoding="utf-8")
    return ruta


@pytest.fixture
def precios(tmp_path: Path) -> Path:
    ruta = tmp_path / "cotizaciones.csv"
    ruta.write_text(PRECIOS, encoding="utf-8")
    return ruta


# ---------------------------------------------------------------------------
# Contrato de carga
# ---------------------------------------------------------------------------


class TestCargaDeFichas:
    def test_lee_tipos_cupones_y_vencimientos(self, fichas: Path) -> None:
        instrumentos = cargar_instrumentos_tes(fichas)
        assert [i.ric for i in instrumentos] == ["AA=RR", "BB=RR"]
        assert instrumentos[0].tipo is TipoInstrumento.BONO
        assert instrumentos[0].cupon == pytest.approx(0.06)
        assert instrumentos[1].tipo is TipoInstrumento.LETRA
        assert instrumentos[1].vencimiento.isoformat() == "2027-03-23"

    def test_falla_si_el_archivo_no_esta(self, tmp_path: Path) -> None:
        with pytest.raises(FuenteManualAusenteError, match="fichas de instrumentos"):
            cargar_instrumentos_tes(tmp_path / "no_existe.csv")

    def test_falla_si_faltan_columnas(self, tmp_path: Path) -> None:
        ruta = tmp_path / "corto.csv"
        ruta.write_text("ric,etiqueta\nAA=RR,2Y\n", encoding="utf-8")
        with pytest.raises(EsquemaInesperadoError, match="le faltan columnas"):
            cargar_instrumentos_tes(ruta)

    def test_falla_ante_un_tipo_desconocido(self, tmp_path: Path) -> None:
        ruta = tmp_path / "raro.csv"
        ruta.write_text(
            "ric,etiqueta,tipo,cupon,vencimiento\nAA=RR,2Y,SWAP,0.06,2028-04-28\n",
            encoding="utf-8",
        )
        with pytest.raises(EsquemaInesperadoError, match="no es un tipo"):
            cargar_instrumentos_tes(ruta)


class TestCargaDeCotizaciones:
    def test_la_punta_vendedora_vacia_queda_en_none(self, precios: Path) -> None:
        cotizaciones = cargar_cotizaciones_tes(precios, registrar=False)
        assert cotizaciones[0].ask == pytest.approx(88.400)
        assert cotizaciones[1].ask is None
        assert cotizaciones[1].ask_yield is None

    def test_cada_cotizacion_conserva_su_propia_fecha(self, precios: Path) -> None:
        cotizaciones = cargar_cotizaciones_tes(precios, registrar=False)
        assert cotizaciones[0].fecha.isoformat() == "2026-08-19"
        assert cotizaciones[1].fecha.isoformat() == "2026-08-18"

    def test_sin_archivo_se_detiene_con_instrucciones(self, tmp_path: Path) -> None:
        """No hay degradación silenciosa: excepción tipada y mensaje accionable."""
        with pytest.raises(FuenteLicenciadaAusenteError) as excinfo:
            cargar_cotizaciones_tes(tmp_path / "no_existe.csv")
        mensaje = str(excinfo.value)
        assert "NO se versionan" in mensaje
        assert "ric,fecha,bid,ask,bid_yield,ask_yield" in mensaje
        assert "--fuente banrep" in mensaje

    def test_falla_si_faltan_columnas(self, tmp_path: Path) -> None:
        ruta = tmp_path / "corto.csv"
        ruta.write_text("ric,fecha\nAA=RR,2026-08-19\n", encoding="utf-8")
        with pytest.raises(EsquemaInesperadoError, match="le faltan columnas"):
            cargar_cotizaciones_tes(ruta, registrar=False)


class TestProcedenciaDeFuenteRestringida:
    def test_registra_el_hash_pero_no_la_ruta(
        self, precios: Path, monkeypatch, tmp_path: Path
    ) -> None:
        """La procedencia queda verificable sin publicar el archivo.

        Es la pieza que concilia dos reglas del proyecto: toda fuente se registra con
        su SHA256, y los precios licenciados no se versionan.
        """
        manifest = tmp_path / "manifest.json"
        monkeypatch.setattr(config, "RUTA_MANIFEST", manifest)
        monkeypatch.setattr(config, "DIR_DATOS_CRUDOS", tmp_path / "raw")

        cargar_cotizaciones_tes(precios, registrar=True)

        entrada = json.loads(manifest.read_text(encoding="utf-8"))["fuentes"][
            "cotizaciones_tes"
        ]
        assert entrada["licencia"] == "restringida"
        assert entrada["origen"] == "manual_export"
        assert entrada["filas"] == 2
        assert len(entrada["sha256"]) == 64
        assert entrada["archivo"] is None
        assert not (tmp_path / "raw" / "cotizaciones_tes.json").exists()


class TestCruceDeFichasYPrecios:
    def test_devuelve_la_canasta_ordenada_por_plazo(
        self, fichas: Path, precios: Path
    ) -> None:
        canasta = construir_instrumentos_cotizados(fichas, precios, registrar=False)
        plazos = [c.plazo_anios for c in canasta]
        assert plazos == sorted(plazos)
        assert [c.etiqueta for c in canasta] == ["1Y", "2Y"]

    def test_falla_si_una_cotizacion_no_tiene_ficha(
        self, fichas: Path, tmp_path: Path
    ) -> None:
        ruta = tmp_path / "sobra.csv"
        ruta.write_text(
            PRECIOS + "ZZ=RR,2026-08-19,99.0,,0.11,\n", encoding="utf-8"
        )
        with pytest.raises(EsquemaInesperadoError, match="sin ficha de instrumento"):
            construir_instrumentos_cotizados(fichas, ruta, registrar=False)

    def test_falla_si_un_instrumento_no_tiene_precio(
        self, fichas: Path, tmp_path: Path
    ) -> None:
        """Una canasta recortada en silencio calibraría otra curva sin avisar."""
        ruta = tmp_path / "falta.csv"
        ruta.write_text(
            "ric,fecha,bid,ask,bid_yield,ask_yield\n"
            "AA=RR,2026-08-19,88.250,88.400,0.12300,0.12100\n",
            encoding="utf-8",
        )
        with pytest.raises(EsquemaInesperadoError, match="sin cotización"):
            construir_instrumentos_cotizados(fichas, ruta, registrar=False)


# ---------------------------------------------------------------------------
# Contra las cotizaciones reales
# ---------------------------------------------------------------------------

licenciado = pytest.mark.skipif(
    not config.RUTA_COTIZACIONES_TES.exists(),
    reason=f"faltan las cotizaciones licenciadas en {config.RUTA_COTIZACIONES_TES}",
)


@pytest.fixture
def canasta_real():
    return construir_instrumentos_cotizados(registrar=False)


@pytest.mark.licenciado
@licenciado
class TestContraCotizacionesReales:
    def test_reproduce_los_yields_publicados(self, canasta_real) -> None:
        """Las convenciones por tipo reproducen la tasa que muestra la pantalla.

        Es la verificación que fija la convención: con descuento ACT/365 transcurrido
        en vez del exponente street/ISMA, los bonos se desvían cerca de 1 bp de forma
        sistemática. Acá el margen es de centésimas.
        """
        tolerancia = config.TOLERANCIA_YIELD_PROVEEDOR_BPS
        for cotizado in canasta_real:
            calculada = cotizado.tir(cotizado.cotizacion.bid)
            publicada = cotizado.cotizacion.bid_yield
            desvio_bps = abs(calculada - publicada) / 1e-4
            assert desvio_bps < tolerancia, (
                f"{cotizado.ric}: calculada {calculada:.6%} vs publicada "
                f"{publicada:.6%} ({desvio_bps:.2f} bps)"
            )

    def test_la_calibracion_ajusta_dentro_de_la_tolerancia(self, canasta_real) -> None:
        resultado = calibrar_desde_precios(canasta_real, svensson=True)
        assert resultado.exito
        assert resultado.rmse_bps < config.RMSE_MAXIMO_BPS_MERCADO

    def test_svensson_queda_con_grados_de_libertad(self, canasta_real) -> None:
        """El punto del cambio: con doce instrumentos Svensson deja de interpolar."""
        resultado = calibrar_desde_precios(canasta_real, svensson=True)
        assert resultado.grados_libertad == 6
        assert not resultado.es_interpolacion

    def test_las_letras_ultracortas_quedan_fuera_del_ajuste(self, canasta_real) -> None:
        """Y su desajuste es enorme, que es justamente por lo que no se ajustan."""
        resultado = calibrar_desde_precios(canasta_real, svensson=True)
        fuera = resultado.no_ajustados
        assert len(fuera) == 3
        assert all(r.plazo_anios < config.PLAZO_MINIMO_CALIBRACION_ANIOS for r in fuera)
        assert min(abs(r.residuo_bps) for r in fuera) > 100.0

    def test_la_curva_no_tiene_arbitraje(self, canasta_real) -> None:
        from motor_tes.curva_nss import chequeo_no_arbitraje

        resultado = calibrar_desde_precios(canasta_real, svensson=True)
        diagnostico = chequeo_no_arbitraje(resultado.params, t_max=30.0)
        assert diagnostico.sin_arbitraje, diagnostico.resumen()
        assert len(diagnostico.forwards_negativas) == 0
        assert diagnostico.descuentos_monotonos
