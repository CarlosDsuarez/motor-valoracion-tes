"""Constantes centrales del motor: endpoints, ids de serie, convenciones y rutas.

Todos los endpoints de esta tabla fueron verificados contra el servicio en vivo el
2026-08-03. Ver el README para el detalle de cómo se obtuvieron.

ADVERTENCIA DE PROCEDENCIA
--------------------------
La API REST de SUAMECA (Banco de la República) **no está documentada públicamente**.
Su URL base y los nombres de método se extrajeron del bundle Angular del portal
(``main-IAAKA6VH.js``), donde aparece::

    urlServiceEstadisticas = urlBase + "/estadisticas-economicas-back/rest/estadisticaEconomicaRestService"

Esto significa que puede cambiar sin aviso. El fetcher está construido para fallar
de forma ruidosa y explícita ante un cambio de esquema, nunca para degradar en
silencio a datos sintéticos.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Rutas del proyecto
# ---------------------------------------------------------------------------

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parents[2]
DIR_DATOS: Final[Path] = RAIZ_PROYECTO / "data"
DIR_DATOS_CRUDOS: Final[Path] = DIR_DATOS / "raw"
RUTA_MANIFEST: Final[Path] = DIR_DATOS / "manifest.json"
#: Fichas de referencia de los instrumentos soberanos: RIC, cupón y vencimiento.
#: Son hechos públicos de un emisor soberano, así que este archivo sí se versiona.
RUTA_INSTRUMENTOS_TES: Final[Path] = DIR_DATOS / "instrumentos_tes.csv"

#: Cotizaciones licenciadas. NO se versionan: los precios son de un proveedor
#: comercial y el repositorio es público. El manifest registra su SHA256 y su
#: cantidad de filas para que la procedencia siga siendo verificable sin publicar
#: el dato.
DIR_DATOS_PRIVADOS: Final[Path] = DIR_DATOS / "privado"
NOMBRE_CSV_COTIZACIONES_TES: Final[str] = "cotizaciones_tes.csv"
RUTA_COTIZACIONES_TES: Final[Path] = DIR_DATOS_PRIVADOS / NOMBRE_CSV_COTIZACIONES_TES
DIR_VALIDACION: Final[Path] = RAIZ_PROYECTO / "validation"
DIR_FIGURAS: Final[Path] = DIR_VALIDACION / "figs"

#: Reportes que no se versionan porque permiten reconstruir el dato licenciado.
#: El residual por instrumento, combinado con los parámetros publicados de la
#: curva, devuelve el precio observado: publicarlo equivale a publicar la
#: cotización. En el repositorio van agregados.
DIR_VALIDACION_PRIVADA: Final[Path] = DIR_VALIDACION / "privado"
DIR_EXCEL: Final[Path] = RAIZ_PROYECTO / "excel"

#: Nombre con el que el portal descarga el export manual de la curva cero cupón TES.
#: Ver :data:`URL_PAGINA_CERO_CUPON_TES` para las instrucciones de descarga.
NOMBRE_XLSX_CERO_CUPON_TES: Final[str] = "Deuda pública.xlsx"
RUTA_XLSX_CERO_CUPON_TES: Final[Path] = DIR_DATOS_CRUDOS / NOMBRE_XLSX_CERO_CUPON_TES

#: Hoja del export que trae los datos. La otra hoja, "Información", trae metadata.
HOJA_XLSX_CERO_CUPON_TES: Final[str] = "Datos"

#: El export trae dos filas de encabezado: nombre de serie y unidad de medida.
FILAS_ENCABEZADO_XLSX_TES: Final[int] = 2

#: Tenores que publica el Banco de la República, en años. Son solo tres por curva:
#: no es una curva continua, sino tres puntos extraídos de la curva cero cupón que el
#: Banco estima con la metodología de Nelson y Siegel (1987) sobre operaciones de SEN
#: y MEC. Tres nodos no bastan para calibrar Nelson-Siegel (4 parámetros) ni Svensson
#: (6), así que el motor los combina con el tramo corto de IBR.
TENORES_TES_ANIOS: Final[tuple[float, ...]] = (1.0, 5.0, 10.0)


# ---------------------------------------------------------------------------
# Endpoints — SUAMECA (Banco de la República)
# ---------------------------------------------------------------------------

SUAMECA_BASE: Final[str] = (
    "https://suameca.banrep.gov.co/estadisticas-economicas-back"
    "/rest/estadisticaEconomicaRestService"
)

#: Serie completa o últimos ``cantDatos`` registros.
#: Respuesta: lista de un objeto con metadata + ``data: [[epoch_ms, valor], ...]``.
SUAMECA_SERIE_X_TIPO_DATO: Final[str] = f"{SUAMECA_BASE}/consultaInformacionSerieXTipoDato"

#: Variante con control de frecuencia de agregación.
SUAMECA_SERIE_X_FECHA_DESDE: Final[str] = (
    f"{SUAMECA_BASE}/consultaInformacionSerieXTipoDatoXFechaDesde"
)

#: Solo metadata (sin el array ``data``). Útil para descubrir/validar ids.
SUAMECA_INFO_BASICA: Final[str] = f"{SUAMECA_BASE}/consultaInformacionBasicaSerie"


class SerieSuameca(int, Enum):
    """Ids de serie de SUAMECA verificados contra el servicio en vivo (2026-08-03).

    El id que aparece en la URL del portal web (p. ej. ``.../informacionSerie/220002/...``)
    es un id de **página**, no de serie: consultarlo contra la API devuelve ``[]``.
    Los ids de abajo son los reales del backend.
    """

    #: TRM. Diaria, COP/USD. Historia desde 1991-11-27. Fuente: Superfinanciera.
    TRM = 1
    #: Tasa de intervención del Banco de la República. Diaria, E.A. base 365.
    POLITICA_MONETARIA = 59
    #: DTF 90 días, semanal. E.A. base 365.
    DTF_90D_SEMANAL = 65
    #: DTF 90 días, mensual. E.A. base 365.
    DTF_90D_MENSUAL = 70
    #: Tasa interbancaria (TIB). Diaria, porcentaje.
    TIB = 89
    #: CDT 90 días, diaria. E.A. base 365.
    CDT_90D = 238
    #: CDT 180 días, diaria. E.A. base 365.
    CDT_180D = 239
    #: CDT 360 días, diaria. E.A. base 365.
    CDT_360D = 240
    #: IBR overnight, nominal base 360. Diaria.
    IBR_ON = 241
    #: IBR 1 mes, nominal base 360. Diaria.
    IBR_1M = 242
    #: IBR 3 meses, nominal base 360. Diaria.
    IBR_3M = 243


#: ``tipoDato`` que usa el portal para pedir la serie de datos. Verificado: 1 devuelve
#: el array ``data`` poblado.
TIPO_DATO_SERIE: Final[int] = 1


# ---------------------------------------------------------------------------
# Endpoints — Socrata (datos.gov.co), fuente secundaria de TRM
# ---------------------------------------------------------------------------

SOCRATA_DOMINIO: Final[str] = "www.datos.gov.co"

#: "Tasa de Cambio Representativa del Mercado - Histórico". 8.318 filas al 2026-08-03.
#: Columnas: ``valor`` (str), ``unidad`` (str), ``vigenciadesde`` / ``vigenciahasta``
#: (ISO-8601 sin zona, p. ej. ``"2026-08-01T00:00:00.000"``).
SOCRATA_DATASET_TRM: Final[str] = "mcec-87by"

#: Dataset espejo; sirve de control cruzado.
SOCRATA_DATASET_TRM_ALT: Final[str] = "32sa-8pi3"


# ---------------------------------------------------------------------------
# Endpoints — Federal Reserve Bank of New York (pata USD)
# ---------------------------------------------------------------------------

#: SOFR publicado por la Fed de Nueva York. API pública, sin llave.
#: Respuesta: ``{"refRates": [{"effectiveDate": "2026-07-30", "type": "SOFR",
#: "percentRate": 3.65, ...}]}``.
NYFED_SOFR_ULTIMOS: Final[str] = (
    "https://markets.newyorkfed.org/api/rates/secured/sofr/last/{n}.json"
)


# ---------------------------------------------------------------------------
# Fuente NO automatizable: curva cero cupón TES
# ---------------------------------------------------------------------------

#: Página del portal donde vive la curva cero cupón TES. Los datos están detrás de un
#: embed de Oracle Analytics DV, no de la API JSON: no hay id de serie que los exponga
#: (escaneo exhaustivo de ids 1-2599 y bandas altas: 235 series, ninguna cero cupón).
#: La única vía es exportar la tabla a Excel manualmente desde esa página.
URL_PAGINA_CERO_CUPON_TES: Final[str] = (
    "https://suameca.banrep.gov.co/estadisticas-economicas"
    "/informacionSerie/640001/deuda_publica_tasas_cero_cupon_tes"
)


# ---------------------------------------------------------------------------
# Convenciones de conteo de días
# ---------------------------------------------------------------------------


class BaseDia(str, Enum):
    """Convención de conteo de días, explícita en toda la API pública del motor."""

    #: Días calendario sobre 365. Convención de la curva TES en COP (tasa E.A.).
    ACT_365 = "ACT/365"
    #: Días calendario sobre 360. Convención del IBR (nominal) y de SOFR.
    ACT_360 = "ACT/360"


class ConvencionTasa(str, Enum):
    """Cómo se compone una tasa al llevarla a factor de capitalización."""

    #: Efectiva anual con base 365: ``(1 + i) ** t``, ``t`` en años ACT/365.
    #: Convención de la curva cero cupón TES en pesos.
    EA_365 = "EA_365"
    #: Interés simple con base 360: ``1 + i * dias / 360``.
    #: Convención de SOFR y de la fórmula lineal de paridad cubierta del enunciado.
    SIMPLE_360 = "SIMPLE_360"


#: Base de la curva TES cero cupón en COP. Las tasas publicadas por el Banco de la
#: República son efectivas anuales; el descuento es ``(1 + z(t)) ** -t`` con t en ACT/365.
BASE_CURVA_COP: Final[BaseDia] = BaseDia.ACT_365

#: Base de la pata USD (SOFR).
BASE_CURVA_USD: Final[BaseDia] = BaseDia.ACT_360

#: Días por año usados para convertir plazos en días a fracción de año en cada base.
DIAS_POR_ANIO: Final[dict[BaseDia, float]] = {
    BaseDia.ACT_365: 365.0,
    BaseDia.ACT_360: 360.0,
}


# ---------------------------------------------------------------------------
# Parámetros numéricos
# ---------------------------------------------------------------------------

#: Umbral de RMSE (en puntos básicos) por debajo del cual se considera que la
#: calibración NSS reproduce los nodos de mercado. Usado en tests y en el reporte.
RMSE_MAXIMO_BPS: Final[float] = 5.0

#: Por debajo de este cociente ``t / lambda`` se usa la expansión de Taylor de los
#: factores de carga NSS para evitar la indeterminación 0/0 en ``t -> 0``.
UMBRAL_TAYLOR: Final[float] = 1e-6

#: Plazo mínimo, en años, para que un instrumento entre a la calibración contra
#: precios. Las letras muy cortas del mercado COP cotizan en un segmento de dinero
#: que no se conecta suavemente con la curva de bonos: en la muestra de referencia
#: hay un salto de ~150 bps entre el instrumento a 0.17 años y el de 0.44. Una NSS
#: no puede atravesar ese quiebre, y forzarla degrada el ajuste en TODA la curva
#: (RMSE 32 bps contra 3.8, y el nodo de 2 años se desvía 24 bps). El corte va en
#: medio del hueco: los instrumentos por debajo se valoran y reportan, pero no
#: entran al objetivo.
PLAZO_MINIMO_CALIBRACION_ANIOS: Final[float] = 0.30

#: Umbral de RMSE (en bps de tasa) para la calibración contra precios de mercado.
#: Más holgado que :data:`RMSE_MAXIMO_BPS` porque ahí se ajusta contra una curva ya
#: suavizada y acá contra precios crudos de instrumentos individuales. El valor
#: medido sobre la muestra de referencia es 3.8 bps.
RMSE_MAXIMO_BPS_MERCADO: Final[float] = 8.0

#: Tolerancia al reproducir los yields publicados por el proveedor de precios, en
#: bps. El error medido con las convenciones verificadas es de 0.02 a 0.08 bps; el
#: resto del margen absorbe el redondeo a tres decimales de la pantalla.
TOLERANCIA_YIELD_PROVEEDOR_BPS: Final[float] = 0.5

#: Timeout por request HTTP, en segundos.
TIMEOUT_HTTP: Final[float] = 30.0
