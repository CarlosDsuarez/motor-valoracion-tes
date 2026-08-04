"""Ensambla el libro con macros: toma el .xlsx y le incorpora los módulos VBA.

openpyxl no puede crear un proyecto VBA desde cero, así que este paso automatiza Excel
con xlwings: abre el libro que generó :mod:`motor_tes.export_excel`, importa los ``.bas``
y lo guarda como ``.xlsm``.

RESTRICCIÓN CONOCIDA
--------------------
Importar módulos VBA por automatización exige que Excel tenga habilitado el acceso al
modelo de objetos del proyecto VBA. Si no lo está, el script falla con un error de
permisos y **lo dice explícitamente** en vez de dejar un libro a medias: en ese caso hay
que hacer la importación a mano, que son tres pasos y está documentada abajo y en el
README.

    macOS:   Excel -> Preferencias -> Seguridad -> "Confiar en el acceso al modelo de
             objetos de proyectos de VBA"
    Windows: Archivo -> Opciones -> Centro de confianza -> Configuración del Centro de
             confianza -> Configuración de macros -> "Confiar en el acceso al modelo de
             objetos de proyectos de VBA"

Uso::

    python excel/build_excel.py

Requiere Microsoft Excel instalado y ``pip install ".[excel]"``.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from motor_tes.config import DIR_EXCEL  # noqa: E402
from motor_tes.export_excel import RUTA_LIBRO_BASE  # noqa: E402

#: Módulos VBA que se incorporan, en orden de dependencia: ModuloForward llama a
#: funciones públicas de ModuloCurvaNSS.
MODULOS_VBA: tuple[str, ...] = ("ModuloCurvaNSS.bas", "ModuloForward.bas")

#: Libro final, con macros.
RUTA_LIBRO_MACROS: Path = DIR_EXCEL / "motor_tes_forwards.xlsm"

#: Constante de Excel para el formato xlOpenXMLWorkbookMacroEnabled (.xlsm).
_FORMATO_XLSM = 52

_INSTRUCCIONES_MANUALES = f"""
No pude importar los módulos VBA por automatización.

Casi siempre es porque Excel no tiene habilitado el acceso al modelo de objetos del
proyecto VBA. Dos caminos:

  A) Habilitarlo y volver a correr este script
     macOS:   Excel -> Preferencias -> Seguridad -> marcar
              "Confiar en el acceso al modelo de objetos de proyectos de VBA"
     Windows: Archivo -> Opciones -> Centro de confianza -> Configuración del Centro
              de confianza -> Configuración de macros -> misma casilla

  B) Importar a mano (3 pasos, no necesita permisos especiales)
     1. Abrí {RUTA_LIBRO_BASE}
     2. Herramientas -> Macro -> Editor de Visual Basic. En el editor:
        Archivo -> Importar archivo... y elegí, en este orden:
          - {DIR_EXCEL / "vba" / "ModuloCurvaNSS.bas"}
          - {DIR_EXCEL / "vba" / "ModuloForward.bas"}
     3. Guardá como "Libro de Excel habilitado para macros (.xlsm)" en:
        {RUTA_LIBRO_MACROS}

En cualquiera de los dos casos, verificá abriendo la hoja "Validacion": la columna
resultado_vba debe reproducir valor_python.
""".strip()

_INSTRUCCIONES_PERMISO_MACOS = f"""
macOS bloqueó el control de Excel desde este proceso (error -1743).

Es un permiso del sistema operativo, distinto del acceso al proyecto VBA. Nadie puede
concederlo por vos desde la terminal: la casilla la tenés que marcar en Ajustes.

  1. Ajustes del Sistema -> Privacidad y seguridad -> Automatización
  2. Buscá la app desde la que corrés esto (Terminal, iTerm, Visual Studio Code,
     Claude, ...) y activá "Microsoft Excel" debajo de ella.
     Si no aparece en la lista, volvé a correr este script: el intento fallido hace
     que macOS registre la app y muestre el diálogo de permiso.
  3. Corré de nuevo:  python excel/build_excel.py

Si preferís no tocar permisos del sistema, el libro se arma a mano en 3 pasos:

  1. Abrí {RUTA_LIBRO_BASE}
  2. Herramientas -> Macro -> Editor de Visual Basic. En el editor:
     Archivo -> Importar archivo... y elegí, en este orden:
       - {DIR_EXCEL / "vba" / "ModuloCurvaNSS.bas"}
       - {DIR_EXCEL / "vba" / "ModuloForward.bas"}
  3. Guardá como "Libro de Excel habilitado para macros (.xlsm)" en:
     {RUTA_LIBRO_MACROS}

Verificá en la hoja "Validacion": resultado_vba debe reproducir valor_python.
""".strip()


def construir_libro_con_macros(
    ruta_base: Path | None = None,
    ruta_destino: Path | None = None,
    visible: bool = False,
) -> Path:
    """Abre el libro base, importa los módulos VBA y lo guarda como ``.xlsm``.

    Args:
        ruta_base: Libro ``.xlsx`` generado por
            :func:`motor_tes.export_excel.exportar_libro`.
        ruta_destino: Ruta del ``.xlsm`` resultante.
        visible: Si es ``True``, muestra Excel durante el proceso.

    Returns:
        Ruta del libro con macros.

    Raises:
        FileNotFoundError: Si falta el libro base o alguno de los módulos ``.bas``.
        RuntimeError: Si Excel rechaza la importación. El mensaje trae las
            instrucciones exactas para habilitar el permiso o hacerlo a mano.
    """
    ruta_base = ruta_base or RUTA_LIBRO_BASE
    ruta_destino = ruta_destino or RUTA_LIBRO_MACROS

    if not ruta_base.exists():
        raise FileNotFoundError(
            f"No existe {ruta_base}. Generalo primero con "
            "'python -m motor_tes.cli excel'."
        )

    rutas_modulos = [DIR_EXCEL / "vba" / nombre for nombre in MODULOS_VBA]
    faltantes = [str(r) for r in rutas_modulos if not r.exists()]
    if faltantes:
        raise FileNotFoundError(f"Faltan módulos VBA: {faltantes}")

    try:
        import xlwings as xw
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            'xlwings no está instalado. Corré: pip install ".[excel]"\n\n'
            + _INSTRUCCIONES_MANUALES
        ) from exc

    try:
        app = xw.App(visible=visible, add_book=False)
    except Exception as exc:
        raise RuntimeError(_diagnosticar(exc)) from exc

    try:
        try:
            libro = app.books.open(str(ruta_base))
        except Exception as exc:
            raise RuntimeError(_diagnosticar(exc)) from exc

        try:
            proyecto = libro.api.VBProject
            for ruta_modulo in rutas_modulos:
                proyecto.VBComponents.Import(str(ruta_modulo))
        except Exception as exc:
            raise RuntimeError(_diagnosticar(exc)) from exc

        if ruta_destino.exists():
            ruta_destino.unlink()
        libro.api.SaveAs(str(ruta_destino), FileFormat=_FORMATO_XLSM)
        libro.close()
    finally:
        app.quit()

    return ruta_destino


def _diagnosticar(exc: Exception) -> str:
    """Traduce el fallo de automatización a instrucciones accionables.

    Hay dos causas distintas que se confunden con facilidad y se arreglan en lugares
    diferentes del sistema:

    * **Permiso de automatización de macOS** (``-1743``): el sistema operativo no deja
      que este proceso controle Excel. Se habilita en Ajustes del Sistema.
    * **Acceso al proyecto VBA**: Excel sí se deja controlar, pero bloquea la escritura
      sobre el proyecto de macros. Se habilita dentro de Excel.
    """
    texto = str(exc)
    if "-1743" in texto or "declined permission" in texto.lower():
        return _INSTRUCCIONES_PERMISO_MACOS + f"\n\nError original: {exc}"
    return _INSTRUCCIONES_MANUALES + f"\n\nError original: {exc}"


def main() -> int:
    """Punto de entrada de línea de comandos."""
    try:
        destino = construir_libro_con_macros()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"FALLÓ: {exc}", file=sys.stderr)
        return 1

    print(f"Libro con macros generado: {destino}")
    print("Abrilo y revisá la hoja 'Validacion' para contrastar VBA contra Python.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
