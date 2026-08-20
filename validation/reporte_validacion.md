# Reporte de validación

Generado 2026-08-20T05:29:39+00:00

| Insumo | Valor | Fecha del dato |
|---|---|---|
| Curva COP (último nodo) | — | 2026-08-19 |
| TRM contado | 3,053.48 COP/USD | 2026-08-20 |
| SOFR | 3.6500% | 2026-08-18 |

Las tres fechas pueden no coincidir: cada fuente tiene su propio calendario de publicación. Se muestran por separado para que cualquier desalineación quede a la vista en vez de esconderse bajo una única fecha de reporte.

## Procedencia de los datos

| fuente | origen | filas | sha256 | descargado |
|---|---|---|---|---|
| suameca_serie_1 | api | 1 | 8e978ce19cf84bd9 | 2026-08-20T05:29:38+00:00 |
| nyfed_sofr | api | 5 | 5421636662078d68 | 2026-08-20T05:29:38+00:00 |
| suameca_serie_241 | api | 10 | d9a73cc053ef22cd | 2026-08-20T05:29:37+00:00 |
| suameca_serie_242 | api | 10 | c4f6283072908a33 | 2026-08-20T05:29:37+00:00 |
| suameca_serie_243 | api | 10 | 69564837d4b981af | 2026-08-20T05:29:38+00:00 |
| suameca_serie_239 | api | 10 | a8b8b9fae52d4772 | 2026-08-03T15:15:12+00:00 |
| suameca_serie_240 | api | 10 | 2ed43aa177fc9aac | 2026-08-03T15:15:12+00:00 |
| curva_cero_cupon_tes_pesos | manual_export | 5735 | d1180ebcec16a62e | 2026-08-20T05:29:38+00:00 |

`origen = manual_export` marca lo que aportó una persona porque no existe API que lo entregue. Todo lo demás se descargó automáticamente.

## Calibración

```
NS (4p) | RMSE=3.245 bps | max|residual|=4.385 bps | nodos=6 | gl=2 | convergió=True
```

| plazo_anios | fuente | tasa_mercado | tasa_ajustada | residual_bps |
|---|---|---|---|---|
| 0.00274 | IBR | 12.0075% | 11.9641% | -4.339 |
| 0.082192 | IBR | 12.0069% | 12.0295% | 2.255 |
| 0.246575 | IBR | 12.0980% | 12.1419% | 4.385 |
| 1.0 | TES | 12.4300% | 12.3973% | -3.274 |
| 5.0 | TES | 12.2900% | 12.3159% | 2.59 |
| 10.0 | TES | 12.2400% | 12.2238% | -1.617 |

![curva](figs/curva_vs_mercado.png)

![residuales](figs/residuales.png)

## Diagnóstico de no arbitraje

```
No arbitraje: OK | forwards negativas=0 | DF monótono=True | max|z''|=0.0125
```

![forwards](figs/forwards_instantaneas.png)

## Comparación NS vs. Svensson

| modelo | grados_libertad | rmse_bps | max_curvatura |
|---|---|---|---|
| Nelson-Siegel (4p) | 2 | 3.2451 | 0.0125 |
| Svensson (6p) | 0 | 0.0 | 0.0844 |

Con 6 nodos, Svensson queda sin grados de libertad: el RMSE nulo está garantizado por construcción y la curva pierde suavidad. Por eso el modelo por defecto es Nelson-Siegel.

## Benchmark contra QuantLib

| magnitud | detalle | propio | quantlib | desvío | estado |
|---|---|---|---|---|---|
| factor_descuento | 30d | 0.9907071368 | 0.9907071368 | 1.11e-16 | OK |
| factor_descuento | 90d | 0.9721392679 | 0.9721392679 | 1.11e-16 | OK |
| factor_descuento | 180d | 0.9445476896 | 0.9445476896 | 1.11e-16 | OK |
| factor_descuento | 360d | 0.8911435842 | 0.8911435842 | 0 | OK |
| factor_descuento | 720d | 0.7934761032 | 0.7934761032 | 0 | OK |
| factor_descuento | 1825d | 0.5594917725 | 0.5594917725 | 0 | OK |
| factor_descuento | 3650d | 0.3156087822 | 0.3156087822 | 5.55e-17 | OK |
| valor_presente | cupón 13.25%, 7a, 1x/año | 104.3267802 | 104.3265904 | 1.82e-06 | OK |
| dv01 | cupón 13.25%, 7a, 1x/año | 0.04661496323 | 0.04664098519 | 0.000558 | OK |
| duracion_macaulay | cupón 13.25%, 7a, 1x/año | 5.01744242 | 5.016593146 | 0.000169 | OK |
| forward_usdcop | 30d | 3072.775377 | 3072.775377 | 1.48e-16 | OK |
| forward_usdcop | 90d | 3112.587924 | 3112.587924 | 1.46e-16 | OK |
| forward_usdcop | 180d | 3174.802919 | 3174.802919 | 1.43e-16 | OK |
| forward_usdcop | 360d | 3305.811519 | 3305.811519 | 0 | OK |
| forward_usdcop | 720d | 3586.422958 | 3586.422958 | 0 | OK |

Los factores de descuento y los forwards coinciden a precisión de máquina. El desvío del bono proviene de que este motor ubica los flujos en fracciones de año exactas mientras QuantLib usa calendario real: los cupones de períodos bisiestos valen 366/365 y los flujos caen unos días más tarde. Son dos efectos de signo opuesto, y por eso el signo neto cambia con el plazo.

## Forwards USD/COP

| plazo_dias | forward | puntos | devaluacion_ea | dv01_cop | dv01_usd | theta_dia | brecha_conv_bps | extrapolado |
|---|---|---|---|---|---|---|---|---|
| 30 | 3,072.78 | +19.30 | 7.97% | 0.0225 | -0.0255 | 0.6457 | 6.39 | no |
| 60 | 3,092.49 | +39.01 | 8.03% | 0.0454 | -0.0512 | 0.6553 | 11.89 | no |
| 90 | 3,112.59 | +59.11 | 8.09% | 0.0684 | -0.0771 | 0.6645 | 16.48 | no |
| 180 | 3,174.80 | +121.32 | 8.22% | 0.1395 | -0.1559 | 0.6901 | 24.67 | sí |
| 270 | 3,239.37 | +185.89 | 8.32% | 0.2133 | -0.2365 | 0.7134 | 24.44 | sí |
| 360 | 3,305.81 | +252.33 | 8.38% | 0.2901 | -0.3189 | 0.7349 | 16.02 | sí |
| 540 | 3,443.33 | +389.85 | 8.46% | 0.4531 | -0.4897 | 0.7751 | -23.7 | sí |
| 720 | 3,586.42 | +532.94 | 8.50% | 0.6292 | -0.6685 | 0.8134 | -90.66 | sí |

`extrapolado = sí` marca los plazos que exceden el último nodo automatizado (90 días de IBR): ahí la tasa COP sale de proyectar la forma paramétrica, no de interpolar entre datos observados.

## Riesgo del bono de referencia

Bono cupón 13.25%, 7 años, 1 pago(s) por año, nominal 100.

- Valor presente: **104.3268**
- DV01: **0.046615** por 100 de nominal
- Duración modificada: **4.4682** años

Este bono es ilustrativo. No hay fuente pública gratuita de reference data de TES, así que cupón y vencimiento deben verificarse contra Infovalmer o la BVC antes de usarse para valorar una posición real.

## Suite de pruebas

```
============================= test session starts ==============================
collected 96 items

tests/test_benchmark_quantlib.py ..............                          [ 14%]
tests/test_curva_nss.py ..............................................   [ 62%]
tests/test_pricer_forward.py ....................................        [100%]

============================== 96 passed in 2.36s ==============================
```
