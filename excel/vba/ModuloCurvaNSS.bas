Attribute VB_Name = "ModuloCurvaNSS"
'===============================================================================
' ModuloCurvaNSS - Curva cero cupon Nelson-Siegel-Svensson como UDFs de Excel
'===============================================================================
'
' Replica en VBA la curva calibrada en Python, para pricing intradia dentro del
' libro sin depender de que Python este instalado. El flujo es el de una mesa
' real: Python calibra en batch contra los datos del Banco de la Republica y
' deja los seis parametros en el rango con nombre NSS_PARAMS; estas funciones
' los leen y valoran al instante.
'
' MODELO
'   z(t) = b0 + b1*f1(t/L1) + b2*f2(t/L1) + b3*f2(t/L2)
'   f1(x) = (1 - Exp(-x)) / x
'   f2(x) = f1(x) - Exp(-x)
'
' CONVENCIONES
'   - Plazos en anios, base Actual/365.
'   - Tasas efectivas anuales en decimal (0.1243 = 12.43%).
'   - Descuento: DF(t) = (1 + z(t)) ^ -t
'
' RANGO NSS_PARAMS
'   Seis celdas en orden: beta0, beta1, beta2, beta3, lambda1, lambda2.
'   Lo escribe motor_tes.export_excel. Acepta orientacion en fila o columna.
'
' DEPENDENCIAS EN EXCEL
'   Si se llama la UDF sin pasar el rango, Excel no sabe que la formula depende
'   de NSS_PARAMS y no recalcula cuando cambian los parametros. Para calculo
'   automatico, pasar el rango explicitamente:
'       =TASA_CERO_CUPON(5; NSS_PARAMS)
'   Sin el argumento la funcion igual resuelve, leyendo el nombre del libro.
'
'===============================================================================

Option Explicit

' Por debajo de este cociente t/lambda se usa la expansion de Taylor de los
' factores de carga, porque la forma exacta es 0/0 cuando t tiende a cero.
Private Const UMBRAL_TAYLOR As Double = 0.000001

' Un punto basico, en decimal.
Public Const UN_BP As Double = 0.0001

' Nombre del rango donde Python deja los parametros calibrados.
Private Const NOMBRE_RANGO_PARAMS As String = "NSS_PARAMS"


'--- Lectura de parametros ----------------------------------------------------

' Devuelve los seis parametros como array base 0: (b0, b1, b2, b3, L1, L2).
' Si paramsRange es Nothing, los toma del rango con nombre NSS_PARAMS.
Private Function LeerParametros(Optional ByVal paramsRange As Range = Nothing) As Double()
    Dim origen As Range
    Dim valores() As Double
    Dim celda As Range
    Dim i As Long

    ReDim valores(0 To 5)

    If paramsRange Is Nothing Then
        On Error GoTo SinRango
        Set origen = ThisWorkbook.Names(NOMBRE_RANGO_PARAMS).RefersToRange
        On Error GoTo 0
    Else
        Set origen = paramsRange
    End If

    If origen.Count < 6 Then Err.Raise 5, , _
        "El rango de parametros debe tener 6 celdas (b0, b1, b2, b3, L1, L2)."

    i = 0
    For Each celda In origen
        If i > 5 Then Exit For
        If Not IsNumeric(celda.Value) Then Err.Raise 5, , _
            "La celda " & celda.Address & " del rango de parametros no es numerica."
        valores(i) = CDbl(celda.Value)
        i = i + 1
    Next celda

    If valores(4) <= 0 Or valores(5) <= 0 Then Err.Raise 5, , _
        "lambda1 y lambda2 deben ser positivos."

    LeerParametros = valores
    Exit Function

SinRango:
    Err.Raise 5, , "No encontre el rango con nombre " & NOMBRE_RANGO_PARAMS & _
        ". Corre 'python -m motor_tes.cli excel' para regenerarlo."
End Function


'--- Factores de carga --------------------------------------------------------

' f1(x) = (1 - Exp(-x)) / x, con expansion de Taylor cerca de cero.
Private Function FactorNivel(ByVal x As Double) As Double
    If x < UMBRAL_TAYLOR Then
        FactorNivel = 1# - x / 2# + x * x / 6#
    Else
        FactorNivel = (1# - Exp(-x)) / x
    End If
End Function

' f2(x) = f1(x) - Exp(-x), con expansion de Taylor cerca de cero.
Private Function FactorCurvatura(ByVal x As Double) As Double
    If x < UMBRAL_TAYLOR Then
        FactorCurvatura = x / 2# - x * x / 3#
    Else
        FactorCurvatura = FactorNivel(x) - Exp(-x)
    End If
End Function


'--- Funciones publicas -------------------------------------------------------

' Tasa cero cupon efectiva anual para el plazo indicado.
'
' plazoAnios : plazo en anios (base Actual/365), no negativo.
' paramsRange: rango opcional con los seis parametros. Si se omite, se lee
'              NSS_PARAMS del libro.
Public Function TASA_CERO_CUPON(ByVal plazoAnios As Double, _
                                Optional ByVal paramsRange As Range = Nothing) As Variant
    Dim p() As Double
    Dim x1 As Double, x2 As Double

    On Error GoTo Fallo
    If plazoAnios < 0 Then GoTo Fallo

    p = LeerParametros(paramsRange)
    x1 = plazoAnios / p(4)
    x2 = plazoAnios / p(5)

    TASA_CERO_CUPON = p(0) _
                    + p(1) * FactorNivel(x1) _
                    + p(2) * FactorCurvatura(x1) _
                    + p(3) * FactorCurvatura(x2)
    Exit Function
Fallo:
    TASA_CERO_CUPON = CVErr(xlErrValue)
End Function


' Factor de descuento DF(t) = (1 + z(t)) ^ -t.
Public Function FACTOR_DESCUENTO(ByVal plazoAnios As Double, _
                                 Optional ByVal paramsRange As Range = Nothing) As Variant
    Dim z As Variant

    On Error GoTo Fallo
    If plazoAnios < 0 Then GoTo Fallo

    z = TASA_CERO_CUPON(plazoAnios, paramsRange)
    If IsError(z) Then GoTo Fallo
    If (1# + CDbl(z)) <= 0 Then GoTo Fallo

    FACTOR_DESCUENTO = (1# + CDbl(z)) ^ (-plazoAnios)
    Exit Function
Fallo:
    FACTOR_DESCUENTO = CVErr(xlErrValue)
End Function


' Forward instantanea en t, por diferencias centradas sobre ln DF.
Private Function ForwardInstantanea(ByVal t As Double, _
                                    Optional ByVal paramsRange As Range = Nothing) As Variant
    Dim h As Double, tIzq As Double, tDer As Double
    Dim dfIzq As Variant, dfDer As Variant
    Dim fContinua As Double

    On Error GoTo Fallo
    h = 0.000001
    tIzq = t - h
    If tIzq < 0 Then tIzq = 0
    tDer = t + h

    dfIzq = FACTOR_DESCUENTO(tIzq, paramsRange)
    dfDer = FACTOR_DESCUENTO(tDer, paramsRange)
    If IsError(dfIzq) Or IsError(dfDer) Then GoTo Fallo

    fContinua = -(Log(CDbl(dfDer)) - Log(CDbl(dfIzq))) / (tDer - tIzq)
    ForwardInstantanea = Exp(fContinua) - 1#
    Exit Function
Fallo:
    ForwardInstantanea = CVErr(xlErrValue)
End Function


' Tasa forward efectiva anual implicita entre t1 y t2, por no arbitraje:
'   (1 + f) ^ (t2 - t1) = DF(t1) / DF(t2)
Public Function TASA_FORWARD(ByVal t1 As Double, ByVal t2 As Double, _
                             Optional ByVal paramsRange As Range = Nothing) As Variant
    Dim df1 As Variant, df2 As Variant

    On Error GoTo Fallo
    If t1 < 0 Or t2 < t1 Then GoTo Fallo

    ' Intervalo nulo: se aproxima la forward instantanea por diferencia centrada.
    If Abs(t2 - t1) < 0.0000001 Then
        TASA_FORWARD = ForwardInstantanea(t1, paramsRange)
        Exit Function
    End If

    df1 = FACTOR_DESCUENTO(t1, paramsRange)
    df2 = FACTOR_DESCUENTO(t2, paramsRange)
    If IsError(df1) Or IsError(df2) Then GoTo Fallo
    If CDbl(df2) <= 0 Then GoTo Fallo

    TASA_FORWARD = (CDbl(df1) / CDbl(df2)) ^ (1# / (t2 - t1)) - 1#
    Exit Function
Fallo:
    TASA_FORWARD = CVErr(xlErrValue)
End Function


'--- Riesgo de un bono TES ----------------------------------------------------

' Valor presente de un bono bullet a tasa fija, descontando cada flujo con la
' curva. Los flujos se ubican en anios exactos hacia atras desde el vencimiento.
'
' shift permite desplazar la curva en paralelo (en decimal) para calcular DV01.
Private Function ValorPresenteBono(ByVal cupon As Double, _
                                   ByVal plazoAnios As Double, _
                                   ByVal frecuencia As Long, _
                                   ByVal nominal As Double, _
                                   ByVal shift As Double, _
                                   Optional ByVal paramsRange As Range = Nothing) As Double
    Dim p() As Double
    Dim paso As Double, cuponPeriodico As Double
    Dim t As Double, z As Double, x1 As Double, x2 As Double
    Dim flujo As Double, vp As Double

    p = LeerParametros(paramsRange)
    paso = 1# / frecuencia
    cuponPeriodico = nominal * cupon / frecuencia

    vp = 0#
    t = plazoAnios
    Do While t > 0.000000001
        x1 = t / p(4)
        x2 = t / p(5)
        z = p(0) + p(1) * FactorNivel(x1) + p(2) * FactorCurvatura(x1) _
                 + p(3) * FactorCurvatura(x2) + shift

        flujo = cuponPeriodico
        If t = plazoAnios Then flujo = flujo + nominal

        If (1# + z) <= 0 Then Err.Raise 5, , "Factor de capitalizacion no positivo."
        vp = vp + flujo * (1# + z) ^ (-t)
        t = t - paso
    Loop

    ValorPresenteBono = vp
End Function


' Valor presente de un bono TES con la curva vigente.
Public Function VP_TES(ByVal cupon As Double, _
                       ByVal plazoAnios As Double, _
                       Optional ByVal frecuencia As Long = 1, _
                       Optional ByVal nominal As Double = 100#, _
                       Optional ByVal paramsRange As Range = Nothing) As Variant
    On Error GoTo Fallo
    If plazoAnios <= 0 Or frecuencia <= 0 Then GoTo Fallo
    VP_TES = ValorPresenteBono(cupon, plazoAnios, frecuencia, nominal, 0#, paramsRange)
    Exit Function
Fallo:
    VP_TES = CVErr(xlErrValue)
End Function


' DV01 de un bono TES: cambio de valor presente ante un desplazamiento paralelo
' de un punto basico en toda la curva. Signo positivo para posicion larga.
'
' Se calcula por diferencia centrada de mas/menos medio punto basico, que cancela
' el error de segundo orden.
'
' cupon      : tasa cupon anual en decimal (0.1325 = 13.25%).
' plazoAnios : anios hasta el vencimiento.
' frecuencia : pagos por anio. Opcional, por defecto 1 (los TES pagan anual).
' nominal    : valor facial. Opcional, por defecto 100.
Public Function DV01_TES(ByVal cupon As Double, _
                         ByVal plazoAnios As Double, _
                         Optional ByVal frecuencia As Long = 1, _
                         Optional ByVal nominal As Double = 100#, _
                         Optional ByVal paramsRange As Range = Nothing) As Variant
    Dim medioBp As Double

    On Error GoTo Fallo
    If plazoAnios <= 0 Or frecuencia <= 0 Then GoTo Fallo

    medioBp = UN_BP / 2#
    DV01_TES = ValorPresenteBono(cupon, plazoAnios, frecuencia, nominal, -medioBp, paramsRange) _
             - ValorPresenteBono(cupon, plazoAnios, frecuencia, nominal, medioBp, paramsRange)
    Exit Function
Fallo:
    DV01_TES = CVErr(xlErrValue)
End Function


' Duracion modificada implicita en el DV01, en anios:
'   D_mod = DV01 / (VP * 1bp)
' No requiere reducir la curva a una TIR unica, asi que es consistente con
' descontar cada flujo a su propia tasa cero cupon.
Public Function DURACION_MOD_TES(ByVal cupon As Double, _
                                 ByVal plazoAnios As Double, _
                                 Optional ByVal frecuencia As Long = 1, _
                                 Optional ByVal nominal As Double = 100#, _
                                 Optional ByVal paramsRange As Range = Nothing) As Variant
    Dim vp As Variant, sensibilidad As Variant

    On Error GoTo Fallo
    vp = VP_TES(cupon, plazoAnios, frecuencia, nominal, paramsRange)
    sensibilidad = DV01_TES(cupon, plazoAnios, frecuencia, nominal, paramsRange)
    If IsError(vp) Or IsError(sensibilidad) Then GoTo Fallo
    If CDbl(vp) = 0 Then GoTo Fallo

    DURACION_MOD_TES = CDbl(sensibilidad) / (CDbl(vp) * UN_BP)
    Exit Function
Fallo:
    DURACION_MOD_TES = CVErr(xlErrValue)
End Function
