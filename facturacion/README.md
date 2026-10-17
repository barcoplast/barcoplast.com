# BarcoFact: facturación electrónica de BARCOPLAST (ARCA, WSFEv1)

Sistema local para importar o cargar ventas (WhatsApp, Mercado Libre, CSV o carga manual), validarlas, calcular los importes con el IVA incluido, mostrar una vista previa y pedir el CAE a ARCA. Guarda el resultado y genera el PDF con QR, sin facturar dos veces la misma venta.

> **Estado al 17/10/2026**
>
> - **Probado:** solo con pruebas **SIMULADAS** (44 pruebas automáticas contra un simulador local de ARCA).
> - **Sin probar:** homologación real de ARCA y API real de Mercado Libre. Ninguna de las dos se probó todavía, porque faltan los certificados, el CUIT del emisor y el acceso de red. No hay que darlas por funcionando hasta correr las pruebas de la sección 6.

## 1. Arquitectura

```
facturacion/
  barcofact/
    config.py            configuración local (fuera del repo), homologación y producción separadas
    fiscal.py            tablas ARCA, tipo de comprobante según emisor/receptor, validación de CUIT
    importes.py          aritmética decimal: precio con IVA → neto + IVA por alícuota
    validacion.py        datos faltantes / inconsistencias con «cómo resolverlo»
    emision.py           numeración con bloqueo, escritura anticipada, conciliación, notas de crédito
    arca/wsaa.py         autenticación con certificado (LoginCms, CMS firmado)
    arca/wsfe.py         FECAESolicitar, FECompUltimoAutorizado, FECompConsultar, parámetros
    arca/simulador.py    simulador local con fallas inyectables (sólo pruebas)
    comprobante_pdf.py   PDF con datos obligatorios, QR (RG 4892) y leyenda Ley 27.743 en clase B
    canales/             csv_import, whatsapp (borradores), mercadolibre (API oficial)
    web.py               pantalla de control en http://127.0.0.1:8765
    exportar.py          CSV/ZIP para el contador
    backup.py            copias de seguridad y restauración
    automatico.py        emisión automática (DESACTIVADA)
  tests/                 pruebas simuladas
```

- **Base de datos:** SQLite, un archivo por entorno, con permisos 600.
- **Dependencias:** `cryptography`, `reportlab` y `requests`.
- **Datos y secretos:** viven en `~/.barcofact` (o en `BARCOFACT_DATOS`), **nunca en el repositorio**.
- **Sitio web:** el sitio se publica con GitHub Pages y `_config.yml` excluye esta carpeta. Igual conviene pasar el sistema a un repositorio **privado** propio.

## 2. Instalación

```bash
cd facturacion
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m barcofact init          # crea ~/.barcofact/config.toml y catalogo.csv
```

Después:

1. Completá `[emisor]` en `~/.barcofact/config.toml` con los datos de la **constancia de inscripción** del emisor actual.
2. Revisá `catalogo.csv` con tu contador. Todas las alícuotas vienen en 21 % y marcadas `alicuota_confirmada = no`.
3. Creá tu usuario: `python -m barcofact crear-usuario TU_NOMBRE`. La clave se escribe en tu terminal, no en el chat.

## 3. Configurar ARCA (lo hacés vos, con tu clave fiscal, en la web de ARCA)

El sistema **no** entra a la web de ARCA con tu clave fiscal. Usa los web services oficiales con certificado. Nunca me pases claves, certificados ni claves privadas por el chat.

### Homologación (pruebas)

1. Ingresá a ARCA con clave fiscal y adherí el servicio **«WSASS – Autogestión Certificados Homologación»**.
2. Generá la clave y el pedido de certificado (CSR) en tu equipo:
   `python -m barcofact --entorno homologacion generar-csr --organizacion "RAZON SOCIAL"`
   La clave privada queda en `~/.barcofact/certs/homologacion.key` y nunca sale de tu equipo.
3. En WSASS:
   1. Creá un certificado pegando el contenido de `homologacion.csr`.
   2. Guardá el certificado como `~/.barcofact/certs/homologacion.crt` y ejecutá `chmod 600` sobre él.
   3. Creá la autorización para el servicio **wsfe** (Facturación electrónica) con tu CUIT.
4. Cargá `punto_venta` en `[homologacion]`.
5. Probá la conexión: `python -m barcofact verificar --parametros`.
   - Si falla por TLS, probá con `BARCOFACT_TLS_LEGADO=1`.

### Producción (recién después de aprobar la vista previa del primer comprobante real)

1. Generá la clave y el CSR de producción: `python -m barcofact --entorno produccion generar-csr --organizacion "RAZON SOCIAL"`.
2. En ARCA:
   1. En **«Administración de Certificados Digitales»**, creá un alias, subí el CSR y descargá el `.crt`.
   2. En **«Administrador de Relaciones de Clave Fiscal»**, creá una nueva relación con el servicio **«Facturación Electrónica»** (wsfe) para ese certificado.
3. En **«Administración de puntos de venta y domicilios»**, creá un punto de venta **exclusivo** para este sistema, con el sistema **«Factura Electrónica – Web Services»** (RECE para aplicativo y web services).
   - No compartas el punto de venta con Comprobantes en línea ni con otra herramienta. Si el sistema detecta numeración ajena, se frena.
4. Cargá la ruta del certificado y el punto de venta en `[produccion]`. Dejá `habilitada = false` hasta la aprobación.

Si ARCA encuadró al emisor para emitir clase **M**, poné `emite_clase_m = true`.

## 4. Uso diario

```bash
python -m barcofact web                       # homologación real (requiere certificado)
python -m barcofact --simulado web            # simulador local, sin ARCA (sólo para practicar)
python -m barcofact --entorno produccion web  # producción (banda roja en pantalla)
```

1. **Cargar** una venta: «Cargar venta», «Importar CSV», «Borrador WhatsApp» o «Mercado Libre → Sincronizar».
2. Cada venta tiene un **ID interno único** y una clave única **canal + n° de pedido**. Si importás el mismo pedido dos veces, no se duplica.
3. **Validación:** cada problema dice cómo resolverlo. Las ventas incompletas quedan en «Faltan datos».
4. **Vista previa:** muestra el tipo de comprobante (A/B/M) y el motivo, los datos del receptor y los importes por alícuota.
5. **Aprobar y emitir:** queda registrado quién aprobó y cuándo. En producción además hay que escribir el CUIT del emisor como confirmación.
6. **Resultado:**
   - **Autorizado:** se guardan el CAE, su vencimiento, el número y la respuesta de ARCA, y se genera el PDF.
   - **Rechazado:** se muestran los errores. Corregís y reemitís; el número rechazado no se consume.
   - **Sin respuesta / corte:** el comprobante queda «incierto». El sistema consulta a ARCA (`FECompConsultar`) antes de cualquier reintento. Un timeout nunca se toma como «no emitido».
7. **Notas de crédito:** sólo sobre facturas autorizadas, siempre con aprobación y motivo. Pueden ser totales o parciales, con control de saldo. Los comprobantes autorizados no se borran.
8. **Exportar:** CSV para el contador (las notas de crédito van en negativo) o ZIP con los PDF.
9. **Copias de seguridad:** `python -m barcofact backup --copiar-a /ruta/externa` (programala a diario con cron).
   - `restaurar` se niega a perder comprobantes autorizados.
   - Los certificados y las claves se guardan aparte, en un lugar seguro.

### Mercado Libre

1. Registrá una aplicación en developers.mercadolibre.com.ar (permisos de lectura de órdenes).
2. Ejecutá `python -m barcofact ml-configurar` y seguí los pasos en tu terminal.

Al sincronizar:

- Se factura lo que **pagó el comprador**: ítems más el envío a su cargo, nunca el neto de comisiones.
- Si el pedido ya tiene un documento fiscal en ML, queda «Facturada por otra herramienta».
- Si no se pudo verificar, la venta queda bloqueada hasta que lo verifiques.
- Las cancelaciones no generan nota de crédito automática: se marcan para revisión.

### WhatsApp

No hay integración oficial configurada, así que hoy se usa carga manual, CSV o el borrador desde un mensaje pegado.

- El borrador **nunca** se emite sin que confirmes cada dato.
- No deduce CUIT, DNI, domicilio ni condición fiscal.
- Más adelante se puede conectar la API oficial de WhatsApp Business (Cloud API).

### Emisión automática (desactivada)

Se activa en `[automatico]` con `habilitado = true`, los `canales`, el `importe_maximo` y `reglas_confirmadas_por`. En producción además requiere `[produccion] habilitada = true`.

Sólo emite ventas **sin ningún aviso**: pago acreditado, alícuotas confirmadas por el contador, verificación de ML hecha, etc. Se ejecuta con `python -m barcofact automatico`, por ejemplo con cron.

### Envío a clientes

Está deshabilitado (`[envio_clientes] habilitado = false`). El sistema no envía nada por WhatsApp ni por correo.

## 5. Reglas fiscales aplicadas (confirmar con el contador)

| Regla | Fuente | Estado |
|---|---|---|
| RI → A a RI y monotributistas; B a consumidor final, exento y no alcanzado | RG 1415, RG 5003/2021 | Confirmar |
| `CondicionIVAReceptorId` en cada comprobante | RG 5616 / manual WSFEv1 (obligatorio desde 2026; la v4.8 prorrogó al 01/12/2026 según fuentes secundarias) | Se envía siempre |
| Identificar consumidor final desde $ 10.000.000 | RG 5700/2025 y RG 5866/2026 (configurable) | Confirmar |
| Clase B: «Régimen de Transparencia Fiscal al Consumidor (Ley 27.743)» con IVA contenido | RG 5614/2024 | Implementado |
| Plazo de emisión para bienes: hasta el cierre del mes de entrega | RG 5866/2026 | Aviso en pantalla |
| Alícuota de resinas, pinturas y demás: 21 % por defecto | — | **Sin confirmar** |
| Envío cobrado al comprador: 21 %, concepto «productos» | — | **Sin confirmar** |
| Cupones de ML: quién financia el descuento | — | **Sin confirmar** (aviso) |
| Clientes del exterior (Factura E, WSFEX) | — | No implementado |

No pude acceder a la documentación oficial de ARCA desde el entorno de desarrollo (la red lo bloqueaba). Antes de producción:

1. Contrastá estas reglas con el manual WSFEv1 vigente.
2. Contrastá los parámetros con `verificar --parametros`, que lista tipos de comprobante, documentos, alícuotas y condiciones de IVA del receptor directamente desde ARCA.

## 6. Plan de pruebas

### Simuladas (hechas, `python -m unittest discover -s tests -t .`)

- Venta con IVA incluido.
- Datos incompletos.
- Pedido ya facturado o facturado por otra herramienta.
- Corte de conexión antes y después de autorizar, y también con ARCA caído en la consulta.
- Rechazo.
- Numeración tomada por otro sistema, tanto antes como durante el envío.
- Emisión concurrente.
- Redondeos (3.000 casos aleatorios).
- PDF/QR desde una autorización válida.
- Notas de crédito, modo automático, bloqueo de producción, backup/restauración y pantalla web.

### Homologación real (pendientes)

Con el certificado de homologación cargado:

1. `verificar --parametros`: conexión, punto de venta y tablas.
2. Factura B a consumidor final con IVA incluido. Verificá el CAE con FECompConsultar y el QR.
3. Factura A a un CUIT de prueba con alícuotas 21 % y 10,5 %.
4. Pedido con datos faltantes: no debe llegar a ARCA.
5. Reemitir una venta ya facturada: debe bloquearse.
6. Corte de red durante el envío (desenchufar o bloquear el host): queda «incierto» y luego se concilia.
7. Rechazo provocado (por ejemplo, alícuota inválida): queda en error y se reemite sin consumir número.
8. Emitir un comprobante en el mismo PV desde Comprobantes en línea de homologación: el sistema debe detectar la numeración ajena.
9. Redondeos con precios como 33,33 × 3.
10. Nota de crédito total y parcial asociada.

Cada resultado se registra indicando que fue **homologación real**.
