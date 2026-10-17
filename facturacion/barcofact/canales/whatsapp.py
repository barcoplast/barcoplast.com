"""Borradores de pedido a partir de un mensaje de WhatsApp pegado por el usuario.

Sólo genera un BORRADOR: nunca se emite sin que el usuario confirme cada dato.
No completa ni deduce CUIT, DNI, domicilio ni condición frente al IVA: sólo toma
números que el mensaje identifica explícitamente («DNI 30123456», «CUIT 20-...»).
"""
from __future__ import annotations

import re
from datetime import date

from .. import fiscal
from ..catalogo import Catalogo

RE_CANT = re.compile(r"(?<![\d-])(\d+(?:[.,]\d+)?)\s*(?:x|u\.?|unidades?|de)?\s+([^\d].*)$", re.I)
RE_CANT_FIN = re.compile(r"^\s*(?:[-•*]\s*)?(.+?)\s+x\s*(\d+(?:[.,]\d+)?)\s*$", re.I)
RE_SEGMENTOS = re.compile(r"[\n;]|,\s|\.\s|\s+y\s+", re.I)
RE_CUIT = re.compile(r"\bcuit\s*:?\s*(\d{2}-?\d{8}-?\d)\b", re.I)
RE_DNI = re.compile(r"\bdni\s*:?\s*(\d{1,2}\.?\d{3}\.?\d{3})\b", re.I)


def borrador(mensaje: str, catalogo: Catalogo, telefono: str = "") -> dict:
    items, sin_identificar = [], []
    sin_docs = RE_DNI.sub(" ", RE_CUIT.sub(" ", mensaje))
    for linea in RE_SEGMENTOS.split(sin_docs):
        if not linea.strip():
            continue
        m = RE_CANT.search(linea)
        if m:
            cant, texto = m.group(1), m.group(2)
        else:
            m = RE_CANT_FIN.match(linea)
            if not m:
                continue
            cant, texto = m.group(2), m.group(1)
        prod = catalogo.parecido(texto)
        if prod is None:
            sin_identificar.append(linea.strip())
            continue
        items.append(catalogo.completar_item({"descripcion": prod.nombre, "sku": prod.sku,
                                              "cantidad": cant.replace(",", "."), "precio_unitario_final": "",
                                              "descuento_final": "0", "alicuota": ""}, prod))
    cliente = {"nombre": "", "tipo_doc": None, "nro_doc": "", "condicion_iva": "", "domicilio": "",
               "telefono": telefono}
    c = RE_CUIT.search(mensaje)
    d = RE_DNI.search(mensaje)
    if c and fiscal.cuit_valido(c.group(1)):
        cliente.update(tipo_doc=fiscal.DOC_CUIT, nro_doc=re.sub(r"\D", "", c.group(1)))
    elif d:
        cliente.update(tipo_doc=fiscal.DOC_DNI, nro_doc=re.sub(r"\D", "", d.group(1)))
    notas = "Borrador desde WhatsApp. Completar precios, datos fiscales del cliente y confirmar."
    if sin_identificar:
        notas += " Líneas no reconocidas: " + " | ".join(sin_identificar)
    return {"canal": "whatsapp", "pedido_externo": "", "fecha_venta": date.today().isoformat(),
            "estado": "borrador", "estado_pago": "desconocido", "cliente": cliente, "items": items,
            "notas": notas, "datos_canal": {"mensaje_original": mensaje}}
