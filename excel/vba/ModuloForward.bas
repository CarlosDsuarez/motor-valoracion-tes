Attribute VB_Name = "ModuloForward"
'===============================================================================
' ModuloForward - Forwards USD/COP por paridad cubierta de tasas de interes
'===============================================================================
'
' Valora forwards de tasa de cambio dentro de Excel usando la curva COP que
' Python dejo calibrada en NSS_PARAMS. Depende de ModuloCurvaNSS.
'
' MODELO
'   F = S0 * K_cop(d) / K_usd(d)
'
' CONVENCIONES
'   Pata COP: efectiva anual base 365 por defecto, K = (1 + i) ^ (d/365).
'             Es como publica la curva local el Banco de la Republica.
'   Pata USD: simple base 360, K = 1 + i * d/360. Es la convencion de SOFR.
'
'   La formula lineal clasica aplica base 360 tambien a la pata COP. Sobre el
'   MISMO numero de tasa da otro precio: FORWARD_USDCOP_SIMPLE la implementa y
'   BRECHA_CONVENCIONES_BPS mide la diferencia. Medido con TRM 3144.14, SOFR
'   3.66% y COP 12%, el atajo lineal sobrevalora hasta +24 bps cerca de 180-270
'   dias, cruza cero pasado el anio y a 2 anios subvalora ~88 bps. O sea: es mas
'   caro justo en la ventana donde el forward USD/COP tiene mas liquidez.
'
'===============================================================================

Option Explicit

Private Const DIAS_ANIO_365 As Double = 365#
Private Const DIAS_ANIO_360 As Double = 360#


'--- Factores de capitalizacion -----------------------------------------------

' K = (1 + i) ^ (d/365). Convencion de la curva COP.
Private Function FactorEA365(ByVal tasa As Double, ByVal plazoDias As Double) As Double
    If plazoDias = 0 Then
        FactorEA365 = 1#
    Else
        If (1# + tasa) <= 0 Then Err.Raise 5, , "1 + tasa no es positivo."
        FactorEA365 = (1# + tasa) ^ (plazoDias / DIAS_ANIO_365)
    End If
End Function

' K = 1 + i * d/360. Convencion de SOFR y de la formula lineal clasica.
Private Function FactorSimple360(ByVal tasa As Double, ByVal plazoDias As Double) As Double
    Dim k As Double
    k = 1# + tasa * plazoDias / DIAS_ANIO_360
    If k <= 0 Then Err.Raise 5, , "El factor de capitalizacion no es positivo."
    FactorSimple360 = k
End Function


' Toma la tasa COP del argumento si vino, y si no la lee de la curva calibrada.
Private Function ResolverTasaCop(ByVal plazoDias As Double, _
                                 ByVal tasaCop As Variant, _
                                 Optional ByVal paramsRange As Range) As Double
    Dim z As Variant

    ' Excel manda un argumento salteado en el medio -por ejemplo
    ' FORWARD_USDCOP(spot;dias;sofr;;params)- como Empty, no como Missing. Y en VBA
    ' IsNumeric(Empty) devuelve True, asi que sin el chequeo de IsEmpty se valoraria
    ' con tasa COP = 0 en lugar de leerla de la curva. Se descartan los dos casos.
    If Not IsMissing(tasaCop) Then
        If Not IsEmpty(tasaCop) Then
            If IsNumeric(tasaCop) Then
                ResolverTasaCop = CDbl(tasaCop)
                Exit Function
            End If
        End If
    End If

    z = TASA_CERO_CUPON(plazoDias / DIAS_ANIO_365, paramsRange)
    If IsError(z) Then Err.Raise 5, , "No pude leer la tasa COP de la curva."
    ResolverTasaCop = CDbl(z)
End Function


'--- Pricing ------------------------------------------------------------------

' Precio forward USD/COP, en pesos por dolar.
'
' spot      : TRM contado, pesos por dolar.
' plazoDias : plazo del contrato en dias calendario.
' tasaUsd   : tasa USD en decimal, simple ACT/360 (SOFR).
' tasaCop   : tasa COP en decimal. Opcional: si se omite, se lee de la curva
'             calibrada al plazo del contrato.
Public Function FORWARD_USDCOP(ByVal spot As Double, _
                               ByVal plazoDias As Double, _
                               ByVal tasaUsd As Double, _
                               Optional ByVal tasaCop As Variant, _
                               Optional ByVal paramsRange As Range) As Variant
    Dim iCop As Double

    On Error GoTo Fallo
    If spot <= 0 Or plazoDias < 0 Then GoTo Fallo

    iCop = ResolverTasaCop(plazoDias, tasaCop, paramsRange)

    FORWARD_USDCOP = spot * FactorEA365(iCop, plazoDias) _
                          / FactorSimple360(tasaUsd, plazoDias)
    Exit Function
Fallo:
    FORWARD_USDCOP = CVErr(xlErrValue)
End Function


' Variante con la formula lineal clasica: base 360 en las dos patas.
' Se expone para poder contrastar convenciones dentro del libro, no porque sea
' la correcta para el mercado local.
Public Function FORWARD_USDCOP_SIMPLE(ByVal spot As Double, _
                                      ByVal plazoDias As Double, _
                                      ByVal tasaUsd As Double, _
                                      Optional ByVal tasaCop As Variant, _
                                      Optional ByVal paramsRange As Range) As Variant
    Dim iCop As Double

    On Error GoTo Fallo
    If spot <= 0 Or plazoDias < 0 Then GoTo Fallo

    iCop = ResolverTasaCop(plazoDias, tasaCop, paramsRange)

    FORWARD_USDCOP_SIMPLE = spot * FactorSimple360(iCop, plazoDias) _
                                 / FactorSimple360(tasaUsd, plazoDias)
    Exit Function
Fallo:
    FORWARD_USDCOP_SIMPLE = CVErr(xlErrValue)
End Function


'--- Puntos forward y devaluacion ---------------------------------------------

' Puntos forward: F - S0, en pesos. Es lo que cotiza una mesa local.
Public Function PUNTOS_FORWARD(ByVal spot As Double, _
                               ByVal plazoDias As Double, _
                               ByVal tasaUsd As Double, _
                               Optional ByVal tasaCop As Variant, _
                               Optional ByVal paramsRange As Range) As Variant
    Dim f As Variant
    On Error GoTo Fallo
    f = FORWARD_USDCOP(spot, plazoDias, tasaUsd, tasaCop, paramsRange)
    If IsError(f) Then GoTo Fallo
    PUNTOS_FORWARD = CDbl(f) - spot
    Exit Function
Fallo:
    PUNTOS_FORWARD = CVErr(xlErrValue)
End Function


' Devaluacion implicita, expresada como tasa efectiva anual.
Public Function DEVALUACION_IMPLICITA(ByVal spot As Double, _
                                      ByVal plazoDias As Double, _
                                      ByVal tasaUsd As Double, _
                                      Optional ByVal tasaCop As Variant, _
                                      Optional ByVal paramsRange As Range) As Variant
    Dim f As Variant
    On Error GoTo Fallo
    If plazoDias <= 0 Then
        DEVALUACION_IMPLICITA = 0#
        Exit Function
    End If
    f = FORWARD_USDCOP(spot, plazoDias, tasaUsd, tasaCop, paramsRange)
    If IsError(f) Then GoTo Fallo
    DEVALUACION_IMPLICITA = (CDbl(f) / spot) ^ (DIAS_ANIO_365 / plazoDias) - 1#
    Exit Function
Fallo:
    DEVALUACION_IMPLICITA = CVErr(xlErrValue)
End Function


'--- Sensibilidades -----------------------------------------------------------

' Delta respecto al spot: dF/dS0 = K_cop / K_usd.
' Es cercano a 1 porque el diferencial de tasas es chico, no por definicion.
Public Function DELTA_SPOT_FWD(ByVal plazoDias As Double, _
                               ByVal tasaUsd As Double, _
                               Optional ByVal tasaCop As Variant, _
                               Optional ByVal paramsRange As Range) As Variant
    Dim iCop As Double
    On Error GoTo Fallo
    iCop = ResolverTasaCop(plazoDias, tasaCop, paramsRange)
    DELTA_SPOT_FWD = FactorEA365(iCop, plazoDias) / FactorSimple360(tasaUsd, plazoDias)
    Exit Function
Fallo:
    DELTA_SPOT_FWD = CVErr(xlErrValue)
End Function


' DV01 de la pata COP: cambio del precio forward, en pesos, ante +1 punto basico
' en la tasa local. Positivo: subir la tasa COP encarece el forward.
Public Function DV01_COP_FWD(ByVal spot As Double, _
                             ByVal plazoDias As Double, _
                             ByVal tasaUsd As Double, _
                             Optional ByVal tasaCop As Variant, _
                             Optional ByVal paramsRange As Range) As Variant
    Dim iCop As Double, kUsd As Double

    On Error GoTo Fallo
    If spot <= 0 Or plazoDias <= 0 Then GoTo Fallo

    iCop = ResolverTasaCop(plazoDias, tasaCop, paramsRange)
    kUsd = FactorSimple360(tasaUsd, plazoDias)

    ' Diferencia centrada de mas/menos medio punto basico.
    DV01_COP_FWD = spot * (FactorEA365(iCop + UN_BP / 2#, plazoDias) _
                         - FactorEA365(iCop - UN_BP / 2#, plazoDias)) / kUsd
    Exit Function
Fallo:
    DV01_COP_FWD = CVErr(xlErrValue)
End Function


' DV01 de la pata USD. Negativo, por simetria de la paridad.
Public Function DV01_USD_FWD(ByVal spot As Double, _
                             ByVal plazoDias As Double, _
                             ByVal tasaUsd As Double, _
                             Optional ByVal tasaCop As Variant, _
                             Optional ByVal paramsRange As Range) As Variant
    Dim iCop As Double, kCop As Double

    On Error GoTo Fallo
    If spot <= 0 Or plazoDias <= 0 Then GoTo Fallo

    iCop = ResolverTasaCop(plazoDias, tasaCop, paramsRange)
    kCop = FactorEA365(iCop, plazoDias)

    DV01_USD_FWD = spot * kCop * (1# / FactorSimple360(tasaUsd + UN_BP / 2#, plazoDias) _
                                - 1# / FactorSimple360(tasaUsd - UN_BP / 2#, plazoDias))
    Exit Function
Fallo:
    DV01_USD_FWD = CVErr(xlErrValue)
End Function


' Theta: cambio del precio, en pesos, por un dia mas de plazo.
Public Function THETA_DIA_FWD(ByVal spot As Double, _
                              ByVal plazoDias As Double, _
                              ByVal tasaUsd As Double, _
                              Optional ByVal tasaCop As Variant, _
                              Optional ByVal paramsRange As Range) As Variant
    Dim hoy As Variant, manana As Variant

    On Error GoTo Fallo
    hoy = FORWARD_USDCOP(spot, plazoDias, tasaUsd, tasaCop, paramsRange)
    manana = FORWARD_USDCOP(spot, plazoDias + 1#, tasaUsd, tasaCop, paramsRange)
    If IsError(hoy) Or IsError(manana) Then GoTo Fallo

    THETA_DIA_FWD = CDbl(manana) - CDbl(hoy)
    Exit Function
Fallo:
    THETA_DIA_FWD = CVErr(xlErrValue)
End Function


'--- Diagnostico de convenciones ----------------------------------------------

' Brecha entre la formula lineal clasica y la consistente con la curva local,
' expresada en puntos basicos del precio forward.
Public Function BRECHA_CONVENCIONES_BPS(ByVal spot As Double, _
                                        ByVal plazoDias As Double, _
                                        ByVal tasaUsd As Double, _
                                        Optional ByVal tasaCop As Variant, _
                                        Optional ByVal paramsRange As Range) As Variant
    Dim ea As Variant, simple As Variant

    On Error GoTo Fallo
    ea = FORWARD_USDCOP(spot, plazoDias, tasaUsd, tasaCop, paramsRange)
    simple = FORWARD_USDCOP_SIMPLE(spot, plazoDias, tasaUsd, tasaCop, paramsRange)
    If IsError(ea) Or IsError(simple) Then GoTo Fallo
    If CDbl(ea) = 0 Then GoTo Fallo

    BRECHA_CONVENCIONES_BPS = (CDbl(simple) - CDbl(ea)) / CDbl(ea) / UN_BP
    Exit Function
Fallo:
    BRECHA_CONVENCIONES_BPS = CVErr(xlErrValue)
End Function
