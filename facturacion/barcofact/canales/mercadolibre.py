"""Mercado Libre: lectura de pedidos con la API oficial (OAuth 2.0).

- Credenciales (client_id, client_secret) y tokens se guardan en
  <datos>/mercadolibre.json con permisos 600, nunca en el repositorio.
- El importe a facturar es lo que pagó el comprador (ítems + envío a su cargo).
  Nunca se usa el neto recibido después de comisiones de Mercado Libre.
- Antes de habilitar la factura se verifica si el pedido ya tiene un documento
  fiscal cargado en Mercado Libre (otra herramienta o el Facturador de ML).
- Una cancelación NO genera nota de crédito automáticamente: se marca para revisión.

ESTADO: no probado contra la API real (requiere una aplicación registrada en
developers.mercadolibre.com.ar y la autorización del vendedor).
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from decimal import Decimal
from pathlib import Path

import requests

from .. import db
from ..catalogo import Catalogo
from ..importes import r2

API = "https://api.mercadolibre.com"
AUTH = "https://auth.mercadolibre.com.ar/authorization"

# Equivalencias de condición fiscal informada por el comprador en ML
TAXPAYER = {
    "iva responsable inscripto": "Responsable Inscripto",
    "responsable inscripto": "Responsable Inscripto",
    "monotributo": "Monotributo", "iva monotributista": "Monotributo", "monotributista": "Monotributo",
    "consumidor final": "Consumidor Final",
    "iva exento": "Exento", "exento": "Exento",
}
DOC_ML = {"CUIT": 80, "CUIL": 86, "DNI": 96, "CDI": 87}


class ErrorML(Exception):
    pass


class ClienteML:
    def __init__(self, archivo: Path):
        self.archivo = archivo
        if not archivo.exists():
            raise ErrorML("Mercado Libre no está configurado. Ejecutá `barcofact ml-configurar`.")
        self.datos = json.loads(archivo.read_text())
        self.s = requests.Session()

    @staticmethod
    def guardar(archivo: Path, datos: dict) -> None:
        tmp = archivo.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(datos, f)
        os.replace(tmp, archivo)

    @staticmethod
    def url_autorizacion(client_id: str, redirect_uri: str) -> str:
        q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri})
        return f"{AUTH}?{q}"

    @classmethod
    def canjear_codigo(cls, archivo: Path, client_id, client_secret, redirect_uri, code) -> None:
        r = requests.post(f"{API}/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
            "code": code, "redirect_uri": redirect_uri}, timeout=30)
        if r.status_code != 200:
            raise ErrorML(f"Mercado Libre rechazó el código (HTTP {r.status_code}).")
        t = r.json()
        cls.guardar(archivo, {"client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
                              "access_token": t["access_token"], "refresh_token": t["refresh_token"],
                              "expira": time.time() + int(t.get("expires_in", 21600)), "user_id": t.get("user_id")})

    def _token(self) -> str:
        if time.time() > self.datos.get("expira", 0) - 300:
            r = self.s.post(f"{API}/oauth/token", data={
                "grant_type": "refresh_token", "client_id": self.datos["client_id"],
                "client_secret": self.datos["client_secret"], "refresh_token": self.datos["refresh_token"]}, timeout=30)
            if r.status_code != 200:
                raise ErrorML("No se pudo renovar el acceso a Mercado Libre. Volvé a autorizar con `barcofact ml-configurar`.")
            t = r.json()
            self.datos.update(access_token=t["access_token"], refresh_token=t["refresh_token"],
                              expira=time.time() + int(t.get("expires_in", 21600)))
            self.guardar(self.archivo, self.datos)
        return self.datos["access_token"]

    def get(self, ruta: str, params=None, permitir_404=False):
        r = self.s.get(f"{API}{ruta}", params=params, timeout=30,
                       headers={"Authorization": f"Bearer {self._token()}"})
        if r.status_code == 404 and permitir_404:
            return None
        if r.status_code != 200:
            raise ErrorML(f"GET {ruta}: HTTP {r.status_code}")
        return r.json()

    # ---------------------------------------------------------------- pedidos
    def pedidos_recientes(self, desde_iso: str, limite: int = 200) -> list[dict]:
        res, offset = [], 0
        while offset < limite:
            page = self.get("/orders/search", {"seller": self.datos["user_id"], "sort": "date_desc",
                                               "order.date_created.from": desde_iso, "offset": offset, "limit": 50})
            res += page.get("results", [])
            if offset + 50 >= page.get("paging", {}).get("total", 0):
                break
            offset += 50
        return res

    def datos_facturacion(self, order_id) -> dict | None:
        return self.get(f"/orders/{order_id}/billing_info", permitir_404=True)

    def documentos_fiscales(self, pack_id) -> list:
        """Documentos fiscales ya adjuntos al pedido/pack en Mercado Libre."""
        r = self.get(f"/packs/{pack_id}/fiscal_documents", permitir_404=True)
        if not r:
            return []
        return r.get("fiscal_documents", r if isinstance(r, list) else [])


def _info_adicional(bi: dict) -> dict:
    out = {}
    for a in (bi or {}).get("additional_info", []) or []:
        out[a.get("type", "")] = a.get("value", "")
    return out


def venta_desde_pedido(o: dict, billing: dict | None, docs_fiscales: list | None, catalogo: Catalogo,
                       verificacion_error: str | None = None) -> dict:
    items = []
    for it in o.get("order_items", []):
        item_id = it.get("item", {}).get("id", "")
        sku = it.get("item", {}).get("seller_sku") or ""
        prod = catalogo.por_sku(sku) or catalogo.por_item_ml(item_id)
        items.append(catalogo.completar_item({
            "descripcion": it.get("item", {}).get("title", ""), "sku": sku or (prod.sku if prod else ""),
            "cantidad": str(it.get("quantity", 0)), "precio_unitario_final": str(Decimal(str(it.get("unit_price", 0)))),
            "descuento_final": "0", "alicuota": "", "ml_item_id": item_id}, prod))

    pagos = [p for p in o.get("payments", []) if p.get("status") == "approved"]
    envio = sum((Decimal(str(p.get("shipping_cost") or 0)) for p in pagos), Decimal("0"))
    if envio > 0:
        items.append(catalogo.completar_item({"descripcion": "Envío", "cantidad": "1",
                                              "precio_unitario_final": str(r2(envio)), "es_envio": True,
                                              "alicuota": ""}, catalogo.por_sku("ENVIO")))
    # Lo que pagó el comprador (no el neto de comisiones).
    pagado = sum((Decimal(str(p.get("total_paid_amount") or p.get("transaction_amount") or 0)) for p in pagos),
                 Decimal("0"))

    bi = (billing or {}).get("billing_info", billing or {}) or {}
    extra = _info_adicional(bi)
    nombre = extra.get("BUSINESS_NAME") or " ".join(x for x in (extra.get("FIRST_NAME"), extra.get("LAST_NAME")) if x)
    domicilio = ", ".join(x for x in (" ".join(y for y in (extra.get("STREET_NAME"), extra.get("STREET_NUMBER")) if y),
                                      extra.get("CITY_NAME"), extra.get("STATE_NAME"), extra.get("ZIP_CODE")) if x)
    cond_txt = (extra.get("TAXPAYER_TYPE_ID") or "").strip().lower()

    estado_op = "cancelada" if o.get("status") == "cancelled" else "activa"
    if docs_fiscales:
        verif = "con_comprobante"
    elif verificacion_error:
        verif = f"no_verificada: {verificacion_error}"
    else:
        verif = "sin_comprobante"
    return {
        "canal": "mercadolibre", "pedido_externo": str(o["id"]),
        "fecha_venta": (o.get("date_closed") or o.get("date_created") or "")[:10],
        "estado_pago": "acreditado" if o.get("status") == "paid" and pagos else "pendiente",
        "estado_operacion": estado_op,
        "estado": "facturada_externamente" if docs_fiscales else "pendiente_datos",
        "cliente": {"nombre": nombre, "tipo_doc": DOC_ML.get((bi.get("doc_type") or "").upper()),
                    "nro_doc": bi.get("doc_number") or "", "condicion_iva": TAXPAYER.get(cond_txt, ""),
                    "domicilio": domicilio, "ml_nickname": o.get("buyer", {}).get("nickname", "")},
        "items": items, "total_informado": str(r2(pagado)) if pagos else None,
        "facturada_externamente": f"Mercado Libre: documento fiscal adjunto ({len(docs_fiscales)})" if docs_fiscales else None,
        "datos_canal": {"pack_id": o.get("pack_id"), "status": o.get("status"), "tags": o.get("tags", []),
                        "verificacion_facturacion_ml": verif,
                        "cupon": o.get("coupon", {}), "comision_total": str(sum(
                            Decimal(str(i.get("sale_fee") or 0)) * int(i.get("quantity") or 0)
                            for i in o.get("order_items", [])))},
    }


def sincronizar(con, cli: ClienteML, catalogo: Catalogo, desde_iso: str, usuario: str) -> dict:
    nuevos, actualizados, revisar = [], [], []
    for o in cli.pedidos_recientes(desde_iso):
        pack = o.get("pack_id") or o["id"]
        err = None
        try:
            docs = cli.documentos_fiscales(pack)
        except ErrorML as e:
            docs, err = None, str(e)
        try:
            billing = cli.datos_facturacion(o["id"])
        except ErrorML:
            billing = None
        datos = venta_desde_pedido(o, billing, docs, catalogo, err)
        existente = con.execute("SELECT id, estado FROM ventas WHERE canal='mercadolibre' AND pedido_externo=?",
                                (str(o["id"]),)).fetchone()
        if not existente:
            db.guardar_venta(con, datos, usuario)
            nuevos.append(str(o["id"]))
            continue
        vid, estado = existente["id"], existente["estado"]
        if datos["estado_operacion"] == "cancelada":
            con.execute("UPDATE ventas SET estado_operacion='cancelada', actualizado=? WHERE id=?", (db.ahora(), vid))
            if estado == "facturada":
                db.evento(con, usuario, "revisar_cancelacion", vid,
                          detalle="Pedido cancelado en ML con factura autorizada: evaluar nota de crédito (no automática).")
                revisar.append(str(o["id"]))
        if datos["facturada_externamente"] and estado not in ("facturada",):
            con.execute("UPDATE ventas SET facturada_externamente=?, estado='facturada_externamente', actualizado=? "
                        "WHERE id=?", (datos["facturada_externamente"], db.ahora(), vid))
            db.evento(con, usuario, "facturada_externamente", vid, detalle=datos["facturada_externamente"])
        actualizados.append(str(o["id"]))
    return {"nuevos": nuevos, "actualizados": actualizados, "revisar_cancelacion": revisar}
