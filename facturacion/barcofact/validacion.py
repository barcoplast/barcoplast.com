"""Validación de una venta antes de facturar.

Cada problema indica cómo resolverlo. Los bloqueantes impiden emitir; los avisos se
muestran en la vista previa y, en modo automático, también bloquean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import fiscal, importes
from .config import Config


@dataclass
class Problema:
    campo: str
    mensaje: str
    como_resolver: str
    bloqueante: bool = True


@dataclass
class Resultado:
    problemas: list[Problema] = field(default_factory=list)
    tipo: int | None = None
    motivo_tipo: str = ""
    calculo: importes.Calculo | None = None
    concepto: int = fiscal.CONCEPTO_PRODUCTOS
    doc_tipo: int | None = None
    doc_nro: str = ""
    cond_iva: int | None = None

    @property
    def bloqueantes(self) -> list[Problema]:
        return [p for p in self.problemas if p.bloqueante]

    @property
    def avisos(self) -> list[Problema]:
        return [p for p in self.problemas if not p.bloqueante]

    @property
    def emitible(self) -> bool:
        return not self.bloqueantes and self.tipo is not None and self.calculo is not None

    def error(self, campo, mensaje, como):
        self.problemas.append(Problema(campo, mensaje, como, True))

    def aviso(self, campo, mensaje, como):
        self.problemas.append(Problema(campo, mensaje, como, False))


def lineas_de(venta: dict) -> tuple[list[importes.LineaEntrada], list[str]]:
    """Convierte los ítems guardados en líneas de cálculo. Devuelve también errores de formato."""
    lineas, errores = [], []
    for i, it in enumerate(venta["items"], 1):
        desc = (it.get("descripcion") or "").strip()
        try:
            alic_txt = str(it.get("alicuota", "")).strip().replace("%", "").replace(",", ".")
            if alic_txt in (importes.EXENTO, importes.NO_GRAVADO):
                alic = alic_txt
            elif alic_txt == "" or alic_txt == "None":
                errores.append(f"Ítem {i} «{desc}»: falta la alícuota de IVA.")
                continue
            else:
                alic = Decimal(alic_txt)
            lineas.append(importes.LineaEntrada(
                descripcion=desc or f"Ítem {i}",
                cantidad=importes.dec(it.get("cantidad", "0")),
                precio_unitario_final=importes.dec(it.get("precio_unitario_final", "0")),
                alicuota=alic,
                descuento_final=importes.dec(it.get("descuento_final", "0") or "0"),
                sku=it.get("sku", ""),
                es_envio=bool(it.get("es_envio", False)),
            ))
        except (importes.ErrorImportes, ArithmeticError, ValueError) as e:
            errores.append(f"Ítem {i} «{desc}»: {e}")
    return lineas, errores


def validar(venta: dict, cfg: Config, automatico: bool = False) -> Resultado:
    r = Resultado()

    # Emisor y entorno
    for campo in cfg.emisor.faltantes():
        r.error(f"emisor.{campo}", f"Falta el dato del emisor «{campo}».",
                "Completalo en config.toml (sección [emisor]) con los datos de la constancia de inscripción.")
    if cfg.emisor.cuit and not fiscal.cuit_valido(cfg.emisor.cuit):
        r.error("emisor.cuit", "El CUIT del emisor no es válido.", "Revisá el CUIT en config.toml.")
    if cfg.arca.punto_venta <= 0:
        r.error("punto_venta", f"No hay punto de venta configurado para {cfg.entorno}.",
                "Creá un punto de venta 'RECE para aplicativo y web services' en ARCA y cargalo en config.toml.")
    if cfg.es_produccion and not cfg.arca.habilitada:
        r.error("produccion", "Producción no está habilitada.",
                "Se habilita sólo después de aprobar la vista previa del primer comprobante real.")

    # Estado de la operación
    if venta["estado"] == "borrador":
        r.error("estado", "La venta es un borrador (por ejemplo, extraído de un mensaje).",
                "Revisá y confirmá cada dato y marcala como confirmada; una conversación no alcanza para emitir.")
    if venta["estado_operacion"] != "activa":
        r.error("estado_operacion", f"La operación figura como «{venta['estado_operacion']}».",
                "No se factura una venta cancelada. Si ya tenía factura, evaluá una nota de crédito.")
    if venta.get("facturada_externamente"):
        r.error("facturada_externamente",
                f"Ya fue facturada por otra herramienta: {venta['facturada_externamente']}.",
                "No emitir. Si el dato es erróneo, quitá la referencia indicando el motivo.")
    if venta["canal"] == "mercadolibre":
        dc = venta.get("datos_canal", {})
        verif = dc.get("verificacion_facturacion_ml", "")
        if verif != "sin_comprobante" and not dc.get("verificacion_manual"):
            r.error("mercadolibre.facturacion",
                    f"No está verificado que Mercado Libre u otra herramienta no la haya facturado ({verif or 'sin dato'}).",
                    "Sincronizá de nuevo o revisá el pedido en Mercado Libre y registrá la verificación manual.")
        if dc.get("cupon") and dc["cupon"].get("amount"):
            r.aviso("mercadolibre.cupon", "El pedido tiene un cupón/descuento.",
                    "Confirmá con el contador si el descuento lo financió Mercado Libre o el vendedor y cómo se factura.")
    if venta["moneda"] != "PES":
        r.error("moneda", f"Moneda {venta['moneda']} no soportada todavía.",
                "Esta versión emite en pesos. Consultá al contador cómo facturar en moneda extranjera.")
    if venta["estado_pago"] != "acreditado":
        (r.error if automatico else r.aviso)(
            "estado_pago", f"Estado de pago: {venta['estado_pago']}.",
            "Verificá que el cobro esté acreditado o que corresponda facturar igual (entrega de bienes).")
    try:
        fv = date.fromisoformat(venta["fecha_venta"])
        hoy = date.today()
        if (fv.year, fv.month) < (hoy.year, hoy.month):
            r.aviso("fecha_venta", "La venta es de un mes anterior.",
                    "Para bienes, el comprobante debe emitirse a más tardar al cierre del mes de entrega. "
                    "Consultá al contador.")
        if fv > hoy:
            r.error("fecha_venta", "La fecha de venta es futura.", "Corregí la fecha.")
    except ValueError:
        r.error("fecha_venta", "Fecha de venta inválida.", "Usá el formato AAAA-MM-DD.")

    # Ítems e importes
    lineas, errores = lineas_de(venta)
    for e in errores:
        r.error("items", e, "Corregí el ítem (cantidad, precio con IVA incluido y alícuota).")
    for it in venta["items"]:
        if not it.get("alicuota_confirmada", False):
            (r.error if automatico else r.aviso)(
                "items.alicuota", f"Alícuota de «{it.get('descripcion', '')}» sin confirmar por el contador.",
                "Confirmá la alícuota en el catálogo (catalogo.csv, columna alicuota_confirmada).")
        if it.get("es_envio"):
            r.aviso("envio", "La venta incluye envío facturado al comprador.",
                    "Verificá que el envío lo pagó el comprador y su tratamiento de IVA con el contador.")
    if lineas and not errores:
        try:
            r.calculo = importes.calcular(lineas, importes.dec(venta["descuento_general"] or "0"))
        except importes.ErrorImportes as e:
            r.error("importes", str(e), "Revisá cantidades, precios y descuentos.")
    elif not lineas and not errores:
        r.error("items", "La venta no tiene ítems.", "Agregá los productos vendidos.")

    if r.calculo and venta.get("total_informado") not in (None, ""):
        informado = importes.dec(venta["total_informado"])
        if informado != r.calculo.imp_total:
            r.error("total", f"El total calculado ({r.calculo.imp_total}) no coincide con el total "
                    f"informado por el canal ({informado}).",
                    "Revisá envío, descuentos y cupones. Se factura lo que pagó el comprador, "
                    "nunca el neto de comisiones.")
    if r.calculo and r.calculo.imp_total <= 0:
        r.error("total", "El total es cero o negativo.", "Revisá los ítems.")

    # Receptor
    cli = venta["cliente"]
    cond = fiscal.condicion_desde_texto(cli.get("condicion_iva"))
    r.cond_iva = cond
    td = fiscal.determinar_tipo(cfg.emisor.condicion_iva, cfg.emisor.emite_clase_m, cond)
    if td.error:
        r.error("cliente.condicion_iva", td.error,
                "Pedile al cliente su condición frente al IVA (o consultá su constancia de CUIT).")
    r.tipo, r.motivo_tipo = td.tipo, td.motivo

    tipo_doc = cli.get("tipo_doc")
    tipo_doc = int(tipo_doc) if str(tipo_doc or "").isdigit() else None
    nro = "".join(ch for ch in str(cli.get("nro_doc", "")) if ch.isdigit())
    nombre = (cli.get("nombre") or "").strip()
    domicilio = (cli.get("domicilio") or "").strip()

    if r.tipo is not None and fiscal.es_clase_a_o_m(r.tipo):
        if tipo_doc != fiscal.DOC_CUIT or not fiscal.cuit_valido(nro):
            r.error("cliente.cuit", "Para factura A/M el receptor debe identificarse con CUIT válido.",
                    "Pedile al cliente su CUIT. No se inventa ni se completa con datos de otra persona.")
        if not nombre:
            r.error("cliente.nombre", "Falta la razón social del receptor.", "Usá la de su constancia de CUIT.")
        if not domicilio:
            r.error("cliente.domicilio", "Falta el domicilio del receptor.", "Usá el de su constancia de CUIT.")
    elif r.tipo is not None:
        supera = r.calculo is not None and r.calculo.imp_total >= cfg.reglas.umbral_identificacion_cf
        if cond != fiscal.COND_CONSUMIDOR_FINAL:
            if tipo_doc != fiscal.DOC_CUIT or not fiscal.cuit_valido(nro):
                r.error("cliente.cuit", "Receptor exento / no alcanzado: identificalo con CUIT válido.",
                        "Pedile al cliente su CUIT.")
        elif supera and (tipo_doc in (None, fiscal.DOC_SIN_IDENTIFICAR) or not nro or not nombre or not domicilio):
            r.error("cliente.identificacion",
                    f"El total supera ${cfg.reglas.umbral_identificacion_cf}: hay que identificar al consumidor final "
                    "(documento, nombre y domicilio).", "Pedile DNI/CUIL/CUIT, nombre y domicilio al cliente.")
        if tipo_doc in (fiscal.DOC_CUIT, fiscal.DOC_CUIL) and not fiscal.cuit_valido(nro):
            r.error("cliente.nro_doc", "El CUIT/CUIL del cliente no es válido.", "Verificá el número con el cliente.")
        if tipo_doc == fiscal.DOC_DNI and not fiscal.dni_valido(nro):
            r.error("cliente.nro_doc", "El DNI del cliente no es válido.", "Verificá el número con el cliente.")
        if tipo_doc is not None and tipo_doc != fiscal.DOC_SIN_IDENTIFICAR and not nro:
            r.error("cliente.nro_doc", "Se indicó tipo de documento pero no el número.", "Completá el número.")

    if r.tipo is not None:
        if tipo_doc is None or (tipo_doc == fiscal.DOC_SIN_IDENTIFICAR) or not nro:
            r.doc_tipo, r.doc_nro = fiscal.DOC_SIN_IDENTIFICAR, "0"
        else:
            r.doc_tipo, r.doc_nro = tipo_doc, nro

    # Modo automático: reglas adicionales
    if automatico:
        au = cfg.automatico
        if venta["canal"] not in au.canales:
            r.error("automatico.canal", f"Canal {venta['canal']} no habilitado para emisión automática.",
                    "Emitir manualmente.")
        if r.calculo and au.importe_maximo and r.calculo.imp_total > au.importe_maximo:
            r.error("automatico.importe", "Supera el importe máximo para emisión automática.", "Emitir manualmente.")
    return r
