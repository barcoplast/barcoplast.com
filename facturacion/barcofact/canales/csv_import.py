"""Importación de ventas desde CSV (una fila por ítem).

Columnas: canal,pedido,fecha,estado_pago,cliente_nombre,tipo_doc,nro_doc,condicion_iva,
domicilio,email,sku,descripcion,cantidad,precio_unitario_final,descuento,alicuota,envio,
descuento_general,total_informado

- precio_unitario_final, descuento, envio: CON IVA incluido.
- envio y descuento_general se toman de la primera fila del pedido.
- tipo_doc: CUIT | CUIL | DNI | CDI | (vacío = sin identificar).
- condicion_iva: «Responsable Inscripto», «Monotributo», «Consumidor Final», «Exento»...
"""
from __future__ import annotations

import csv
import io

from .. import db, importes
from ..catalogo import Catalogo

DOCS = {"cuit": 80, "cuil": 86, "cdi": 87, "dni": 96, "pasaporte": 94, "": 99, "99": 99}


def leer(texto: str, catalogo: Catalogo) -> tuple[list[dict], list[str]]:
    pedidos: dict[tuple[str, str], dict] = {}
    errores: list[str] = []
    lector = csv.DictReader(io.StringIO(texto.lstrip("﻿")))
    for n, f in enumerate(lector, 2):
        f = {k.strip(): (v or "").strip() for k, v in f.items() if k}
        canal = (f.get("canal") or "manual").lower()
        pedido = f.get("pedido", "")
        if not pedido:
            errores.append(f"Fila {n}: falta el número de pedido.")
            continue
        clave = (canal, pedido)
        if clave not in pedidos:
            tdoc = f.get("tipo_doc", "").lower()
            if tdoc not in DOCS and not tdoc.isdigit():
                errores.append(f"Fila {n}: tipo de documento desconocido «{f.get('tipo_doc')}».")
            pedidos[clave] = {
                "canal": canal, "pedido_externo": pedido, "fecha_venta": f.get("fecha", ""),
                "estado_pago": f.get("estado_pago") or "desconocido",
                "cliente": {"nombre": f.get("cliente_nombre", ""),
                            "tipo_doc": DOCS.get(tdoc, int(tdoc) if tdoc.isdigit() else None),
                            "nro_doc": f.get("nro_doc", ""), "condicion_iva": f.get("condicion_iva", ""),
                            "domicilio": f.get("domicilio", ""), "email": f.get("email", "")},
                "items": [], "descuento_general": f.get("descuento_general") or "0",
                "total_informado": f.get("total_informado") or None,
            }
            if f.get("envio") and importes.dec(f["envio"]) > 0:
                prod = catalogo.por_sku("ENVIO")
                pedidos[clave]["_envio"] = catalogo.completar_item(
                    {"descripcion": "Envío", "cantidad": "1", "precio_unitario_final": f["envio"],
                     "es_envio": True, "alicuota": ""}, prod)
        prod = catalogo.por_sku(f.get("sku", "")) or (catalogo.parecido(f["descripcion"]) if f.get("descripcion") and not f.get("sku") else None)
        item = {"descripcion": f.get("descripcion") or (prod.nombre if prod else ""), "sku": f.get("sku", ""),
                "cantidad": f.get("cantidad", "1"), "precio_unitario_final": f.get("precio_unitario_final", ""),
                "descuento_final": f.get("descuento") or "0", "alicuota": f.get("alicuota", "")}
        pedidos[clave]["items"].append(catalogo.completar_item(item, prod))
    resultado = []
    for p in pedidos.values():
        if "_envio" in p:
            p["items"].append(p.pop("_envio"))
        resultado.append(p)
    return resultado, errores


def importar(con, texto: str, catalogo: Catalogo, usuario: str) -> dict:
    pedidos, errores = leer(texto, catalogo)
    nuevos, repetidos = [], []
    for p in pedidos:
        vid, creado = db.guardar_venta(con, p, usuario)
        (nuevos if creado else repetidos).append(f"{p['canal']} {p['pedido_externo']}")
    return {"nuevos": nuevos, "repetidos": repetidos, "errores": errores}
