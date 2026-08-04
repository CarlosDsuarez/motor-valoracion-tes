# Motor de Valoración e Inmunización de Renta Fija Local

Curva cero cupón COP calibrada con Nelson-Siegel-Svensson sobre datos del Banco de la
República, y pricer de forwards USD/COP por paridad cubierta de tasas de interés, con
sensibilidades. El cálculo pesado corre en Python; el pricing intradía ocurre en Excel
mediante UDFs de VBA que leen los parámetros calibrados, replicando el flujo de una mesa.

**Validación externa:** los factores de descuento y los precios forward coinciden con
QuantLib a **precisión de máquina (1e-16)**. Las discrepancias que quedan en el bono
están medidas y atribuidas a una causa concreta, no toleradas por umbral.

---

## Resultados

> Los números de esta sección son una **fotografía al 2026-08-03**. Las fuentes se
> actualizan a diario, así que para ver el estado vigente hay que regenerar el reporte:
> `make validate` deja [`validation/reporte_validacion.md`](validation/reporte_validacion.md)
> con procedencia, residuales, gráficos y el benchmark recalculados.

### Curva COP al 2026-08-03

| Plazo | Tasa E.A. | Fuente |
|------:|----------:|--------|
| 1 día | 12.0087 % | IBR overnight |
| 30 días | 11.9664 % | IBR 1M |
| 90 días | 12.1323 % | IBR 3M |
| 1 año | 12.4300 % | TES cero cupón |
| 5 años | 12.2900 % | TES cero cupón |
| 10 años | 12.2400 % | TES cero cupón |

Calibración **Nelson-Siegel (4 parámetros)**: RMSE **3.60 bps**, residual máximo 5.79 bps,
2 grados de libertad, sin forwards negativas y con factores de descuento monótonos.

Sobre estos mismos 6 nodos, Svensson (6 parámetros) da RMSE 0.000 bps. **Eso no es un
mejor ajuste**: con 6 parámetros y 6 nodos hay cero grados de libertad, así que el ajuste
perfecto está garantizado por construcción y la curva resultante es 24 veces menos suave
(`max |z''|` pasa de 0.014 a 0.338). El motor marca ese caso explícitamente:

```
NSS (6p) | RMSE=0.000 bps | nodos=6 | gl=0  [INTERPOLA: 0 grados de libertad, RMSE no informativo]
```

### Contraste con QuantLib

| Magnitud | Desvío | Lectura |
|----------|-------:|---------|
| Factor de descuento (30 d – 10 a) | 1e-16 | idéntico |
| Forward USD/COP (30 d – 720 d) | 1e-16 | idéntico |
| Valor presente del bono | 4e-5 rel | atribuido, ver abajo |
| DV01 | 6e-4 rel | atribuido |
| Duración de Macaulay | 1.5e-3 rel | atribuido |

La discrepancia del bono **no es error de fórmula**. Son dos efectos de signo opuesto,
ambos por trabajar con fracciones de año exactas en vez de calendario real:

1. **Cupones mayores en años bisiestos.** Con `Actual365Fixed`, QuantLib paga
   `13.25 × 366/365 = 13.286301` en los períodos que contienen un 29 de febrero. Sube el
   valor presente.
2. **Flujos más tardíos.** Del 2026-08-03 al 2033-08-03 hay 2557 días, es decir 7.005479
   años en vez de 7.0. Baja el valor presente.

El signo neto depende del plazo: a 7 años gana el primero, a 10 años el segundo. Un bono
a 1 año no cruza ningún 29 de febrero y ahí el desvío cae a **2e-6**, lo que confirma la
atribución. `tests/test_benchmark_quantlib.py` fija ambos efectos como propiedades.

### Forwards USD/COP

Con TRM 3144.14 y SOFR 3.66 %:

| Plazo | Forward | Puntos | Devaluación E.A. | DV01 COP | Theta/día |
|------:|--------:|-------:|-----------------:|---------:|----------:|
| 30 d | 3 163.84 | +19.70 | 7.89 % | +0.0232 | +0.6591 |
| 90 d | 3 204.86 | +60.72 | 8.07 % | +0.0705 | +0.6826 |
| 180 d | 3 275.81 | +131.67 | 8.67 % | +0.1433 | +0.7496 |

Todas las griegas se calculan de forma analítica y se contrastan contra diferencias
finitas en los tests.

---

## Cómo correrlo

```bash
python3.13 -m venv .venv && ./.venv/bin/python -m pip install -e ".[benchmark,socrata,excel,dev]"
```

```bash
./.venv/bin/python -m pytest tests/ -q
```

96 pruebas, sin acceso a red salvo las marcadas.

---

## Modelo

### Curva cero cupón

$$z(t) = \beta_0 + \beta_1 f_1\!\left(\tfrac{t}{\lambda_1}\right) + \beta_2 f_2\!\left(\tfrac{t}{\lambda_1}\right) + \beta_3 f_2\!\left(\tfrac{t}{\lambda_2}\right)$$

$$f_1(x) = \frac{1 - e^{-x}}{x}, \qquad f_2(x) = f_1(x) - e^{-x}$$

`β₀` es el nivel de largo plazo, `β₁` la pendiente (`z(0) = β₀ + β₁` es la tasa
instantánea), `β₂` y `β₃` dos jorobas de curvatura ubicadas por `λ₁` y `λ₂`. Con
`β₃ = 0` colapsa a Nelson-Siegel (1987), que es la metodología con la que el propio
Banco de la República estima su curva.

En `t = 0` los factores de carga son 0/0; el motor usa la expansión de Taylor
`f₁ ≈ 1 - x/2 + x²/6`, `f₂ ≈ x/2 - x²/3` por debajo de `t/λ = 1e-6`.

Descuento: `DF(t) = (1 + z(t))^(-t)`, con `t` en años ACT/365.

**Calibración.** Mínimos cuadrados no lineales (`scipy.optimize.least_squares`) con
cotas y **multi-start** sobre una rejilla de `(λ₁, λ₂)`. El multi-start no es adorno: la
superficie objetivo tiene mínimos locales en las escalas temporales, y arrancar de un
único punto es el error clásico de esta calibración.

**Identificabilidad.** NSS está débilmente identificado: vectores de parámetros
visiblemente distintos pueden generar curvas indistinguibles. Por eso comparar dos
calibraciones se hace sobre la **curva** (`NSSParams.curva_equivalente_a`), no sobre los
parámetros. No existe simetría exacta de permutación entre `(β₂, λ₁)` y `(β₃, λ₂)`,
porque `β₁` también se apoya en `λ₁`.

### Paridad cubierta

$$F = S_0 \cdot \frac{K_{\text{COP}}(d)}{K_{\text{USD}}(d)}$$

| Pata | Factor | Convención |
|------|--------|------------|
| COP | `(1 + i)^(d/365)` | efectiva anual, ACT/365 |
| USD | `1 + i·d/360` | simple, ACT/360 (SOFR) |

El enunciado clásico usa base 360 en las dos patas. Aplicada al **mismo número** de tasa,
esa fórmula lineal da otro precio, y la brecha **no es monótona en el plazo**: son dos
efectos opuestos — dividir por 360 en vez de 365 infla la pata simple, mientras que
capitalizar compuesto acelera la efectiva anual. Medido con TRM 3144.14, SOFR 3.66 % y
COP 12.00 %:

| Plazo | Brecha |
|------:|-------:|
| 30 d | +6.4 bps |
| 180 d | +23.8 bps |
| 270 d | +23.5 bps |
| 360 d | +15.5 bps |
| 730 d | −88.2 bps |

El atajo lineal sobrevalora hasta ~24 bps y el máximo cae justo en la ventana de mayor
liquidez del forward USD/COP. Ambas convenciones están implementadas
(`ConvencionTasa.EA_365` y `SIMPLE_360`) y `comparar_convenciones()` cuantifica la brecha.

### Riesgo

DV01 por desplazamiento paralelo de la curva, con diferencia centrada de ±½ punto básico,
que cancela el error de segundo orden. La duración modificada se deriva del DV01:

`D_mod = DV01 / (VP × 1bp)`

A diferencia de `D_mac/(1+y)`, no requiere reducir la curva a una TIR única, así que es
consistente con descontar cada flujo a su propia tasa cero cupón.

---

## Fuentes de datos

Cada descarga deja registro en `data/manifest.json` con URL exacta, timestamp, SHA256,
número de filas y origen (`api` o `manual_export`). **El motor nunca degrada en silencio
a datos simulados**: si una fuente falla o cambia de esquema, levanta una excepción
tipada y el pipeline se detiene.

| Fuente | Endpoint | Estado |
|--------|----------|--------|
| SUAMECA – Banco de la República | `.../estadisticas-economicas-back/rest/estadisticaEconomicaRestService/consultaInformacionSerieXTipoDato?idSerie={id}&tipoDato=1&cantDatos={n}` | automático |
| Socrata – datos.gov.co (TRM) | `https://www.datos.gov.co/resource/mcec-87by.json` | automático |
| NY Fed (SOFR) | `https://markets.newyorkfed.org/api/rates/secured/sofr/last/{n}.json` | automático |
| Curva cero cupón TES | export manual desde el portal | **manual** |

IDs de serie verificados: TRM = 1, IBR overnight/1M/3M = 241/242/243, DTF 90d = 65,
CDT 90/180/360 = 238/239/240, tasa de política = 59.

> **La API de SUAMECA no está documentada públicamente.** Su URL base y los nombres de
> método se extrajeron del bundle Angular del portal. Puede cambiar sin aviso; el fetcher
> está construido para fallar de forma explícita si eso pasa.

### Por qué la curva TES es manual

Los ids `220002` y `640001` que aparecen en la URL del portal son ids de **página**, no de
serie: consultarlos contra la API devuelve `[]`. Un escaneo de los ids 1–2599 más varias
bandas altas encontró 235 series, ninguna de cero cupón. Esa página es un embed de Oracle
Analytics DV y su endpoint de token responde 404. La única vía es exportar la tabla a
Excel a mano. `cargar_curva_tes_manual()` levanta `FuenteManualAusenteError` con las
instrucciones exactas si el archivo no está.

El export trae 3 tenores por curva (1, 5 y 10 años) en pesos y en UVR, con historia diaria
desde 2003. Tres nodos no alcanzan para calibrar ni Nelson-Siegel: por eso se combinan con
el tramo corto de IBR.

---

## Excel y VBA

```
excel/
├── vba/ModuloCurvaNSS.bas    # curva, descuento, forwards, DV01 y duración del bono
├── vba/ModuloForward.bas     # forwards USD/COP, griegas, brecha de convenciones
└── build_excel.py            # ensambla el .xlsm importando los módulos
```

Python calibra y escribe los parámetros en el rango con nombre `NSS_PARAMS`; las UDFs los
leen. **El VBA es autosuficiente**: no llama a Python, así que el libro funciona en
cualquier máquina con Excel.

| UDF | Qué devuelve |
|-----|--------------|
| `TASA_CERO_CUPON(t; NSS_PARAMS)` | tasa cero cupón a `t` años |
| `FACTOR_DESCUENTO(t; NSS_PARAMS)` | `DF(t)` |
| `TASA_FORWARD(t1; t2; NSS_PARAMS)` | forward implícita entre `t1` y `t2` |
| `VP_TES` / `DV01_TES` / `DURACION_MOD_TES` | valoración y riesgo de un TES |
| `FORWARD_USDCOP(spot; días; SOFR)` | precio forward |
| `PUNTOS_FORWARD` / `DEVALUACION_IMPLICITA` | cotización de mesa |
| `DELTA_SPOT_FWD` / `DV01_COP_FWD` / `DV01_USD_FWD` / `THETA_DIA_FWD` | griegas |
| `BRECHA_CONVENCIONES_BPS` | diferencia entre convenciones |

La hoja `Validacion` trae, lado a lado, el valor calculado en Python y la fórmula VBA que
debe reproducirlo, con la diferencia en una tercera columna. Es el control de paridad
entre ambas implementaciones.

### Armar el `.xlsm`

```bash
./.venv/bin/python excel/build_excel.py
```

openpyxl no puede crear un proyecto VBA desde cero, así que este paso automatiza Excel.
Requiere dos permisos que **son del usuario, no del script**:

- **macOS, automatización:** Ajustes del Sistema → Privacidad y seguridad → Automatización
  → habilitar *Microsoft Excel* para la app desde la que se corre. Sin esto el script
  falla con `-1743`.
- **Excel, proyecto VBA:** Excel → Preferencias → Seguridad → *Confiar en el acceso al
  modelo de objetos de proyectos de VBA*.

Si preferís no tocar permisos, la importación manual son tres pasos y el script los
imprime. En ambos casos el resultado se verifica igual: abrir la hoja `Validacion` y
comprobar que `resultado_vba` reproduce `valor_python`.

---

## Estructura

```
src/motor_tes/
├── config.py              # endpoints, ids verificados, convenciones, rutas
├── data_fetch.py          # conectores + registro de procedencia
├── curva_nss.py           # calibración, curva, duración y DV01
├── pricer_forward.py      # paridad cubierta y griegas
├── benchmark_quantlib.py  # contraste contra QuantLib
└── export_excel.py        # puente a Excel
tests/                     # 96 pruebas
data/manifest.json         # procedencia con SHA256 por fuente
excel/                     # UDFs de VBA y ensamblador del libro
```

---

## Supuestos y limitaciones

Declarados a propósito, porque afectan la interpretación de los números:

1. **Se mezcla curva interbancaria con soberana.** El tramo corto es IBR y el largo TES.
   Es la construcción habitual de mesa, pero no son el mismo riesgo de crédito: el spread
   entre tramos queda absorbido dentro de la forma de la curva en vez de modelarse aparte.
2. **Circularidad parcial en la calibración.** El Banco de la República ya publica sus
   nodos de TES ajustados con Nelson-Siegel. Recalibrar sobre ellos no prueba gran cosa,
   así que **la validación real de la calibración es la recuperación de parámetros
   sintéticos**: se genera una curva con parámetros conocidos, se calibra sobre esos
   puntos y se verifica que se recupera la curva original (desvío máximo 1e-6 bps).
3. **Sin calendario de días hábiles.** Los flujos se ubican en fracciones de año exactas.
   Es el origen medido de la discrepancia contra QuantLib y la extensión natural hacia
   producción.
4. **Los CDT se excluyen de los nodos.** El CDT a 180 días osciló entre 9.98 % y 10.65 %
   en una semana mientras el de 360 días iba de 11.74 % a 12.41 %: son promedios
   ponderados por monto emitido, reflejan qué bancos captaron cada día, no una estructura
   de plazos. Incluirlos daba RMSE de 59 bps. Quedan disponibles con
   `construir_nodos_ibr(incluir_cdt=True)`.
5. **Extrapolación señalada.** Con los nodos actuales el tramo automatizado llega a 90
   días; `pricer_forward(..., plazo_max_curva_dias=90)` marca el resultado como
   `extrapolado` cuando el contrato excede el último nodo observado.
6. **El bono de referencia no está verificado.** No hay fuente pública gratuita de
   reference data de TES (cupón, vencimiento, ISIN). Los ejemplos del repo son
   ilustrativos y deben contrastarse contra Infovalmer o la BVC antes de usarse.
7. **Frecuencia de actualización.** IBR, TRM y SOFR son diarios. La curva TES depende de
   que alguien vuelva a exportar el Excel del portal.
