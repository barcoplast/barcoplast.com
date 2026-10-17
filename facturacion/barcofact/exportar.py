"""Exportaciones para el contador."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

from . import fiscal

ALICS = ["21", "10.5", "27", "5", "2.5", "0"]


def comprobantes_autorizados(con, desde: str, hasta: str) -> list[dict]:
    """desde/hasta en AAAA-MM-DD (fecha del comprobante)."""
    return [dict(r) for r in con.execute(
        "SELECT c.*, v.canal, v.pedido_externo FROM comprobantes c JOIN ventas v ON v.id=c.venta_id "
        "WHERE c.estado='autorizado' AND c.fecha_cbte BETWEEN ? AND ? ORDER BY c.fecha_cbte, c.tipo, c.numero",
        (desde.replace("-", ""), hasta.replace("-", "")))]


def csv_contador(con, desde: str, hasta: str) -> str:
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    cab = ["fecha", "tipo_cod", "tipo", "punto_venta", "numero", "cae", "cae_vto", "doc_tipo", "doc_nro",
           "receptor", "condicion_iva_receptor"]
    for a in ALICS:
        cab += [f"neto_{a}", f"iva_{a}"]
    cab += ["exento", "no_gravado", "total", "canal", "pedido", "entorno", "simulado", "comprobante_asociado"]
    w.writerow(cab)
    for c in comprobantes_autorizados(con, desde, hasta):
        imp = json.loads(c["importes_json"])
        det = json.loads(c["detalle_json"])
        signo = Decimal("-1") if fiscal.TIPOS_CBTE[c["tipo"]][1] == "NOTA DE CRÉDITO" else Decimal("1")
        f = c["fecha_cbte"]
        fila = [f"{f[0:4]}-{f[4:6]}-{f[6:8]}", c["tipo"], fiscal.nombre_tipo(c["tipo"]), c["pto_vta"], c["numero"],
                c["cae"], c["cae_vto"], fiscal.TIPOS_DOC.get(c["doc_tipo"], c["doc_tipo"]), c["doc_nro"],
                det["receptor"]["nombre"], fiscal.CONDICIONES_IVA.get(c["cond_iva_receptor"], "")]
        grupos = {str(Decimal(g["alicuota"]).normalize()): g for g in imp["grupos_iva"]}
        for a in ALICS:
            g = grupos.get(str(Decimal(a).normalize()))
            fila += [_n(signo * Decimal(g["base"])) if g else "0,00", _n(signo * Decimal(g["importe"])) if g else "0,00"]
        a = det.get("asociado")
        fila += [_n(signo * Decimal(imp["imp_op_ex"])), _n(signo * Decimal(imp["imp_tot_conc"])),
                 _n(signo * Decimal(imp["imp_total"])), c["canal"], c["pedido_externo"], c["entorno"],
                 "SI" if c["simulado"] else "NO",
                 f"{a['tipo']}-{a['pto_vta']:05d}-{a['numero']:08d}" if a else ""]
        w.writerow(fila)
    return out.getvalue()


def _n(d: Decimal) -> str:
    return f"{d:.2f}".replace(".", ",")


def zip_pdfs(con, desde: str, hasta: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"comprobantes_{desde}_{hasta}.csv", csv_contador(con, desde, hasta))
        for c in comprobantes_autorizados(con, desde, hasta):
            if c["pdf_path"] and Path(c["pdf_path"]).exists():
                z.write(c["pdf_path"], Path(c["pdf_path"]).name)
    return buf.getvalue()
