"""Conectores a las fuentes de datos del motor.

Principio rector: **nunca degradar en silencio a datos simulados**. Si una fuente falla
o cambia de esquema, este módulo levanta una excepción tipada y el pipeline se detiene.
Toda descarga deja rastro en ``data/manifest.json`` con URL, timestamp, SHA256 y número
de filas, de modo que el reporte de validación pueda declarar exactamente qué dato es
real, de dónde salió y cuándo.

Fuentes
-------
=========================  ==========  ==================================================
Fuente                     Origen      Nota
=========================  ==========  ==================================================
SUAMECA (Banrep)           ``api``     API no documentada; ver :mod:`motor_tes.config`
Socrata (datos.gov.co)     ``api``     Fuente secundaria de TRM, para control cruzado
NY Fed (SOFR)              ``api``     Pata USD del forward; pública, sin llave
Curva cero cupón TES       ``manual``  NO automatizable: export manual desde Oracle DV
=========================  ==========  ==================================================
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

import pandas as pd
import requests

from motor_tes import config
from motor_tes.config import BaseDia, SerieSuameca

__all__ = [
    "ErrorFuenteDatos",
    "FuenteNoDisponibleError",
    "EsquemaInesperadoError",
    "FuenteManualAusenteError",
    "fetch_serie_suameca",
    "fetch_trm_socrata",
    "fetch_sofr",
    "sofr_vigente",
    "cargar_serie_tes_manual",
    "cargar_curva_tes_manual",
    "construir_nodos_ibr",
    "construir_nodos_curva_cop",
    "leer_manifest",
]

Origen = Literal["api", "manual_export"]

#: Plazo en días de las series IBR, que forman el tramo limpio de la curva COP.
#: Se publican como tasa nominal base 360.
PLAZOS_NODOS_IBR: Final[dict[SerieSuameca, int]] = {
    SerieSuameca.IBR_ON: 1,
    SerieSuameca.IBR_1M: 30,
    SerieSuameca.IBR_3M: 90,
}

#: Plazo en días de las series de CDT, publicadas en efectiva anual base 365.
#:
#: NO se incluyen por defecto en la curva. Verificado contra el dato en vivo
#: (2026-07-21 a 2026-07-30): el CDT a 180 días cotiza entre 9.98% y 10.65% mientras
#: el de 360 días va de 11.74% a 12.41% y el IBR 3M ronda 11.8%. Esa inversión de
#: casi 200 bps no es estructura de plazos: los CDT son promedios ponderados por el
#: monto efectivamente emitido cada día, así que el nivel refleja qué bancos captaron
#: y a qué plazo, no el precio del dinero a ese plazo. Meterlos como nodos produce un
#: zigzag que ninguna familia paramétrica suave puede ajustar (RMSE ~59 bps medido).
PLAZOS_NODOS_CDT: Final[dict[SerieSuameca, int]] = {
    SerieSuameca.CDT_180D: 180,
    SerieSuameca.CDT_360D: 360,
}

#: Series que el Banco de la República publica como tasa **nominal base 360**
#: (requieren conversión a efectiva anual antes de mezclarlas con la curva TES).
SERIES_NOMINAL_360: Final[frozenset[SerieSuameca]] = frozenset(
    {SerieSuameca.IBR_ON, SerieSuameca.IBR_1M, SerieSuameca.IBR_3M}
)


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class ErrorFuenteDatos(RuntimeError):
    """Base de todos los fallos de obtención de datos."""


class FuenteNoDisponibleError(ErrorFuenteDatos):
    """El endpoint no respondió, respondió un error HTTP o devolvió vacío."""


class EsquemaInesperadoError(ErrorFuenteDatos):
    """El endpoint respondió, pero con una estructura que el parser no reconoce.

    Señal típica de que la API no documentada cambió. Se levanta a propósito en vez de
    intentar adivinar el nuevo formato.
    """


class FuenteManualAusenteError(ErrorFuenteDatos):
    """Falta el archivo que debe aportarse a mano (curva cero cupón TES)."""


# ---------------------------------------------------------------------------
# Infraestructura común: HTTP + manifest de procedencia
# ---------------------------------------------------------------------------


def _get_json(url: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
    """Ejecuta un GET y devuelve ``(json_parseado, url_efectiva)``.

    Args:
        url: URL absoluta del endpoint.
        params: Parámetros de query.

    Returns:
        Tupla con el JSON ya parseado y la URL final (con query), para el manifest.

    Raises:
        FuenteNoDisponibleError: Ante fallo de red, timeout o status != 200.
        EsquemaInesperadoError: Si el cuerpo no es JSON válido.
    """
    try:
        respuesta = requests.get(url, params=params, timeout=config.TIMEOUT_HTTP)
    except requests.RequestException as exc:
        raise FuenteNoDisponibleError(f"Fallo de red consultando {url}: {exc}") from exc

    if respuesta.status_code != 200:
        raise FuenteNoDisponibleError(
            f"{url} respondió HTTP {respuesta.status_code} "
            f"(primeros 200 caracteres: {respuesta.text[:200]!r})"
        )

    try:
        return respuesta.json(), respuesta.url
    except ValueError as exc:
        raise EsquemaInesperadoError(
            f"{respuesta.url} no devolvió JSON. Content-Type="
            f"{respuesta.headers.get('Content-Type')!r}. "
            f"Primeros 200 caracteres: {respuesta.text[:200]!r}"
        ) from exc


def leer_manifest() -> dict[str, Any]:
    """Devuelve el manifest de procedencia, o un esqueleto vacío si aún no existe."""
    if not config.RUTA_MANIFEST.exists():
        return {"fuentes": {}}
    return json.loads(config.RUTA_MANIFEST.read_text(encoding="utf-8"))


def _registrar_procedencia(
    clave: str,
    url: str,
    contenido: bytes,
    filas: int,
    origen: Origen,
    archivo: Path | None = None,
) -> None:
    """Guarda el snapshot crudo y anota su procedencia en ``data/manifest.json``.

    Args:
        clave: Identificador estable de la fuente (p. ej. ``"suameca_serie_1"``).
        url: URL exacta consultada, o la página de origen si el dato es manual.
        contenido: Bytes exactos recibidos, sobre los que se calcula el SHA256.
        filas: Número de observaciones parseadas.
        origen: ``"api"`` si se descargó automáticamente, ``"manual_export"`` si lo
            aportó una persona.
        archivo: Ruta del snapshot. Si es ``None`` se escribe ``data/raw/<clave>.json``.
    """
    config.DIR_DATOS_CRUDOS.mkdir(parents=True, exist_ok=True)
    if archivo is None:
        archivo = config.DIR_DATOS_CRUDOS / f"{clave}.json"
        archivo.write_bytes(contenido)

    manifest = leer_manifest()
    manifest["fuentes"][clave] = {
        "url": url,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(contenido).hexdigest(),
        "filas": filas,
        "origen": origen,
        "archivo": str(archivo.relative_to(config.RAIZ_PROYECTO)),
    }
    config.RUTA_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    config.RUTA_MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# SUAMECA — Banco de la República
# ---------------------------------------------------------------------------


def fetch_serie_suameca(
    id_serie: SerieSuameca | int,
    cant_datos: int = 5000,
    registrar: bool = True,
) -> pd.DataFrame:
    """Descarga una serie de tiempo del portal de estadísticas del Banco de la República.

    El endpoint devuelve una lista con un único objeto que trae metadata de la serie
    más un array ``data`` de pares ``[epoch_milisegundos, valor]``.

    Args:
        id_serie: Id **de serie** del backend (no el id de página que aparece en la URL
            del portal). Ver :class:`~motor_tes.config.SerieSuameca`.
        cant_datos: Número máximo de observaciones, contando desde la más reciente.
        registrar: Si es ``True``, guarda snapshot y anota procedencia en el manifest.

    Returns:
        ``DataFrame`` indexado por fecha (``DatetimeIndex`` ascendente) con una columna
        ``valor`` (float, en las unidades originales de la serie) y los atributos de
        metadata en ``df.attrs`` (``nombre``, ``unidad``, ``periodicidad``, ``fuente``).

    Raises:
        FuenteNoDisponibleError: Si el endpoint falla o la serie viene vacía.
        EsquemaInesperadoError: Si la respuesta no trae el array ``data`` esperado.
    """
    id_int = int(id_serie)
    params = {
        "idSerie": id_int,
        "tipoDato": config.TIPO_DATO_SERIE,
        "cantDatos": cant_datos,
    }
    payload, url_efectiva = _get_json(config.SUAMECA_SERIE_X_TIPO_DATO, params)

    if not isinstance(payload, list) or not payload:
        raise FuenteNoDisponibleError(
            f"La serie {id_int} devolvió una respuesta vacía ({payload!r}). "
            "Los ids que aparecen en la URL del portal son ids de página, no de serie: "
            "consultá motor_tes.config.SerieSuameca para los ids válidos."
        )

    registro = payload[0]
    if not isinstance(registro, dict) or "data" not in registro:
        claves = sorted(registro) if isinstance(registro, dict) else type(registro)
        raise EsquemaInesperadoError(
            f"La respuesta de la serie {id_int} no trae la clave 'data'. "
            f"Claves recibidas: {claves}. "
            "La API de SUAMECA no está documentada y puede haber cambiado."
        )

    observaciones = registro["data"]
    if not observaciones:
        raise FuenteNoDisponibleError(f"La serie {id_int} no trae observaciones.")

    try:
        marcas = [int(par[0]) for par in observaciones]
        valores = [None if par[1] is None else float(par[1]) for par in observaciones]
    except (TypeError, ValueError, IndexError) as exc:
        raise EsquemaInesperadoError(
            f"No pude parsear 'data' de la serie {id_int} como pares "
            f"[epoch_ms, valor]. Primer elemento: {observaciones[0]!r}"
        ) from exc

    # Los epoch de SUAMECA codifican medianoche hora de Bogotá (UTC-5, sin horario de
    # verano). Convertir a America/Bogota antes de descartar la zona deja el índice en
    # 00:00 local; parsear como UTC a secas lo dejaría desplazado 5 horas.
    indice = (
        pd.to_datetime(pd.Series(marcas), unit="ms", utc=True)
        .dt.tz_convert("America/Bogota")
        .dt.tz_localize(None)
    )
    df = pd.DataFrame({"valor": valores})
    df.index = pd.DatetimeIndex(indice, name="fecha")
    df = df.sort_index()
    df.attrs.update(
        id_serie=id_int,
        nombre=registro.get("nombre"),
        unidad=registro.get("unidad"),
        periodicidad=registro.get("descripcionPeriodicidad"),
        fuente=registro.get("fuente"),
        url=url_efectiva,
    )

    if registrar:
        _registrar_procedencia(
            clave=f"suameca_serie_{id_int}",
            url=url_efectiva,
            contenido=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            filas=len(df),
            origen="api",
        )
    return df


# ---------------------------------------------------------------------------
# Socrata (datos.gov.co) — fuente secundaria de TRM
# ---------------------------------------------------------------------------


def fetch_trm_socrata(
    desde: date | None = None,
    limite: int = 50_000,
    registrar: bool = True,
) -> pd.DataFrame:
    """Descarga la TRM histórica desde datos.gov.co vía protocolo Socrata (SODA).

    Sirve de **control cruzado independiente** contra la TRM que publica SUAMECA:
    ambas deben coincidir, y el reporte de validación compara las dos.

    El dataset entrega ``valor`` como texto y las fechas de vigencia en ISO-8601 sin
    zona horaria (``"2026-08-01T00:00:00.000"``). Una misma TRM puede estar vigente
    varios días (fines de semana y festivos), por eso hay ``vigenciadesde`` y
    ``vigenciahasta``; este loader indexa por ``vigenciadesde``.

    Args:
        desde: Si se indica, filtra observaciones con ``vigenciadesde >= desde``.
        limite: Tope de filas a solicitar.
        registrar: Si es ``True``, guarda snapshot y anota procedencia.

    Returns:
        ``DataFrame`` indexado por ``vigenciadesde`` con columnas ``valor`` (float) y
        ``vigenciahasta`` (datetime).

    Raises:
        FuenteNoDisponibleError: Si el endpoint falla o no devuelve filas.
        EsquemaInesperadoError: Si faltan las columnas esperadas.
    """
    url = f"https://{config.SOCRATA_DOMINIO}/resource/{config.SOCRATA_DATASET_TRM}.json"
    params: dict[str, Any] = {"$limit": limite, "$order": "vigenciadesde ASC"}
    if desde is not None:
        params["$where"] = f"vigenciadesde >= '{desde.isoformat()}T00:00:00.000'"

    payload, url_efectiva = _get_json(url, params)
    if not isinstance(payload, list) or not payload:
        raise FuenteNoDisponibleError(
            f"El dataset Socrata {config.SOCRATA_DATASET_TRM} no devolvió filas."
        )

    columnas_faltantes = {"valor", "vigenciadesde"} - set(payload[0])
    if columnas_faltantes:
        raise EsquemaInesperadoError(
            f"Al dataset Socrata {config.SOCRATA_DATASET_TRM} le faltan columnas "
            f"{sorted(columnas_faltantes)}. Columnas recibidas: {sorted(payload[0])}"
        )

    df = pd.DataFrame(payload)
    df["valor"] = df["valor"].astype(float)
    df["vigenciadesde"] = pd.to_datetime(df["vigenciadesde"])
    if "vigenciahasta" in df.columns:
        df["vigenciahasta"] = pd.to_datetime(df["vigenciahasta"])
    df = df.set_index("vigenciadesde").sort_index()
    df.attrs.update(dataset=config.SOCRATA_DATASET_TRM, url=url_efectiva)

    if registrar:
        _registrar_procedencia(
            clave=f"socrata_trm_{config.SOCRATA_DATASET_TRM}",
            url=url_efectiva,
            contenido=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            filas=len(df),
            origen="api",
        )
    return df


# ---------------------------------------------------------------------------
# NY Fed — SOFR (pata USD)
# ---------------------------------------------------------------------------


def fetch_sofr(n: int = 30, registrar: bool = True) -> pd.DataFrame:
    """Descarga las últimas ``n`` publicaciones de SOFR de la Fed de Nueva York.

    SOFR es una tasa overnight garantizada, cotizada en base **Actual/360**. El
    enunciado original asumía que había que ingresarla a mano; existe fuente pública
    y sin llave, así que el motor la trae automáticamente y deja el input manual como
    override explícito.

    Args:
        n: Número de publicaciones más recientes.
        registrar: Si es ``True``, guarda snapshot y anota procedencia.

    Returns:
        ``DataFrame`` indexado por ``effectiveDate`` con columna ``tasa`` en forma
        **decimal** (0.0365 para 3.65%).

    Raises:
        FuenteNoDisponibleError: Si el endpoint falla o no devuelve tasas.
        EsquemaInesperadoError: Si falta ``refRates`` o los campos esperados.
    """
    url = config.NYFED_SOFR_ULTIMOS.format(n=n)
    payload, url_efectiva = _get_json(url)

    if not isinstance(payload, dict) or "refRates" not in payload:
        claves = sorted(payload) if isinstance(payload, dict) else type(payload)
        raise EsquemaInesperadoError(
            f"La respuesta de {url} no trae 'refRates'. Claves: {claves}"
        )
    registros = payload["refRates"]
    if not registros:
        raise FuenteNoDisponibleError(f"{url} devolvió 'refRates' vacío.")

    try:
        df = pd.DataFrame(
            {
                "tasa": [float(r["percentRate"]) / 100.0 for r in registros],
                "fecha": [pd.Timestamp(r["effectiveDate"]) for r in registros],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EsquemaInesperadoError(
            f"No pude parsear 'refRates' de {url}. Primer registro: {registros[0]!r}"
        ) from exc

    df = df.set_index("fecha").sort_index()
    df.attrs.update(base_dia=BaseDia.ACT_360.value, url=url_efectiva)

    if registrar:
        _registrar_procedencia(
            clave="nyfed_sofr",
            url=url_efectiva,
            contenido=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            filas=len(df),
            origen="api",
        )
    return df


def sofr_vigente() -> float:
    """Devuelve el SOFR publicado más reciente, en forma decimal (0.0365 = 3.65%)."""
    return float(fetch_sofr(n=5)["tasa"].iloc[-1])


# ---------------------------------------------------------------------------
# Curva money-market COP, 100% automatizada
# ---------------------------------------------------------------------------


def _nominal_360_a_efectiva_365(tasa_nominal: float, dias: int) -> float:
    """Convierte una tasa nominal base 360 al equivalente efectivo anual base 365.

    El IBR se publica como tasa nominal sobre base 360 para su plazo. Se capitaliza el
    período y se anualiza en base 365, que es la convención de la curva TES::

        factor = 1 + i_nom * dias / 360
        i_ea   = factor ** (365 / dias) - 1

    Args:
        tasa_nominal: Tasa nominal en decimal (0.0925 para 9.25%).
        dias: Plazo de la tasa, en días.

    Returns:
        Tasa efectiva anual equivalente, en decimal, base 365.
    """
    factor_periodo = 1.0 + tasa_nominal * dias / config.DIAS_POR_ANIO[BaseDia.ACT_360]
    return factor_periodo ** (config.DIAS_POR_ANIO[BaseDia.ACT_365] / dias) - 1.0


def construir_nodos_ibr(
    registrar: bool = True, incluir_cdt: bool = False
) -> pd.DataFrame:
    """Arma los nodos de la curva money-market COP con series descargables por API.

    Esta es la **vía automatizada** de la pata COP: se refresca a diario sin
    intervención humana, a diferencia de la curva cero cupón TES, que solo existe como
    export manual (ver :func:`cargar_curva_tes_manual`).

    Nodos por defecto: IBR overnight / 1M / 3M, tasas interbancarias limpias que sí
    describen una estructura de plazos. Cubren hasta 3 meses, que es justamente donde
    se concentra la liquidez del forward USD/COP.

    Tres nodos no alcanzan para calibrar Nelson-Siegel (4 parámetros) ni Svensson (6):
    esta curva se usa por interpolación directa para la pata COP del forward, y la
    calibración paramétrica se hace sobre los nodos TES del export manual. La
    limitación es de la fuente, no del modelo, y está documentada en el README.

    Args:
        registrar: Si es ``True``, cada serie descargada anota su procedencia.
        incluir_cdt: Si es ``True``, agrega los nodos de CDT 180 y 360 días. Ver
            :data:`PLAZOS_NODOS_CDT` para por qué están excluidos por defecto.

    Returns:
        ``DataFrame`` con columnas ``plazo_dias`` (int), ``plazo_anios`` (float, ACT/365),
        ``tasa`` (float decimal, efectiva anual base 365), ``tasa_publicada``,
        ``serie`` (nombre) y ``fecha`` (fecha de la observación usada), ordenado por plazo.

    Raises:
        FuenteNoDisponibleError: Si alguna de las series no está disponible.
    """
    plazos = dict(PLAZOS_NODOS_IBR)
    if incluir_cdt:
        plazos.update(PLAZOS_NODOS_CDT)

    filas = []
    for serie, dias in plazos.items():
        # cant_datos pequeño: solo interesa la observación vigente más reciente.
        df = fetch_serie_suameca(serie, cant_datos=10, registrar=registrar)
        serie_limpia = df["valor"].dropna()
        if serie_limpia.empty:
            raise FuenteNoDisponibleError(
                f"La serie {serie.name} (id {int(serie)}) no trae valores no nulos."
            )
        ultima_fecha = serie_limpia.index[-1]
        tasa_publicada = float(serie_limpia.iloc[-1]) / 100.0  # viene en porcentaje

        if serie in SERIES_NOMINAL_360:
            tasa_ea = _nominal_360_a_efectiva_365(tasa_publicada, dias)
        else:
            tasa_ea = tasa_publicada  # ya es efectiva anual base 365

        filas.append(
            {
                "plazo_dias": dias,
                "plazo_anios": dias / config.DIAS_POR_ANIO[BaseDia.ACT_365],
                "tasa": tasa_ea,
                "tasa_publicada": tasa_publicada,
                "serie": serie.name,
                "fecha": ultima_fecha,
            }
        )

    return pd.DataFrame(filas).sort_values("plazo_dias").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Curva cero cupón TES — fuente manual
# ---------------------------------------------------------------------------

_INSTRUCCIONES_EXPORT_MANUAL = f"""
La curva cero cupón TES NO es descargable por API.

Los ids 220002 y 640001 que aparecen en la URL del portal son ids de *página*, no de
serie: consultarlos contra la API REST devuelve []. Un escaneo de ids 1-2599 y de
bandas altas encontró 235 series, ninguna de cero cupón. Esa página es un embed de
Oracle Analytics DV y su endpoint de token responde 404.

Para aportar el dato a mano:

  1. Abrí {config.URL_PAGINA_CERO_CUPON_TES}
  2. Click derecho sobre la tabla de datos -> exportar -> Excel
  3. Guardá el archivo como:
     {{ruta}}

El motor calcula el SHA256 del archivo y lo registra en data/manifest.json con
origen "manual_export", para que el reporte de validación distinga sin ambigüedad
qué dato bajó solo y cuál lo puso una persona.
""".strip()


#: Patrón que identifica cada columna de tasas del export, por moneda.
_PATRON_MONEDA: Final[dict[str, str]] = {"pesos": "pesos", "uvr": "uvr"}


def _normalizar(texto: str) -> str:
    """Baja a minúsculas y quita tildes, para comparar encabezados sin sorpresas."""
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn").lower()


def cargar_serie_tes_manual(
    ruta: Path | None = None,
    moneda: str = "pesos",
    registrar: bool = True,
) -> pd.DataFrame:
    """Carga la serie histórica completa de tasas cero cupón TES del export manual.

    Layout real del archivo que descarga el portal (verificado 2026-08-04):

    * Hoja ``"Datos"``, más una hoja ``"Información"`` con metadata.
    * Dos filas de encabezado: nombre de la serie y unidad de medida (``%``).
    * ``col0`` es ``Fecha`` en formato ``yyyy/mm/dd``.
    * Seis columnas de tasas: TES en **pesos** a 1, 5 y 10 años, y TES en **UVR** a
      1, 5 y 10 años.
    * Al final trae filas vacías y una leyenda de descarga, que se descartan.

    Args:
        ruta: Ruta del ``.xlsx``. Por defecto
            :data:`~motor_tes.config.RUTA_XLSX_CERO_CUPON_TES`.
        moneda: ``"pesos"`` (tasa nominal) o ``"uvr"`` (tasa real).
        registrar: Si es ``True``, anota el archivo en el manifest con su SHA256.

    Returns:
        ``DataFrame`` indexado por fecha, con una columna por tenor. Los nombres de
        columna son el plazo en años (``1.0``, ``5.0``, ``10.0``) y los valores son
        tasas en decimal, efectivas anuales.

    Raises:
        FuenteManualAusenteError: Si el archivo no existe. El mensaje trae las
            instrucciones exactas de descarga.
        EsquemaInesperadoError: Si el layout no es el esperado. El mensaje lista lo
            que sí se encontró, para ajustar el parser en vez de adivinar.
        ValueError: Si ``moneda`` no es una de las soportadas.
    """
    ruta = ruta or config.RUTA_XLSX_CERO_CUPON_TES
    if not ruta.exists():
        raise FuenteManualAusenteError(_INSTRUCCIONES_EXPORT_MANUAL.format(ruta=ruta))

    if moneda not in _PATRON_MONEDA:
        raise ValueError(
            f"moneda debe ser una de {sorted(_PATRON_MONEDA)}; recibí {moneda!r}"
        )

    try:
        encabezado = pd.read_excel(
            ruta, sheet_name=config.HOJA_XLSX_CERO_CUPON_TES, header=None, nrows=1
        )
    except ValueError as exc:
        raise EsquemaInesperadoError(
            f"No encontré la hoja {config.HOJA_XLSX_CERO_CUPON_TES!r} en {ruta}. "
            f"Hojas disponibles: {pd.ExcelFile(ruta).sheet_names}"
        ) from exc

    nombres = [str(v) for v in encabezado.iloc[0].tolist()]
    patron = _PATRON_MONEDA[moneda]

    # Se localiza cada tenor por su nombre de serie, no por posición: si el portal
    # reordena o agrega columnas, el parser sigue apuntando a la serie correcta.
    columnas: dict[float, int] = {}
    for indice, nombre in enumerate(nombres):
        norm = _normalizar(nombre)
        if patron not in norm or "cero cupon" not in norm:
            continue
        for tenor in config.TENORES_TES_ANIOS:
            etiqueta = f"{int(tenor)} ano" if tenor == 1 else f"{int(tenor)} anos"
            if etiqueta in norm:
                columnas[tenor] = indice

    faltantes = set(config.TENORES_TES_ANIOS) - set(columnas)
    if faltantes:
        raise EsquemaInesperadoError(
            f"En {ruta} no encontré las columnas de TES en {moneda} para los tenores "
            f"{sorted(faltantes)}. Encabezados leídos: {nombres}"
        )

    crudo = pd.read_excel(
        ruta,
        sheet_name=config.HOJA_XLSX_CERO_CUPON_TES,
        header=None,
        skiprows=config.FILAS_ENCABEZADO_XLSX_TES,
    )

    fechas = pd.to_datetime(crudo.iloc[:, 0], format="%Y/%m/%d", errors="coerce")
    tasas = {
        tenor: pd.to_numeric(crudo.iloc[:, indice], errors="coerce") / 100.0
        for tenor, indice in sorted(columnas.items())
    }

    df = pd.DataFrame(tasas)
    df.index = pd.DatetimeIndex(fechas, name="fecha")
    # Descarta el pie de página y cualquier fila sin fecha válida o sin dato alguno.
    df = df[df.index.notna()].dropna(how="all").sort_index()

    if df.empty:
        raise EsquemaInesperadoError(
            f"{ruta} no produjo observaciones válidas para TES en {moneda}."
        )

    df.attrs.update(
        archivo=str(ruta),
        moneda=moneda,
        tenores=sorted(columnas),
        fuente="Banco de la República (SEN y MEC), export manual desde Oracle DV",
    )

    if registrar:
        _registrar_procedencia(
            clave=f"curva_cero_cupon_tes_{moneda}",
            url=config.URL_PAGINA_CERO_CUPON_TES,
            contenido=ruta.read_bytes(),
            filas=len(df),
            origen="manual_export",
            archivo=ruta,
        )
    return df


def cargar_curva_tes_manual(
    ruta: Path | None = None,
    moneda: str = "pesos",
    fecha: date | None = None,
    registrar: bool = True,
) -> pd.DataFrame:
    """Extrae un corte transversal de la curva cero cupón TES para una fecha.

    El export es una serie de tiempo; para calibrar hace falta la estructura de plazos
    de un día. Se toma la observación más reciente que tenga los tres tenores, salvo
    que se pida una fecha concreta.

    Args:
        ruta: Ruta del ``.xlsx``.
        moneda: ``"pesos"`` o ``"uvr"``.
        fecha: Fecha del corte. Si se omite, la última con dato completo.
        registrar: Si es ``True``, anota el archivo en el manifest.

    Returns:
        ``DataFrame`` con columnas ``plazo_anios`` (float), ``tasa`` (decimal, E.A.),
        ``fuente`` (``"TES"``) y ``fecha``, ordenado por plazo.

    Raises:
        FuenteManualAusenteError: Si falta el archivo.
        EsquemaInesperadoError: Si el layout cambió o no hay ninguna fecha completa.
        ValueError: Si la fecha pedida no existe o no tiene los tres tenores.
    """
    serie = cargar_serie_tes_manual(ruta=ruta, moneda=moneda, registrar=registrar)
    completas = serie.dropna(how="any")
    if completas.empty:
        raise EsquemaInesperadoError(
            "No hay ninguna fecha con los tres tenores de TES simultáneamente."
        )

    if fecha is None:
        corte = completas.iloc[-1]
    else:
        marca = pd.Timestamp(fecha)
        if marca not in completas.index:
            raise ValueError(
                f"{marca.date()} no tiene curva TES completa. El rango disponible va de "
                f"{completas.index.min().date()} a {completas.index.max().date()}."
            )
        corte = completas.loc[marca]

    df = pd.DataFrame(
        {
            "plazo_anios": [float(t) for t in corte.index],
            "tasa": [float(v) for v in corte.to_numpy()],
        }
    )
    df["fuente"] = "TES"
    df["fecha"] = corte.name
    df.attrs.update(serie.attrs)
    df.attrs["fecha_corte"] = corte.name
    return df.sort_values("plazo_anios").reset_index(drop=True)


def construir_nodos_curva_cop(
    ruta_tes: Path | None = None,
    fecha_tes: date | None = None,
    registrar: bool = True,
) -> pd.DataFrame:
    """Arma la curva COP completa: tramo corto de IBR más tramo largo de TES.

    Ninguna de las dos fuentes alcanza sola. El IBR llega hasta 90 días (3 nodos) y el
    export de TES sólo publica 1, 5 y 10 años (3 nodos); cada una por separado deja el
    sistema subdeterminado incluso para Nelson-Siegel de 4 parámetros. Juntas dan seis
    nodos entre 1 día y 10 años, que sí permiten calibrar Svensson.

    Supuesto que hay que tener presente: se mezcla una curva **interbancaria** (IBR) con
    una **soberana** (TES). No son el mismo riesgo de crédito. Es la construcción
    habitual en mesa (tramo corto de money market, tramo largo de bonos) pero el spread
    entre ambos tramos queda absorbido dentro de la forma de la curva en vez de
    modelarse aparte. Está declarado en el README como limitación.

    Args:
        ruta_tes: Ruta del export manual de TES.
        fecha_tes: Fecha del corte de la curva TES. Por defecto, la última completa.
        registrar: Si es ``True``, cada fuente anota su procedencia.

    Returns:
        ``DataFrame`` con ``plazo_anios``, ``tasa`` (decimal, E.A. base 365),
        ``fuente`` (``"IBR"`` o ``"TES"``) y ``fecha``, ordenado por plazo.

    Raises:
        FuenteManualAusenteError: Si falta el export de TES.
        FuenteNoDisponibleError: Si alguna serie de IBR no está disponible.
    """
    ibr = construir_nodos_ibr(registrar=registrar)[
        ["plazo_anios", "tasa", "fecha"]
    ].copy()
    ibr["fuente"] = "IBR"

    tes = cargar_curva_tes_manual(ruta=ruta_tes, fecha=fecha_tes, registrar=registrar)[
        ["plazo_anios", "tasa", "fecha", "fuente"]
    ]

    nodos = pd.concat([ibr, tes], ignore_index=True)
    return nodos.sort_values("plazo_anios").reset_index(drop=True)
