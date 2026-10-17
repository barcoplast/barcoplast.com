"""Representación impresa (PDF) del comprobante autorizado, con QR (RG 4892)."""
from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from . import fiscal
from .config import URL_QR, Config


def fmt(x) -> str:
    """1234567.8 -> 1.234.567,80"""
    d = Decimal(str(x)).quantize(Decimal("0.01"))
    s = f"{d:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fecha_txt(aaaammdd: str) -> str:
    return f"{aaaammdd[6:8]}/{aaaammdd[4:6]}/{aaaammdd[0:4]}" if aaaammdd and len(aaaammdd) == 8 else aaaammdd or ""


def url_qr(c: dict) -> str:
    f = c["fecha_cbte"]
    partes = [
        '"ver":1', f'"fecha":"{f[0:4]}-{f[4:6]}-{f[6:8]}"', f'"cuit":{int(c["cuit"])}',
        f'"ptoVta":{int(c["pto_vta"])}', f'"tipoCmp":{int(c["tipo"])}', f'"nroCmp":{int(c["numero"])}',
        f'"importe":{Decimal(json.loads(c["importes_json"])["imp_total"])}', '"moneda":"PES"', '"ctz":1',
    ]
    if int(c["doc_tipo"]) != fiscal.DOC_SIN_IDENTIFICAR:
        partes += [f'"tipoDocRec":{int(c["doc_tipo"])}', f'"nroDocRec":{int(c["doc_nro"])}']
    partes += ['"tipoCodAut":"E"', f'"codAut":{int(c["cae"])}']
    datos = "{" + ",".join(partes) + "}"
    return URL_QR + "?p=" + base64.b64encode(datos.encode()).decode()


def nombre_archivo(c: dict) -> str:
    return f"{c['cuit']}_{int(c['tipo']):03d}_{int(c['pto_vta']):05d}_{int(c['numero']):08d}.pdf"


def generar(cfg: Config, c: dict, destino: Path | None = None) -> Path:
    if c["estado"] != "autorizado" or not c["cae"]:
        raise ValueError("Sólo se genera el comprobante de una autorización válida (con CAE).")
    det = json.loads(c["detalle_json"])
    imp = json.loads(c["importes_json"])
    tipo = int(c["tipo"])
    letra = fiscal.letra(tipo)
    clase_a = fiscal.es_clase_a_o_m(tipo)
    destino = destino or (cfg.dir_comprobantes / nombre_archivo(c))

    W, H = A4
    p = canvas.Canvas(str(destino), pagesize=A4)
    p.setTitle(f"{fiscal.nombre_tipo(tipo)} {c['pto_vta']:05d}-{c['numero']:08d}")

    # Marca de agua de entorno
    if c["entorno"] != "produccion" or c["simulado"]:
        p.saveState()
        p.setFillColorRGB(0.85, 0.1, 0.1, alpha=0.18)
        p.setFont("Helvetica-Bold", 38)
        p.translate(W / 2, H / 2)
        p.rotate(35)
        p.drawCentredString(0, 20, "HOMOLOGACIÓN" if not c["simulado"] else "SIMULADO")
        p.drawCentredString(0, -30, "SIN VALIDEZ FISCAL")
        p.restoreState()

    m = 15 * mm
    y = H - m
    # Encabezado
    p.rect(m, y - 45 * mm, W - 2 * m, 45 * mm)
    p.rect(W / 2 - 9 * mm, y - 16 * mm, 18 * mm, 16 * mm)
    p.setFont("Helvetica-Bold", 26)
    p.drawCentredString(W / 2, y - 11 * mm, letra)
    p.setFont("Helvetica", 7)
    p.drawCentredString(W / 2, y - 14.5 * mm, f"COD. {tipo:02d}")
    p.line(W / 2, y - 16 * mm, W / 2, y - 45 * mm)
    p.setFont("Helvetica-Bold", 9)
    p.drawCentredString(W / 2, y + 2 * mm, "ORIGINAL")

    e = det["emisor"]
    p.setFont("Helvetica-Bold", 13)
    p.drawString(m + 4 * mm, y - 10 * mm, e["razon_social"][:40])
    p.setFont("Helvetica", 8.5)
    p.drawString(m + 4 * mm, y - 20 * mm, f"Domicilio comercial: {e['domicilio'][:60]}")
    p.drawString(m + 4 * mm, y - 25 * mm, f"Condición frente al IVA: {e['condicion_iva']}")

    x2 = W / 2 + 12 * mm
    p.setFont("Helvetica-Bold", 13)
    p.drawString(x2, y - 10 * mm, fiscal.TIPOS_CBTE[tipo][1])
    p.setFont("Helvetica", 9)
    p.drawString(x2, y - 17 * mm, f"Punto de venta: {int(c['pto_vta']):05d}   Comp. Nro: {int(c['numero']):08d}")
    p.drawString(x2, y - 23 * mm, f"Fecha de emisión: {fecha_txt(c['fecha_cbte'])}")
    p.drawString(x2, y - 29 * mm, f"CUIT: {e['cuit']}")
    p.drawString(x2, y - 35 * mm, f"Ingresos Brutos: {e['ingresos_brutos']}")
    p.drawString(x2, y - 41 * mm, f"Inicio de actividades: {e['inicio_actividades']}")

    # Receptor
    y -= 50 * mm
    rc = det["receptor"]
    p.rect(m, y - 20 * mm, W - 2 * m, 20 * mm)
    p.setFont("Helvetica", 9)
    doc_txt = fiscal.TIPOS_DOC.get(int(c["doc_tipo"]), "")
    if int(c["doc_tipo"]) == fiscal.DOC_SIN_IDENTIFICAR:
        doc_linea = "Documento: —"
    else:
        doc_linea = f"{doc_txt}: {c['doc_nro']}"
    p.drawString(m + 4 * mm, y - 6 * mm, f"{doc_linea}")
    p.drawString(m + 70 * mm, y - 6 * mm, f"Apellido y nombre / Razón social: {rc['nombre'][:45]}")
    p.drawString(m + 4 * mm, y - 12 * mm, f"Condición frente al IVA: {fiscal.CONDICIONES_IVA.get(int(c['cond_iva_receptor']), '')}")
    p.drawString(m + 4 * mm, y - 18 * mm, f"Domicilio: {(rc.get('domicilio') or '—')[:90]}")
    if det.get("asociado"):
        a = det["asociado"]
        p.drawString(m + 110 * mm, y - 12 * mm,
                     f"Comprobante asociado: {fiscal.nombre_tipo(a['tipo'])} {a['pto_vta']:05d}-{a['numero']:08d}")

    # Ítems
    y -= 27 * mm
    p.setFont("Helvetica-Bold", 8)
    if clase_a:
        cols = [("Descripción", m + 2 * mm), ("Cant.", m + 92 * mm), ("P. unit. neto", m + 105 * mm),
                ("Bonif.", m + 127 * mm), ("Subtotal neto", m + 140 * mm), ("IVA %", m + 162 * mm)]
    else:
        cols = [("Descripción", m + 2 * mm), ("Cant.", m + 110 * mm), ("P. unitario", m + 125 * mm),
                ("Bonif.", m + 145 * mm), ("Subtotal", m + 160 * mm)]
    p.setFillColorRGB(0.92, 0.92, 0.92)
    p.rect(m, y - 2 * mm, W - 2 * m, 6 * mm, fill=1, stroke=0)
    p.setFillColorRGB(0, 0, 0)
    for t, x in cols:
        p.drawString(x, y, t)
    p.setFont("Helvetica", 8)
    for ln in det["lineas"]:
        y -= 6 * mm
        if y < 80 * mm:
            p.showPage()
            y = H - m
            p.setFont("Helvetica", 8)
        p.drawString(cols[0][1], y, ln["descripcion"][:55 if clase_a else 65])
        p.drawRightString(cols[1][1] + 10 * mm, y, ln["cantidad"].rstrip("0").rstrip(".") if "." in ln["cantidad"] else ln["cantidad"])
        if clase_a:
            p.drawRightString(cols[2][1] + 20 * mm, y, fmt(ln["precio_unitario_neto"]))
            desc_neto = Decimal(ln["precio_unitario_neto"]) * Decimal(ln["cantidad"]) - Decimal(ln["neto"])
            p.drawRightString(cols[3][1] + 11 * mm, y, fmt(max(desc_neto, Decimal("0"))))
            p.drawRightString(cols[4][1] + 20 * mm, y, fmt(ln["neto"]))
            p.drawRightString(cols[5][1] + 12 * mm, y, ln["alicuota"] if ln["alicuota"] not in ("exento", "no_gravado") else ln["alicuota"])
        else:
            p.drawRightString(cols[2][1] + 18 * mm, y, fmt(ln["precio_unitario_final"]))
            p.drawRightString(cols[3][1] + 12 * mm, y, fmt(ln["descuento_final"]))
            p.drawRightString(cols[4][1] + 20 * mm, y, fmt(ln["total_final"]))

    # Totales
    yt = 75 * mm
    p.rect(m, 20 * mm, W - 2 * m, yt - 18 * mm)
    xr = W - m - 4 * mm
    p.setFont("Helvetica", 9)
    fila = yt - 6 * mm
    if clase_a:
        p.drawRightString(xr - 35 * mm, fila, "Importe neto gravado: $")
        p.drawRightString(xr, fila, fmt(imp["imp_neto"]))
        for g in imp["grupos_iva"]:
            fila -= 5 * mm
            p.drawRightString(xr - 35 * mm, fila, f"IVA {g['alicuota']}%: $")
            p.drawRightString(xr, fila, fmt(g["importe"]))
        if Decimal(imp["imp_op_ex"]):
            fila -= 5 * mm
            p.drawRightString(xr - 35 * mm, fila, "Importe exento: $")
            p.drawRightString(xr, fila, fmt(imp["imp_op_ex"]))
    fila -= 7 * mm
    p.setFont("Helvetica-Bold", 11)
    p.drawRightString(xr - 35 * mm, fila, "Importe total: $")
    p.drawRightString(xr, fila, fmt(imp["imp_total"]))

    if not clase_a:
        p.setFont("Helvetica-Bold", 8)
        p.drawString(m + 4 * mm, yt - 6 * mm, "Régimen de Transparencia Fiscal al Consumidor (Ley 27.743)")
        p.setFont("Helvetica", 8)
        p.drawString(m + 4 * mm, yt - 11 * mm, f"IVA Contenido: $ {fmt(imp['imp_iva'])}")
        p.drawString(m + 4 * mm, yt - 16 * mm, "Otros Impuestos Nacionales Indirectos: $ 0,00")

    # CAE + QR
    qr = QrCodeWidget(url_qr(c))
    b = qr.getBounds()
    tam = 32 * mm
    d = Drawing(tam, tam, transform=[tam / (b[2] - b[0]), 0, 0, tam / (b[3] - b[1]), 0, 0])
    d.add(qr)
    renderPDF.draw(d, p, m + 2 * mm, 21 * mm)
    p.setFont("Helvetica-Bold", 9)
    p.drawString(m + 38 * mm, 40 * mm, "Comprobante autorizado")
    p.setFont("Helvetica", 9)
    p.drawString(m + 38 * mm, 34 * mm, f"CAE N°: {c['cae']}")
    p.drawString(m + 38 * mm, 28 * mm, f"Fecha de vto. de CAE: {fecha_txt(c['cae_vto'])}")
    p.setFont("Helvetica", 6.5)
    p.drawString(m + 38 * mm, 23 * mm, f"Pedido {det.get('canal', '')} {det.get('pedido', '')}")
    p.showPage()
    p.save()
    return destino
