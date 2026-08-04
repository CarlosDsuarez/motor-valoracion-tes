"""Motor de valoración e inmunización de renta fija local.

Dos módulos integrados:

* :mod:`motor_tes.curva_nss` — calibración Nelson-Siegel-Svensson de la curva cero
  cupón en pesos, y las funciones de descuento, forward, duración y DV01 que se
  derivan de ella.
* :mod:`motor_tes.pricer_forward` — pricing de forwards USD/COP por paridad cubierta
  de tasas de interés, con sus sensibilidades.

Los datos entran por :mod:`motor_tes.data_fetch`, que registra la procedencia de cada
fuente en ``data/manifest.json``. Las convenciones de conteo de días y los ids de serie
verificados viven en :mod:`motor_tes.config`.
"""

from __future__ import annotations

__version__ = "0.1.0"
