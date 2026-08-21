# Motor de Valoración e Inmunización de Renta Fija Local

**Curva cero cupón COP (Nelson-Siegel-Svensson) y pricer de forwards USD/COP por paridad cubierta**

> **Proyecto de investigación aplicada**
> Universidad Icesi — Finance & Investment Club
> Autor: Carlos Suárez

Implementación de un motor de valoración de renta fija en pesos colombianos y de
derivados de tasa de cambio. El objetivo de la investigación es doble: **reproducir con
rigor la metodología que una mesa de mercado de capitales usa a diario**, y **cuantificar
el costo de los atajos** que suelen tomarse al implementarla (convenciones de conteo de
días, número de nodos, extrapolación).

El motor mantiene **dos curvas COP** y cada una trabaja en su dominio. La de **fondeo**
sale de datos públicos —IBR del Banco de la República y sus nodos de TES cero cupón— y
alimenta la paridad cubierta. La **soberana** se calibra contra precios de mercado de doce
instrumentos y descuenta bonos. Separarlas no es redundancia: son riesgos de crédito
distintos y cubren plazos distintos, y confundirlas es uno de los atajos que este trabajo
mide.

Las cotizaciones vienen de un proveedor comercial y **no se versionan**; sí van al
repositorio las fichas de los instrumentos, los parámetros calibrados y los agregados de
ajuste. Todo lo demás —IBR, TRM, SOFR, curva cero cupón publicada— es público y se
descarga solo. Ver [Qué se versiona y qué no](#qué-se-versiona-y-qué-no).

Validación externa en dos frentes. Contra **QuantLib**, los factores de descuento y los
precios forward coinciden a precisión de máquina (1e-16). Contra el **mercado**, las
convenciones de cotización reproducen las tasas publicadas de los quince instrumentos con
un error máximo de 0,08 bps —y de 0,02 bps en los bonos—, lo que exigió identificar el
exponente *street*/ISMA: descontar con ACT/365 transcurrido deja un sesgo sistemático de
−1,07 bps. Las discrepancias residuales están medidas y atribuidas a una causa concreta,
no toleradas bajo un umbral generoso.

---

## Tabla de contenido

1. [Marco teórico](#marco-teórico)
2. [Casos de aplicación en el sector real](#casos-de-aplicación-en-el-sector-real)
3. [Resultados](#resultados)
4. [Cómo correrlo](#cómo-correrlo)
5. [Fuentes de datos](#fuentes-de-datos)
6. [Excel y VBA](#excel-y-vba)
7. [Supuestos y limitaciones](#supuestos-y-limitaciones)
8. [Referencias](#referencias)

---

## Marco teórico

### 1. Por qué una curva cero cupón y no la TIR

La tasa interna de retorno de un bono es la tasa única que iguala el valor presente de
sus flujos a su precio. Es una medida de rentabilidad, **no una tasa de descuento válida
entre instrumentos**: dos bonos con el mismo vencimiento pero distinto cupón tienen TIR
distinta, porque el peso relativo de los flujos intermedios cambia. Es el llamado *coupon
effect*. Descontar con la TIR supone además reinversión de cada cupón a esa misma tasa,
supuesto que el mercado no ofrece.

La primitiva correcta es la **curva cero cupón** `z(t)`: la tasa a la que hoy se descuenta
un peso único que se recibe en `t`. Cada flujo se descuenta a su propio plazo:

$$VP = \sum_{i} C_i \cdot DF(t_i), \qquad DF(t) = (1 + z(t))^{-t}$$

Con esa curva, dos bonos distintos con el mismo riesgo de crédito se valoran de forma
consistente, y cualquier diferencia de precio es información, no artefacto.

### 2. Bootstrapping frente a modelos paramétricos

Hay dos familias para estimar `z(t)`:

- **Bootstrapping.** Se despeja la curva secuencialmente a partir de instrumentos
  líquidos. Reproduce los precios de mercado exactamente, pero exige un conjunto denso de
  instrumentos y hereda todo el ruido de cotización, que suele producir curvas
  forward erráticas.
- **Modelos paramétricos.** Se impone una forma funcional con pocos parámetros y se ajusta
  por mínimos cuadrados. Se pierde el ajuste exacto y se gana suavidad, robustez al ruido y
  capacidad de interpolar y extrapolar.

El mercado colombiano de deuda pública no tiene la densidad de instrumentos líquidos que
justificaría un bootstrap fino en todos los plazos. Por eso la vía paramétrica es la
adecuada, y es también la que usa el propio Banco de la República para publicar su curva.

### 3. Nelson-Siegel: de dónde sale la fórmula

Nelson y Siegel (1987) no propusieron una curva arbitraria. Partieron de modelar la **tasa
forward instantánea** como la solución de una ecuación diferencial lineal de segundo orden
con raíces iguales, que produce:

$$f(t) = \beta_0 + \beta_1 e^{-t/\lambda} + \beta_2 \frac{t}{\lambda} e^{-t/\lambda}$$

La tasa spot es el promedio de las forwards instantáneas hasta `t`. Al integrar aparece la
forma que implementa este motor:

$$z(t) = \beta_0 + \beta_1 \underbrace{\frac{1 - e^{-t/\lambda}}{t/\lambda}}_{\text{pendiente}} + \beta_2 \underbrace{\left(\frac{1 - e^{-t/\lambda}}{t/\lambda} - e^{-t/\lambda}\right)}_{\text{curvatura}}$$

La interpretación de los parámetros es económica, no estadística:

| Parámetro | Papel | Comportamiento |
|---|---|---|
| `β₀` | **Nivel** | Asíntota de largo plazo: `z(∞) = β₀` |
| `β₁` | **Pendiente** | `z(0) = β₀ + β₁` es la tasa instantánea; `−β₁` es el spread largo-corto |
| `β₂` | **Curvatura** | Joroba de mediano plazo; su carga es nula en `t = 0` y en `t → ∞` |
| `λ` | **Escala temporal** | Dónde se ubica el máximo de la joroba |

Esto conecta con un hecho estilizado robusto: Litterman y Scheinkman (1991) mostraron que
los tres primeros componentes principales de los cambios de la curva explican la gran
mayoría de su variación y se interpretan justamente como nivel, pendiente y curvatura.
Nelson-Siegel es una parametrización con esa misma estructura, impuesta por construcción
en lugar de extraída de los datos.

**Detalle numérico.** En `t = 0` los factores de carga son indeterminaciones `0/0`. El
motor usa la expansión de Taylor `f₁ ≈ 1 − x/2 + x²/6` y `f₂ ≈ x/2 − x²/3` por debajo de
`t/λ = 1e-6`, de modo que `z(0) = β₀ + β₁` se obtiene de forma exacta y estable.

### 4. La extensión de Svensson

Svensson (1994) agrega un segundo término de curvatura con su propia escala temporal, para
capturar curvas con doble joroba:

$$z(t) = \beta_0 + \beta_1 f_1\!\left(\tfrac{t}{\lambda_1}\right) + \beta_2 f_2\!\left(\tfrac{t}{\lambda_1}\right) + \beta_3 f_2\!\left(\tfrac{t}{\lambda_2}\right)$$

Más flexibilidad no es gratis, y cuánto cuesta depende de cuántas observaciones haya. Con
seis parámetros y seis nodos el sistema tiene **cero grados de libertad**: el ajuste perfecto
está garantizado por construcción y el RMSE deja de ser evidencia de calidad. Por eso la
curva de fondeo, que se arma con seis nodos publicados, usa Nelson-Siegel de cuatro
parámetros.

La curva soberana se calibra contra doce instrumentos, así que le sobran seis grados de
libertad y ahí Svensson sí se justifica. Y no solo ajusta mejor: produce una curva **más
suave** que Nelson-Siegel sobre los mismos datos, de modo que no se está comprando ajuste a
costa de forma. El mismo modelo es la elección equivocada en un caso y la correcta en el
otro; lo que cambia es la cantidad de información disponible (ver
[Resultados](#resultados)).

### 5. Calibración: por qué el multi-start no es opcional

La curva se calibra minimizando la suma de residuales al cuadrado contra los nodos
observados. El problema es **no lineal en `λ`** y su superficie objetivo tiene mínimos
locales: arrancar de un único punto inicial produce ajustes que parecen razonables pero no
son óptimos, y es el error clásico de esta implementación. El motor recorre una rejilla de
valores iniciales de `(λ₁, λ₂)` y conserva el arranque con menor suma de residuales.

Un problema relacionado es la **identificabilidad**: vectores de parámetros visiblemente
distintos pueden generar curvas indistinguibles a nivel de mercado. Por eso comparar dos
calibraciones se hace sobre la curva, no sobre los parámetros.

### 6. No arbitraje y tasas forward

Del principio de no arbitraje entre invertir directo a `t₂` o invertir a `t₁` y renovar,
sale la tasa forward implícita:

$$(1 + f_{t_1,t_2})^{t_2 - t_1} = \frac{DF(t_1)}{DF(t_2)}$$

El límite cuando `t₂ → t₁` es la tasa forward instantánea. Es un **diagnóstico**: si la
curva calibrada implica forwards instantáneas negativas de forma sostenida, o factores de
descuento no monótonos, hay un problema de ajuste, no una oportunidad de mercado. El motor
evalúa ambas condiciones sobre una grilla fina y las reporta.

### 7. Duración, DV01 e inmunización

La duración de Macaulay (Macaulay, 1938) es el plazo promedio de los flujos ponderado por
su valor presente. La **duración modificada** mide la sensibilidad relativa del precio a un
desplazamiento de tasas, y el **DV01** la sensibilidad en unidades monetarias por punto
básico.

Este motor los define por desplazamiento paralelo de la curva cero cupón, no como derivada
respecto a una TIR única:

$$DV01 = VP(z - \tfrac{1}{2}\text{bp}) - VP(z + \tfrac{1}{2}\text{bp}), \qquad D_{mod} = \frac{DV01}{VP \times 1\text{bp}}$$

La diferencia centrada cancela el error de segundo orden. La definición por curva es la
correcta cuando cada flujo se descuenta a su propia tasa, y es la que usa una mesa para
agregar riesgo entre instrumentos distintos.

Sobre esto se construye la **inmunización** (Redington, 1952): un portafolio cuyo valor
presente y duración igualan los del pasivo queda protegido, a primer orden, frente a
desplazamientos paralelos de la curva. Las limitaciones son conocidas y también teoría
estándar: la protección es local, se degrada por convexidad ante movimientos grandes, y no
cubre cambios de pendiente o curvatura, que exigen inmunización multifactorial.

### 8. Paridad cubierta de tasas de interés

Un agente que necesita dólares en `d` días tiene dos rutas: comprarlos forward, o comprar
hoy al contado y financiar la posición. Si ambas no cuestan lo mismo, existe arbitraje sin
riesgo. De ahí:

$$F = S_0 \cdot \frac{K_{COP}(d)}{K_{USD}(d)}$$

donde `K` es el factor de capitalización de cada moneda. El diferencial de tasas —no las
expectativas de devaluación— es lo que fija el precio forward.

**Las convenciones importan y no son un detalle.** La curva local se publica en tasa
efectiva anual base ACT/365; SOFR se cotiza simple base ACT/360:

| Pata | Factor de capitalización | Convención |
|---|---|---|
| COP | `(1 + i)^(d/365)` | efectiva anual, ACT/365 |
| USD | `1 + i·d/360` | simple, ACT/360 |

El enunciado clásico de la paridad aplica base 360 a ambas patas. Sobre el mismo número de
tasa, esa fórmula lineal da otro precio, y la brecha **no es monótona en el plazo**: son dos
efectos opuestos —dividir por 360 en vez de 365 infla la pata simple, mientras que
capitalizar de forma compuesta acelera la efectiva anual—. El motor implementa ambas y
cuantifica la diferencia.

**Una nota sobre la validez empírica.** La paridad cubierta se cumplía casi exactamente
antes de 2008. Desde la crisis financiera persisten desviaciones sistemáticas —el
*cross-currency basis*— documentadas por Du, Tepper y Verdelhan (2018), atribuidas a costos
de balance de los intermediarios y a fricciones regulatorias. Esto no invalida el modelo:
lo convierte en un **valor teórico de referencia**. La diferencia entre el forward de
mercado y el de paridad es precisamente la señal que una mesa monitorea.

---

### 9. Calibrar contra precios, no contra tasas publicadas

Hay dos formas de ajustar una curva paramétrica y no son equivalentes.

La primera toma nodos de una curva ya publicada y busca los parámetros que los reproducen.
Es cómoda, pero cuando el emisor de esos nodos ya los suavizó con Nelson-Siegel el ejercicio
es parcialmente circular: se ajusta un modelo a la salida de ese mismo modelo. Un RMSE bajo
mide qué tan parecidas son dos parametrizaciones, no qué tan bien describe el mercado.

La segunda descuenta los flujos de instrumentos individuales con la curva candidata y compara
contra el precio de pantalla. No hay suavizado previo de por medio: el residual es distancia
a una cotización.

**Qué se minimiza.** El residual del instrumento `i` es el error de precio dividido por su
duración:

```
r_i = (PV_modelo_i − precio_sucio_i) / D_i
```

No el error de precio crudo, y la razón es de escala. Un desvío de un punto básico en tasa
mueve el precio de un bono a 30 años unas quince veces más que el de uno a 2 años, así que
minimizar precio a secas le entrega la calibración a la parte larga. Medido sobre la muestra
de este repositorio: ponderando por precio, los instrumentos cortos quedan con errores de 130
a 190 bps; ponderando por duración, de unos 60. Dividir por la duración deja el residual en
unidades de tasa —es la aproximación de primer orden del error de yield—, de modo que el RMSE
sigue leyéndose en puntos básicos y es comparable entre las dos formas de calibrar.

**Las convenciones importan más de lo que parece.** Para que el residual signifique algo, el
precio del modelo tiene que estar construido con la misma convención con la que el mercado
cotiza. Las dos que usa este motor están verificadas contra las tasas publicadas:

- **Letras:** interés simple ACT/365, `y = (100/P − 1) · 365/días`.
- **Bonos:** compuesto anual con exponente *street* / ISMA. El cupón `j` —contando desde 0
  para el próximo— se descuenta a `j + f`, con `f = días al próximo cupón / días del período
  de cupón vigente`.

El exponente ISMA no es un detalle. Descontando con ACT/365 transcurrido, o sea `t = días/365`,
los diez bonos de la muestra quedan con un sesgo sistemático de **−1,07 bps** contra la tasa
publicada. Con el exponente ISMA el error máximo baja a **0,02 bps**. El devengo se calcula
ACT/365; se probó también ACT/ACT dentro del período y a esta precisión es indistinguible, así
que lo que discrimina es el exponente, no el devengo.

Ojo con no mezclar: la convención ISMA sirve **solo** para traducir precio ↔ tasa cotizada, que
es como habla la pantalla. El descuento contra la curva cero cupón sigue siendo
`DF(t) = (1 + z(t))^−t` con `t` en años ACT/365. Son dos cosas distintas.

---

## Casos de aplicación en el sector real

Todas las cifras salen de la corrida del motor con datos del **2026-08-19/20**
(TRM 3.053,48 · SOFR 3,65 %) y se reproducen con `make validate`.

### Caso 1 — Importador que cubre una obligación en dólares

Una empresa colombiana importa maquinaria y debe pagar **USD 1.000.000 en 180 días**. Su
riesgo es que el peso se deprecie.

| Concepto | Valor |
|---|---|
| Costo si el peso no se moviera (spot) | COP 3.053.480.000 |
| Costo asegurado con forward a 180 días (3.174,80) | **COP 3.174.802.919** |
| Costo de la cobertura (puntos forward) | COP 121.322.919 |
| Devaluación implícita | 8,22 % E.A. |

La lectura correcta no es "cubrirse cuesta 121 millones". Ese diferencial **no es una prima
de riesgo**: es el diferencial de tasas entre pesos (≈ 12,4 %) y dólares (3,65 %). La
empresa que no se cubre está tomando una posición direccional en tasa de cambio, decisión
que rara vez pertenece a su objeto social.

El motor además entrega las sensibilidades del contrato, que es lo que permite gestionarlo
una vez abierto:

| Sensibilidad | Efecto sobre USD 1.000.000 |
|---|---|
| Tasa COP `+1 bp` | +COP 139.462 |
| Tasa USD `+1 bp` | −COP 155.895 |
| Un día menos de plazo | +COP 690.100 |
| Spot `+1 COP` | +COP 1.039.733 (delta 1,0397) |

El delta mayor a 1 es informativo: cubrir un forward con una posición spot **no es una
relación uno a uno**, sino `K_COP/K_USD`. Ignorarlo deja el libro sub-cubierto.

### Caso 2 — Exportador y tesorería corporativa

El exportador enfrenta el problema espejo: vende dólares forward y **recibe** los puntos, o
sea que el diferencial de tasas juega a su favor.

Para una tesorería, la paridad cubierta responde una pregunta concreta: **¿conviene
endeudarse en pesos o emitir en dólares y cubrir?** Ambas rutas deben costar lo mismo si se
cumple la paridad. Cuando no, la diferencia es el *basis*, y ahí hay una decisión de
financiamiento con valor medible, no una corazonada.

### Caso 3 — Inmunización de un pasivo (fondo de pensiones, aseguradora)

Una aseguradora con un pasivo de duración conocida arma un portafolio de TES que iguale
valor presente y duración. Sobre el bono de referencia del proyecto:

| Métrica | Valor |
|---|---|
| Valor presente (por 100 de nominal) | 104,33 |
| Duración de Macaulay | 5,02 años |
| Duración modificada | **4,47 años** |
| DV01 | 0,046615 por 100 de nominal |

Escalado a un portafolio de **COP 10.000 millones**, el DV01 es **COP 4.661.496 por punto
básico**. Ese número es el que se contrasta contra los límites de riesgo de la tesorería, y
el que permite dimensionar una cobertura.

### Caso 4 — Mesa de distribución y gestión del libro

Una mesa que cotiza forwards a clientes corporativos necesita tres cosas que este motor
entrega: **precio teórico** para fijar el margen, **sensibilidades agregables** para
consolidar el riesgo del libro, y **velocidad**. De ahí la arquitectura: Python calibra en
batch contra las fuentes oficiales, exporta los parámetros, y las UDFs de VBA valoran
dentro de Excel al instante, sin que el operador salga de su hoja de cálculo.

### Caso 5 — Valoración a precios de mercado y cumplimiento

Las entidades vigiladas por la Superintendencia Financiera deben valorar sus posiciones a
precios de mercado con periodicidad diaria. Eso exige una curva reproducible y auditable:
el motor deja registro en `data/manifest.json` de **URL exacta, timestamp, SHA256, número
de filas, origen y licencia** de cada insumo, y distingue explícitamente lo que se descargó
automáticamente de lo que aportó una persona. De las fuentes cuya licencia impide
redistribuirlas registra el hash y las filas pero no la ruta: la procedencia queda
auditable sin publicar el dato. Si una fuente falla, el pipeline se detiene en lugar de
producir números que parecen reales.

### Caso 6 — Detección de desalineaciones

Con el forward teórico calculado, la diferencia contra el forward negociado es una señal
medible: puede reflejar el *cross-currency basis*, condiciones de fondeo en dólares del
sistema local, o una oportunidad. Separar "el mercado está caro" de "mi modelo tiene mal la
convención" exige exactamente el tipo de validación que este proyecto documenta.

### Caso 7 — El costo de los atajos

Resultado de investigación con implicación operativa directa. Aplicar la fórmula lineal de
paridad (base 360 en ambas patas) a una tasa que se publica en efectiva anual base 365
introduce este error:

| Plazo | Brecha |
|---|---|
| 30 días | +6,4 bps |
| 90 días | +16,5 bps |
| **180 días** | **+24,7 bps** |
| 270 días | +24,4 bps |
| 360 días | +16,0 bps |
| 540 días | −23,7 bps |
| 720 días | −90,7 bps |

El atajo **sobrevalora** el forward hasta unos 25 bps y su máximo cae justo en la ventana
de mayor liquidez del USD/COP; más allá del año cambia de signo y **subvalora**. Sobre un
nocional de USD 10 millones a 180 días, 24,7 bps son del orden de **COP 78 millones**. No es
redondeo.

---

## Resultados

> Cifras al **2026-08-19/20**. Las fuentes se actualizan a diario: para el estado vigente,
> `make validate` regenera [`validation/reporte_validacion.md`](validation/reporte_validacion.md)
> con procedencia, residuales, gráficos y benchmark recalculados.

### Curva de fondeo (IBR + nodos del Banco de la República)

| Plazo | Tasa E.A. observada | Ajustada | Residual | Fuente |
|---:|---:|---:|---:|---|
| 1 día | 12,0075 % | 11,9641 % | −4,34 bps | IBR overnight |
| 30 días | 12,0069 % | 12,0295 % | +2,25 bps | IBR 1M |
| 90 días | 12,0980 % | 12,1419 % | +4,39 bps | IBR 3M |
| 1 año | 12,4300 % | 12,3973 % | −3,27 bps | TES cero cupón |
| 5 años | 12,2900 % | 12,3159 % | +2,59 bps | TES cero cupón |
| 10 años | 12,2400 % | 12,2238 % | −1,62 bps | TES cero cupón |

**Nelson-Siegel (4 parámetros):** RMSE **3,25 bps**, residual máximo 4,39 bps, 2 grados de
libertad. Sin forwards instantáneas negativas y con factores de descuento monótonos.

### Curva de mercado (calibrada contra precios)

Quince instrumentos soberanos reales —cinco letras y diez bonos, de 6 días a 31,6 años—
cotizados el 2026-08-19. Se ajustan **doce**: los tres más cortos quedan fuera por la razón
que se explica más abajo.

| Modelo | Instrumentos | Grados de libertad | RMSE | Residual máx. | `max \|z''\|` |
|---|---:|---:|---:|---:|---:|
| Nelson-Siegel (4p) | 12 | 8 | 4,36 bps | 7,12 bps | 1,8367 |
| **Svensson (6p)** | 12 | **6** | **3,82 bps** | 7,22 bps | **0,5437** |

Acá Svensson sí se justifica, y por primera vez: con doce observaciones quedan seis grados de
libertad, así que el ajuste ya no está garantizado de antemano. Y a diferencia del caso de
seis nodos, ajusta mejor **y** produce una curva más suave —un tercio de la curvatura máxima
de Nelson-Siegel—, de modo que no se está comprando ajuste a costa de forma.

| Plazo | 1 a | 2 a | 3 a | 5 a | 7 a | 10 a | 15 a | 20 a | 25 a | 30 a |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Tasa E.A. | 12,050 % | 12,062 % | 12,102 % | 12,073 % | 12,027 % | 11,987 % | 11,992 % | 12,043 % | 12,111 % | 12,178 % |

Las convenciones reproducen las tasas publicadas de los quince instrumentos con un error
máximo de **0,08 bps**, y de 0,02 bps en los bonos. Sin forwards instantáneas negativas y con
factores de descuento monótonos hasta 30 años.

El RMSE de 3,82 bps se compara contra una media horquilla mediana de 3,22 bps: cinco de los
doce instrumentos quedan estrictamente dentro de la horquilla de mercado. O sea que el ajuste
es del orden del costo de transacción, aunque no queda dentro de él en todos los puntos.

### Por qué el tramo corto no se ajusta

Los identificadores de las letras son etiquetas de plazo constante, pero los instrumentos
detrás derivaron: la "1M" vence en 6 días, la "3M" en 34 y la "6M" en 62. Ese tramo cotiza
entre 10,3 % y 11,0 % mientras que todo lo de 9 meses en adelante está entre 12,0 % y 12,6 %.

Es un quiebre real —el segmento de dinero no se conecta de forma suave con la curva de
bonos— y Nelson-Siegel-Svensson no tiene la flexibilidad para atravesarlo. Forzarlo no
degrada solo el tramo corto:

| Conjunto ajustado | n | gl | RMSE | Residual máx. |
|---|---:|---:|---:|---:|
| Los 15 instrumentos | 15 | 9 | 32,2 bps | 66,5 bps |
| Solo los 10 bonos | 10 | 4 | 2,8 bps | 6,8 bps |
| **12 (bonos + letras de 9M y 1 año)** | 12 | 6 | **3,8 bps** | **7,2 bps** |

Con los quince, el nodo de 2 años se desvía 24 bps y el ajuste deja de ser útil en toda la
curva. Con solo los diez bonos el ajuste es bueno pero nada sostiene el extremo corto y la
curva extrapola hacia 19,9 % a tres meses. Los doce son el punto de equilibrio, y además el
mejor condicionado de los tres.

Las tres letras cortas **igual se cargan y se valoran**: el reporte publica su residual, que
va de 366 a 519 bps. Es la evidencia del quiebre, no un dato descartado en silencio.

La contrapartida está declarada: **la curva de mercado no sirve por debajo de 161 días**.

### Dos curvas, cada una en su dominio

El motor mantiene las dos y no son intercambiables:

| | Curva de fondeo | Curva de mercado |
|---|---|---|
| Insumo | IBR overnight/1M/3M + nodos TES publicados | precios de 12 instrumentos soberanos |
| Cobertura | 1 día a 10 años | 161 días a 31,6 años |
| Riesgo | interbancario | soberano |
| Alimenta | forwards USD/COP, paridad cubierta | valoración de bonos, Excel, UDFs de VBA |

Separarlas es lo que resuelve la vieja limitación de mezclar una curva interbancaria con una
soberana dentro de un mismo objeto. La curva de mercado extrapolada por debajo de su
instrumento más corto se dispara —**16,27 % a 30 días contra una letra real en 10,72 %**, o
sea 554 bps— así que usarla para forwards de plazo corto sería un error grande. Y descontar
un TES a 20 años con la curva de fondeo arrastraría el spread interbancario. Cada una en lo
suyo.

**El contraste entre ambas:**

| Plazo | 1 a | 2 a | 3 a | 5 a | 7 a | 10 a | 15 a | 20 a | 25 a | 30 a |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Fondeo − mercado | +34,8 | +38,0 | +30,0 | +24,3 | +23,7 | +23,7 | +20,1 | +13,3 | +5,6 | −1,8 |

Entre 3 y 20 años la curva de fondeo queda **+22,5 bps** por encima de la de mercado, con
desviación estándar de 5,5 bps. Un desplazamiento casi paralelo a lo largo de diecisiete años
de curva no es un error de modelo: es una diferencia de nivel en el insumo. Los nodos TES del
Banco de la República son del 2026-07-31 y las cotizaciones del 2026-08-19 — **19 días de
diferencia**. Es exactamente el tipo de desalineación que el motor ahora reporta en vez de
promediar.

### Por qué no Svensson en la curva de fondeo

| Modelo | Grados de libertad | RMSE | `max \|z''\|` (suavidad) |
|---|---:|---:|---:|
| Nelson-Siegel (4p) | 2 | 3,25 bps | 0,0125 |
| Svensson (6p) | **0** | 0,00 bps | 0,0844 |

Svensson no ajusta mejor: **interpola**. Con seis nodos y seis parámetros el RMSE nulo está
garantizado de antemano, y la curva resultante es casi siete veces menos suave. El motor lo
marca explícitamente en vez de dejar que un `RMSE = 0,000` se lea como excelencia:

```
NSS (6p) | RMSE=0.000 bps | nodos=6 | gl=0  [INTERPOLA: 0 grados de libertad, RMSE no informativo]
```

Este es el argumento para preferir el modelo de menor dimensión **sobre los seis nodos
publicados**. Con los doce instrumentos de la curva de mercado el cálculo se invierte, y ahí
Svensson sí paga: ver más arriba.

### Contraste con QuantLib

| Magnitud | Desvío | Lectura |
|---|---:|---|
| Factor de descuento (30 d – 10 a) | 1e-16 | idéntico |
| Forward USD/COP (30 d – 720 d) | 1e-16 | idéntico |
| Valor presente del bono | ~1e-6 – 4e-5 rel | atribuido |
| DV01 | ~6e-4 rel | atribuido |
| Duración de Macaulay | ~1,5e-3 rel | atribuido |

La discrepancia del bono **no es error de fórmula**. Son dos efectos de signo opuesto, ambos
por trabajar con fracciones de año exactas en lugar de calendario real:

1. **Cupones mayores en años bisiestos.** Con `Actual365Fixed`, QuantLib paga
   `13,25 × 366/365 = 13,286301` en los períodos que contienen un 29 de febrero. Sube el
   valor presente.
2. **Flujos más tardíos.** Entre dos fechas separadas por siete años calendario hay 2.557
   días, o sea 7,005479 años ACT/365 en lugar de 7,0. Baja el valor presente.

El signo neto depende del plazo: a 7 años domina el primero, a 10 años el segundo. Un bono a
1 año no cruza ningún 29 de febrero y ahí el desvío cae a **2e-6**, lo que confirma la
atribución. `tests/test_benchmark_quantlib.py` fija ambos efectos como propiedades para que
no se reinterpreten después como un error.

### Forwards USD/COP

Calculados sobre la **curva de fondeo**, que es la que corresponde: la paridad cubierta se
financia a tasa interbancaria y esa curva arranca en el overnight.

| Plazo | Forward | Puntos | Devaluación E.A. | DV01 COP | Theta/día | Extrapolado |
|---:|---:|---:|---:|---:|---:|:---:|
| 30 d | 3.072,86 | +19,38 | 8,00 % | 0,0225 | 0,6484 | no |
| 90 d | 3.112,83 | +59,35 | 8,12 % | 0,0684 | 0,6672 | no |
| 180 d | 3.175,28 | +121,80 | 8,25 % | 0,1395 | 0,6928 | **sí** |
| 360 d | 3.306,77 | +253,29 | 8,42 % | 0,2902 | 0,7377 | **sí** |

La columna `extrapolado` marca los plazos que exceden el último nodo automatizado (90 días
de IBR): ahí la tasa COP proviene de proyectar la forma paramétrica, no de interpolar entre
datos observados. Es una distinción que un motor de producción debe hacer explícita.

Usar acá la curva soberana sería un error grande y medido: extrapolada por debajo de su
instrumento más corto da **16,27 % a 30 días contra una letra real en 10,72 %**, o sea 554
bps. Es la razón por la que el motor mantiene las dos curvas separadas.

Todas las griegas se calculan de forma analítica y se contrastan contra diferencias finitas
en la suite de pruebas.

---

## Cómo correrlo

```bash
make setup
```

```bash
make test
```

176 pruebas. Otros comandos: `make calibrate`, `make curvas`, `make validate`, `make excel`,
`make xlsm`, `make help`.

Las dos curvas se calibran juntas por defecto. Para trabajar con una sola:

```bash
python -m motor_tes.cli calibrate --fuente mercado
```

```bash
python -m motor_tes.cli calibrate --fuente banrep
```

`--fuente banrep` es el único que corre sin las cotizaciones licenciadas, así que es el
camino para un clon limpio del repositorio. Para ver el contraste entre ambas curvas plazo
por plazo:

```bash
python -m motor_tes.cli curvas
```

Sin la bandera `--svensson`, cada curva usa el modelo que sus datos soportan: Svensson de 6
parámetros para la de mercado, que tiene doce instrumentos, y Nelson-Siegel de 4 para la de
fondeo, que con seis nodos interpolaría.

---

## Fuentes de datos

Cada descarga deja registro en `data/manifest.json` con URL exacta, timestamp, SHA256,
número de filas y origen (`api` o `manual_export`). **El motor nunca degrada en silencio a
datos simulados**: si una fuente falla o cambia de esquema, levanta una excepción tipada y
el pipeline se detiene.

| Fuente | Contenido | Estado |
|---|---|---|
| SUAMECA — Banco de la República | TRM, IBR, DTF, CDT, tasa de política | automático |
| Socrata — datos.gov.co | TRM histórica (control cruzado) | automático |
| Reserva Federal de Nueva York | SOFR (pata USD) | automático |
| Curva cero cupón TES | 1, 5 y 10 años, pesos y UVR, desde 2003 | **manual** |
| Cotizaciones de instrumentos soberanos | 15 letras y bonos, de 6 días a 31 años | **manual, licenciado** |

IDs de serie verificados: TRM = 1, IBR overnight/1M/3M = 241/242/243, DTF 90d = 65,
CDT 90/180/360 = 238/239/240, tasa de política = 59.

> **La API de SUAMECA no está documentada públicamente.** Su URL base y los nombres de
> método se extrajeron del bundle Angular del portal. Puede cambiar sin aviso; el fetcher
> está construido para fallar de forma explícita si eso ocurre.

### Qué se versiona y qué no

Las cotizaciones de instrumentos vienen de un proveedor comercial y este repositorio es
público, así que la fuente está partida en dos según lo que la licencia permite redistribuir:

| Archivo | Contenido | ¿Se versiona? |
|---|---|---|
| `data/instrumentos_tes.csv` | RIC, cupón y vencimiento | **sí** — son hechos públicos de un emisor soberano |
| `data/privado/cotizaciones_tes.csv` | precios y tasas bid/ask por fecha | **no** — dato licenciado |

De la fuente restringida el manifest registra SHA256, cantidad de filas y licencia, pero no
la ruta: la procedencia queda verificable sin publicar el dato. En el reporte de validación
van **agregados** —RMSE, residual máximo, grados de libertad, cuántos instrumentos caen dentro
de la horquilla— y no la tabla instrumento por instrumento, porque el residual de cada uno,
combinado con los parámetros publicados de la curva, permite reconstruir el precio observado.
El detalle se escribe en `validation/privado/`, que tampoco se versiona.

Un clon del repositorio arranca sin las cotizaciones. `calibrate --fuente mercado` se detiene
ahí con `FuenteLicenciadaAusenteError` y las instrucciones para aportar el archivo; nunca
inventa datos ni cae en silencio a la otra curva. La curva de fondeo sigue funcionando sola
con `--fuente banrep`.

### Por qué la curva TES es manual

Los ids `220002` y `640001` que aparecen en la URL del portal son ids de **página**, no de
serie: consultarlos contra la API devuelve `[]`. Un escaneo de los ids 1–2599 más varias
bandas altas encontró 235 series, ninguna de cero cupón. Esa página es un embed de Oracle
Analytics DV y su endpoint de token responde 404. La única vía es exportar la tabla a Excel
a mano. `cargar_curva_tes_manual()` levanta `FuenteManualAusenteError` con las instrucciones
exactas si el archivo no está.

El export trae **tres tenores por curva** (1, 5 y 10 años) en pesos y en UVR, con historia
diaria desde 2003. Tres nodos no alcanzan para calibrar ni Nelson-Siegel, y por eso se
combinan con el tramo corto de IBR.

---

## Excel y VBA

Python calibra en batch y escribe los parámetros en el rango con nombre `NSS_PARAMS`; las
UDFs de VBA los leen y valoran. **El VBA es autosuficiente**: no llama a Python, así que el
libro funciona en cualquier máquina con Excel.

El libro lleva **dos** rangos de parámetros, uno por curva:

| Rango | Curva | Para qué |
|---|---|---|
| `NSS_PARAMS` | soberana, calibrada contra precios | descontar bonos; es la que leen las UDFs por defecto |
| `NSS_PARAMS_FONDEO` | interbancaria IBR + nodos publicados | paridad cubierta y forwards |

Las UDFs de forward aceptan el rango como argumento, así que hay que pasarles
`NSS_PARAMS_FONDEO` explícitamente: la curva soberana extrapola mal en plazos cortos y los
forwards del libro llegan a 30 días.

La hoja `Nodos` lleva **agregados** de la calibración, no el detalle por instrumento: el
`.xlsm` se versiona con las macros incorporadas, y publicar el residual de cada instrumento
junto a los parámetros permitiría reconstruir la cotización licenciada.

| UDF | Devuelve |
|---|---|
| `TASA_CERO_CUPON(t; NSS_PARAMS)` | tasa cero cupón a `t` años |
| `FACTOR_DESCUENTO(t; NSS_PARAMS)` | `DF(t)` |
| `TASA_FORWARD(t1; t2; NSS_PARAMS)` | forward implícita |
| `VP_TES` · `DV01_TES` · `DURACION_MOD_TES` | valoración y riesgo del bono |
| `FORWARD_USDCOP(spot; días; SOFR)` | precio forward |
| `PUNTOS_FORWARD` · `DEVALUACION_IMPLICITA` | cotización de mesa |
| `DELTA_SPOT_FWD` · `DV01_COP_FWD` · `DV01_USD_FWD` · `THETA_DIA_FWD` | griegas |
| `BRECHA_CONVENCIONES_BPS` | diferencia entre convenciones |

La hoja `Validacion` pone lado a lado el valor calculado en Python y la fórmula VBA que debe
reproducirlo, con la diferencia en una tercera columna. Es el control de paridad entre ambas
implementaciones. Verificado sobre la curva de mercado:

```
?TASA_CERO_CUPON(5)
 0.12073013031300431
```

idéntico dígito a dígito a `tasa_cero_cupon(5.0, params_mercado)` en Python.

### Armar el `.xlsm`

```bash
make xlsm
```

openpyxl no puede crear un proyecto VBA desde cero, así que este paso automatiza Excel.
Requiere dos permisos que **son del usuario, no del script**:

- **macOS, automatización:** Ajustes del Sistema → Privacidad y seguridad → Automatización
  → habilitar *Microsoft Excel* para la aplicación desde la que se ejecuta. Sin esto falla
  con `-1743`.
- **Excel, proyecto VBA:** Excel → Preferencias → Seguridad → *Confiar en el acceso al
  modelo de objetos de proyectos de VBA*.

Alternativa sin tocar permisos: abrir `excel/motor_tes_forwards.xlsx`, entrar al editor de
Visual Basic (`Opción+F11`), importar `excel/vba/ModuloCurvaNSS.bas` y luego
`excel/vba/ModuloForward.bas` —en ese orden, porque el segundo depende del primero— y
guardar como `.xlsm`.

---

## Estructura

```
src/motor_tes/
├── config.py              # endpoints, ids verificados, convenciones, rutas
├── data_fetch.py          # conectores + registro de procedencia
├── curva_nss.py           # curva, calibración sobre tasas, duración y DV01
├── instrumentos.py        # letras y bonos: cronograma, devengo, precio <-> TIR
├── calibracion_mercado.py # calibración contra precios y contraste de curvas
├── pricer_forward.py      # paridad cubierta y griegas
├── benchmark_quantlib.py  # contraste contra QuantLib
├── export_excel.py        # puente a Excel
└── cli.py                 # fetch / calibrate / curvas / validate / excel
tests/                     # 176 pruebas
data/manifest.json         # procedencia con SHA256 por fuente
excel/vba/                 # UDFs en VBA
validation/                # reporte autogenerado y figuras
```

---

## Supuestos y limitaciones

Declarados a propósito, porque condicionan la interpretación de los resultados.

**Resueltas por la calibración contra precios.** Se dejan escritas porque explican por qué el
motor está armado como está:

1. ~~Se mezcla curva interbancaria con soberana.~~ Ya no dentro de un mismo objeto. El motor
   mantiene dos curvas separadas: la de fondeo (IBR + nodos publicados) y la soberana
   (precios de instrumentos). Cada una alimenta lo que le corresponde. Lo que queda es una
   decisión explícita de qué curva usar para qué, no un spread absorbido en silencio dentro
   de la forma de una curva única.
2. ~~Circularidad parcial.~~ La curva soberana se calibra contra precios de pantalla, no
   contra nodos que el emisor ya suavizó con Nelson-Siegel. La recuperación de parámetros
   sintéticos sigue siendo la prueba central de la calibración, pero ahora se hace **sobre
   precios**: se generan bonos y letras valorados con una curva conocida y se verifica que
   calibrar sobre esos precios la devuelve.
3. ~~Cobertura de plazos: seis nodos con un vacío entre 90 días y 1 año.~~ La curva soberana
   se ajusta sobre doce instrumentos entre 161 días y 31,6 años. El vacío ahora está en otro
   lado, y es el punto 4.

**Vigentes:**

4. **La curva de mercado no cubre plazos cortos.** Se ajusta desde 161 días. Por debajo
   extrapola y se dispara: a 30 días da 16,27 % contra una letra real en 10,72 %. Para ese
   tramo está la curva de fondeo, que arranca en el overnight. El motor expone el rango
   observado y no descuenta fuera de él sin avisar, pero **la responsabilidad de elegir curva
   es de quien la usa**.
5. **El segmento de dinero queda sin modelar.** Las tres letras de menos de 62 días se cargan
   y se reportan, pero no entran a ninguna curva paramétrica. Cotizan 150 a 200 bps por
   debajo del resto y ningún Nelson-Siegel-Svensson atraviesa ese quiebre. Modelarlas
   requeriría un segmento aparte, que este motor no tiene.
6. **Fechas de cotización heterogéneas.** Cada instrumento descuenta desde su propia fecha.
   En la muestra de referencia una letra cotiza un día antes que el resto, así que la curva
   supone implícitamente que el mercado no se movió en ese día. El reporte publica la
   dispersión de fechas para que el supuesto quede medido en vez de asumido.
7. **Los precios no se versionan.** Vienen de un proveedor comercial. Un clon del repositorio
   no puede reproducir la curva de mercado sin aportar el archivo; el motor se detiene con una
   excepción tipada y las instrucciones. Lo que sí está en el repositorio son las fichas de
   los instrumentos, los parámetros calibrados y los agregados de ajuste.
8. **Sin calendario de días hábiles.** Los flujos se ubican en fechas de aniversario exactas,
   sin ajustar por días no hábiles. Es el origen medido de la discrepancia contra QuantLib y
   la extensión natural hacia producción.
9. **Los CDT se excluyen como nodos.** El CDT a 180 días osciló entre 9,98 % y 10,65 % en una
   semana mientras el de 360 días iba de 11,74 % a 12,41 %: son promedios ponderados por monto
   emitido, reflejan qué bancos captaron cada día y no una estructura de plazos. Incluirlos
   elevaba el RMSE a 59 bps. Quedan disponibles con `construir_nodos_ibr(incluir_cdt=True)`.
10. **El ajuste no queda dentro de la horquilla en todos los puntos.** Cinco de los doce
    instrumentos caen estrictamente dentro de su media horquilla. El RMSE de 3,82 bps es del
    orden del costo de transacción —la media horquilla mediana es 3,22 bps— pero afirmar que
    la curva pasa por dentro del mercado en todo plazo sería falso.
11. **Paridad cubierta como referencia teórica.** El modelo entrega el forward de no
    arbitraje. Las desviaciones del mercado respecto de ese valor —el *cross-currency basis*—
    son un fenómeno documentado y no un error del modelo.
12. **Frecuencia de actualización.** IBR, TRM y SOFR son diarios. Los nodos TES dependen de
    que alguien vuelva a exportar el archivo del portal, y las cotizaciones de que alguien
    aporte el CSV. El reporte fecha la corrida con **el insumo más viejo**, no con el más
    nuevo: una curva es tan fresca como el dato más rancio que la alimenta, y reportar el más
    reciente escondería justamente la obsolescencia que hay que vigilar.

---

## Referencias

- Nelson, C. R., & Siegel, A. F. (1987). Parsimonious Modeling of Yield Curves.
  *The Journal of Business*, 60(4), 473–489.
- Svensson, L. E. O. (1994). Estimating and Interpreting Forward Interest Rates:
  Sweden 1992–1994. *NBER Working Paper 4871*.
- Litterman, R., & Scheinkman, J. (1991). Common Factors Affecting Bond Returns.
  *The Journal of Fixed Income*, 1(1), 54–61.
- Macaulay, F. R. (1938). *Some Theoretical Problems Suggested by the Movements of Interest
  Rates, Bond Yields and Stock Prices in the United States since 1856*. NBER.
- Redington, F. M. (1952). Review of the Principles of Life-Office Valuations.
  *Journal of the Institute of Actuaries*, 78(3), 286–340.
- Du, W., Tepper, A., & Verdelhan, A. (2018). Deviations from Covered Interest Rate Parity.
  *The Journal of Finance*, 73(3), 915–957.
- Banco de la República. *Metodología de la curva cero cupón de TES*, estimada con Nelson y
  Siegel (1987) sobre operaciones de SEN y MEC.

---

## Licencia y uso académico

Proyecto de investigación con fines académicos, desarrollado en el marco del Finance &
Investment Club de la Universidad Icesi. El código se apoya exclusivamente en fuentes
públicas y no incorpora datos propietarios. **No constituye recomendación de inversión.**
Cualquier uso sobre posiciones reales exige verificar de forma independiente los datos de
referencia de los instrumentos y las convenciones aplicables.
