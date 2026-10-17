"""Pruebas SIMULADAS de canales, XML, firma, backup y pantalla."""
import http.cookiejar
import json
import re
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from barcofact import backup, db
from barcofact.arca import wsaa, wsfe
from barcofact.canales import csv_import, mercadolibre, whatsapp
from barcofact.catalogo import Catalogo
from tests.base import CUIT_CLIENTE, Base

CAT = Path(__file__).parents[1] / "catalogo.ejemplo.csv"


class TestCanales(Base):
    def test_csv_importa_y_no_duplica(self):
        texto = ("canal,pedido,fecha,estado_pago,cliente_nombre,tipo_doc,nro_doc,condicion_iva,domicilio,email,sku,descripcion,"
                 "cantidad,precio_unitario_final,descuento,alicuota,envio,descuento_general,total_informado\n"
                 f"csv,100,2026-10-01,acreditado,Taller SRL,CUIT,{CUIT_CLIENTE},Responsable Inscripto,Calle 1,,GELCOAT,Gelcoat 1kg,1,15000,,21,3000,,18000\n"
                 "csv,100,,,,,,,,,CAT-MEK,Catalizador,1,0,,,,,\n"
                 "csv,101,2026-10-01,acreditado,,,,Consumidor Final,,,,Diluyente,2,5000,,21,,,\n")
        cat = Catalogo.cargar(CAT)
        r = csv_import.importar(self.con, texto, cat, "ana")
        self.assertEqual(len(r["nuevos"]), 2)
        r2 = csv_import.importar(self.con, texto, cat, "ana")
        self.assertEqual((len(r2["nuevos"]), len(r2["repetidos"])), (0, 2))
        v = db.buscar_ventas(self.con, texto="100")[0]
        self.assertTrue(v["items"][-1]["es_envio"])
        self.assertFalse(v["items"][0]["alicuota_confirmada"])  # catálogo de ejemplo sin confirmar

    def test_whatsapp_genera_borrador_sin_inventar(self):
        msg = "Hola! quiero 2 x resina poliester comun y 1 catalizador mek. Soy responsable inscripto"
        b = whatsapp.borrador(msg, Catalogo.cargar(CAT), "+54 9 11 0000")
        self.assertEqual(b["estado"], "borrador")
        self.assertEqual(len(b["items"]), 2)
        self.assertEqual(b["cliente"]["condicion_iva"], "")  # no deduce la condición fiscal del texto libre
        self.assertEqual(b["cliente"]["nro_doc"], "")
        self.assertTrue(all(i["precio_unitario_final"] == "" for i in b["items"]))
        b["pedido_externo"] = "WA-1"
        vid, _ = db.guardar_venta(self.con, b, "ana")
        self.assertFalse(self.f.vista_previa(vid)["validacion"].emitible)

    def test_mercadolibre_factura_lo_pagado_por_el_comprador(self):
        orden = {"id": 2000001, "status": "paid", "date_created": "2026-10-10T10:00:00.000-03:00", "pack_id": None,
                 "order_items": [{"item": {"id": "MLA1", "title": "Resina Epoxi Bijou 750 ml", "seller_sku": "RES-EPX-BIJOU"},
                                  "quantity": 2, "unit_price": 20000, "sale_fee": 3000}],
                 "payments": [{"status": "approved", "transaction_amount": 40000, "shipping_cost": 5000,
                               "total_paid_amount": 45000}],
                 "buyer": {"nickname": "COMPRADOR"}}
        billing = {"billing_info": {"doc_type": "DNI", "doc_number": "30111222",
                                    "additional_info": [{"type": "FIRST_NAME", "value": "Juan"},
                                                        {"type": "LAST_NAME", "value": "Pérez"},
                                                        {"type": "TAXPAYER_TYPE_ID", "value": "Consumidor Final"}]}}
        d = mercadolibre.venta_desde_pedido(orden, billing, [], Catalogo.cargar(CAT))
        self.assertEqual(d["total_informado"], "45000.00")  # no 40000-6000 de comisiones
        self.assertEqual(d["datos_canal"]["comision_total"], "6000")
        vid, _ = db.guardar_venta(self.con, d, "ana")
        r = self.f.vista_previa(vid)["validacion"]
        self.assertEqual(r.calculo.imp_total, D("45000.00"))
        self.assertEqual(r.tipo, 6)

    def test_mercadolibre_ya_facturada_o_sin_verificar(self):
        orden = {"id": 2000002, "status": "paid", "date_created": "2026-10-10", "order_items": [], "payments": []}
        d = mercadolibre.venta_desde_pedido(orden, None, [{"id": "doc1"}], Catalogo.cargar(CAT))
        self.assertEqual(d["estado"], "facturada_externamente")
        d2 = mercadolibre.venta_desde_pedido(dict(orden, id=2000003), None, None, Catalogo.cargar(CAT), "HTTP 403")
        vid, _ = db.guardar_venta(self.con, d2, "ana")
        self.assertIn("mercadolibre.facturacion", {p.campo for p in self.f.vista_previa(vid)["validacion"].bloqueantes})


class TestArcaXmlYFirma(Base):
    def test_orden_xml_fecaesolicitar(self):
        s = {"concepto": 1, "doc_tipo": 99, "doc_nro": "0", "numero": 8, "fecha": "20261017", "imp_total": "121.00",
             "imp_tot_conc": "0", "imp_neto": "100.00", "imp_op_ex": "0", "imp_trib": "0", "imp_iva": "21.00",
             "mon_id": "PES", "mon_cotiz": "1", "cond_iva_receptor": 5,
             "iva": [{"id": 5, "base": "100.00", "importe": "21.00"}]}
        x = wsfe.xml_detalle(s)
        tags = re.findall(r"<ar:(\w+)>", x)
        orden = ["Concepto", "DocTipo", "DocNro", "CbteDesde", "CbteHasta", "CbteFch", "ImpTotal", "ImpTotConc",
                 "ImpNeto", "ImpOpEx", "ImpTrib", "ImpIVA", "MonId", "MonCotiz", "CondicionIVAReceptorId", "Iva"]
        self.assertEqual([t for t in tags if t in orden], orden)

    def test_firma_cms_del_tra(self):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "k.pem", "-out",
                        d / "c.pem", "-days", "1", "-subj", "/CN=prueba"], check=True, capture_output=True)
        tra = wsaa.crear_tra("wsfe")
        cms = wsaa.firmar_cms(tra, d / "c.pem", d / "k.pem")
        (d / "cms.der").write_bytes(__import__("base64").b64decode(cms))
        out = subprocess.run(["openssl", "smime", "-verify", "-inform", "DER", "-in", d / "cms.der", "-noverify"],
                             capture_output=True, check=True).stdout
        self.assertEqual(out, tra)
        self.assertIn(b"<service>wsfe</service>", tra)


class TestBackup(Base):
    def test_backup_y_restauracion(self):
        vid = self.venta()
        self.f.emitir(vid, "ana")
        p = backup.crear(self.cfg)
        self.assertEqual(backup.verificar(p), {"integridad": "ok", "autorizados": 1})
        self.f.emitir(self.venta(pedido="P2"), "ana")
        with self.assertRaises(RuntimeError):
            backup.restaurar(self.cfg, p)  # perdería un comprobante autorizado
        self.con.close()
        backup.restaurar(self.cfg, p, forzar=True)
        self.con = db.conectar(self.cfg.db_path)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM comprobantes").fetchone()[0], 1)


class TestPantalla(Base):
    def test_flujo_web(self):
        from http.server import ThreadingHTTPServer
        from barcofact import usuarios, web
        usuarios.crear(self.con, "ana", "clave-segura-123")
        app = web.App(self.cfg)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.crear_handler(app))
        srv.app = app
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            html = op.open(base + "/").read().decode()
            self.assertIn("Ingresar", html)
            self.assertIn("SIMULADOR LOCAL", html)
            op.open(base + "/login", urllib.parse.urlencode({"usuario": "ana", "clave": "mala"}).encode())
            html = op.open(base + "/login", urllib.parse.urlencode({"usuario": "ana", "clave": "clave-segura-123"}).encode()).read().decode()
            self.assertIn("Pedidos", html)
            vid = self.venta()
            html = op.open(f"{base}/venta?id={vid}").read().decode()
            self.assertIn("Vista previa: FACTURA B", html)
            csrf = re.search(r'name=csrf value="([^"]+)"', html).group(1)
            # sin CSRF se rechaza
            with self.assertRaises(urllib.error.HTTPError):
                op.open(base + "/emitir", urllib.parse.urlencode({"id": vid}).encode())
            html = op.open(base + "/emitir", urllib.parse.urlencode({"id": vid, "csrf": csrf}).encode()).read().decode()
            self.assertIn("Autorizado. CAE", html)
            cid = db.comprobantes_de(self.con, vid)[0]["id"]
            pdf = op.open(f"{base}/pdf?id={cid}").read()
            self.assertTrue(pdf.startswith(b"%PDF"))
            hoy = datetime.now(timezone(timedelta(hours=-3))).date().isoformat()
            csv = op.open(f"{base}/descargar?desde={hoy}&hasta={hoy}&formato=csv").read().decode("utf-8-sig")
            self.assertIn("FACTURA B", csv)
            self.assertIn("20000,00", csv)
            html = op.open(base + "/?q=P-1").read().decode()
            self.assertIn("Facturada", html)
            for ruta in ("/nueva", "/importar", "/whatsapp", "/mercadolibre", "/exportar", "/sistema"):
                self.assertEqual(op.open(base + ruta).status, 200)
        finally:
            srv.shutdown()
