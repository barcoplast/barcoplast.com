"""Pruebas SIMULADAS del flujo de emisión (no contactan a ARCA)."""
import base64
import json
import threading
import urllib.parse
from decimal import Decimal as D

from barcofact import db, fiscal
from barcofact.comprobante_pdf import url_qr
from barcofact.emision import EmisionBloqueada, ErrorEmision, Facturador
from tests.base import CUIT_CLIENTE, Base


class TestEmision(Base):
    def test_venta_iva_incluido_factura_b(self):
        vid = self.venta()
        self.assertEqual(db.venta(self.con, vid)["estado"], "lista")
        c = self.f.emitir(vid, "ana")
        self.assertEqual(c["estado"], "autorizado")
        self.assertEqual(c["tipo"], fiscal.FACTURA_B)
        self.assertEqual(c["numero"], 1)
        self.assertEqual(len(c["cae"]), 14)
        s = json.loads(c["solicitud_json"])
        self.assertEqual((s["imp_neto"], s["imp_iva"], s["imp_total"]), ("20000.00", "4200.00", "24200.00"))
        self.assertEqual(s["cond_iva_receptor"], fiscal.COND_CONSUMIDOR_FINAL)
        self.assertEqual(c["aprobado_por"], "ana")
        self.assertEqual(c["simulado"], 1)
        self.assertEqual(db.venta(self.con, vid)["estado"], "facturada")

    def test_pdf_y_qr_desde_autorizacion_valida(self):
        c = self.f.emitir(self.venta(), "ana")
        from pathlib import Path
        self.assertTrue(Path(c["pdf_path"]).read_bytes().startswith(b"%PDF"))
        q = urllib.parse.urlparse(url_qr(c))
        datos = json.loads(base64.b64decode(urllib.parse.parse_qs(q.query)["p"][0]))
        self.assertEqual(datos["codAut"], int(c["cae"]))
        self.assertEqual(datos["nroCmp"], 1)
        self.assertEqual(datos["tipoCmp"], 6)
        self.assertEqual(D(str(datos["importe"])), D("24200.00"))
        self.assertNotIn("nroDocRec", datos)  # consumidor final sin identificar

    def test_no_genera_pdf_sin_autorizacion(self):
        from barcofact.comprobante_pdf import generar
        with self.assertRaises(ValueError):
            generar(self.cfg, {"estado": "rechazado", "cae": None})

    def test_factura_a_requiere_cuit(self):
        vid = self.venta(cliente={"nombre": "Cliente SA", "condicion_iva": "Responsable Inscripto",
                                  "tipo_doc": None, "nro_doc": "", "domicilio": ""})
        with self.assertRaises(EmisionBloqueada) as e:
            self.f.emitir(vid, "ana")
        campos = {p.campo for p in e.exception.resultado.bloqueantes}
        self.assertTrue({"cliente.cuit", "cliente.domicilio"} <= campos)
        self.assertEqual(db.venta(self.con, vid)["estado"], "pendiente_datos")
        db.actualizar_venta(self.con, vid, {"cliente": {"nombre": "Cliente SA", "condicion_iva": "Responsable Inscripto",
                                                         "tipo_doc": 80, "nro_doc": CUIT_CLIENTE, "domicilio": "Av. X 1"}}, "ana")
        c = self.f.emitir(vid, "ana")
        self.assertEqual((c["estado"], c["tipo"], c["doc_tipo"]), ("autorizado", fiscal.FACTURA_A, 80))

    def test_datos_incompletos(self):
        vid = self.venta(cliente={"nombre": ""}, items=[{"descripcion": "x", "cantidad": "1", "precio_unitario_final": "100"}])
        r = self.f.vista_previa(vid)["validacion"]
        campos = {p.campo for p in r.bloqueantes}
        self.assertIn("cliente.condicion_iva", campos)
        self.assertIn("items", campos)  # falta alícuota
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM comprobantes").fetchone()[0], 0)

    def test_consumidor_final_sobre_umbral(self):
        vid = self.venta(items=[{"descripcion": "Lote", "cantidad": "1", "precio_unitario_final": "10000000",
                                 "alicuota": "21", "alicuota_confirmada": True}])
        r = self.f.vista_previa(vid)["validacion"]
        self.assertIn("cliente.identificacion", {p.campo for p in r.bloqueantes})

    def test_total_informado_distinto(self):
        vid = self.venta(total_informado="20000")  # p.ej. neto de comisiones
        r = self.f.vista_previa(vid)["validacion"]
        self.assertIn("total", {p.campo for p in r.bloqueantes})

    def test_ya_facturada(self):
        vid = self.venta()
        self.f.emitir(vid, "ana")
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM comprobantes").fetchone()[0], 1)

    def test_pedido_duplicado_no_se_crea(self):
        a = self.venta(pedido="X1")
        b, creado = db.guardar_venta(self.con, {"canal": "manual", "pedido_externo": "X1", "fecha_venta": "2026-01-01"}, "ana")
        self.assertEqual((a, creado), (b, False))

    def test_facturada_externamente(self):
        vid = self.venta(facturada_externamente="Factura B 0002-00000099 (otro sistema)")
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")

    def test_corte_despues_de_autorizar_no_duplica(self):
        vid = self.venta()
        self.sim.inyectar("corte_despues")
        c = self.f.emitir(vid, "ana")  # concilia automáticamente con FECompConsultar
        self.assertEqual(c["estado"], "autorizado")
        self.assertEqual(c["numero"], 1)
        self.assertEqual(self.sim.ultimo_autorizado(5, 6), 1)
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")
        self.assertIn("conciliado_autorizado",
                      [e["accion"] for e in self.con.execute("SELECT accion FROM eventos WHERE venta_id=?", (vid,))])

    def test_corte_y_arca_tampoco_responde_a_la_consulta(self):
        vid = self.venta()
        self.sim.inyectar("corte_despues")
        orig = self.sim.consultar
        self.sim.consultar = lambda *a: (_ for _ in ()).throw(__import__("barcofact.arca.transporte", fromlist=["x"]).ErrorTransporte("caído"))
        c = self.f.emitir(vid, "ana")
        self.assertEqual(c["estado"], "incierto")  # un timeout NO es "no emitido"
        self.assertEqual(db.venta(self.con, vid)["estado"], "conciliar")
        with self.assertRaises(ErrorEmision):
            self.f.emitir(vid, "ana")  # no reintenta a ciegas
        self.sim.consultar = orig
        c = self.f.emitir(vid, "ana")  # ahora concilia y encuentra el autorizado
        self.assertEqual((c["estado"], c["numero"]), ("autorizado", 1))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM comprobantes").fetchone()[0], 1)

    def test_corte_antes_de_llegar_permite_reintentar(self):
        vid = self.venta()
        self.sim.inyectar("corte_antes")
        c = self.f.emitir(vid, "ana")
        self.assertEqual(c["estado"], "rechazado")  # verificado: no emitido
        self.assertEqual(db.venta(self.con, vid)["estado"], "lista")
        c2 = self.f.emitir(vid, "ana")
        self.assertEqual((c2["estado"], c2["numero"]), ("autorizado", 1))

    def test_rechazo_de_arca(self):
        vid = self.venta()
        self.sim.inyectar("rechazo", ["10015", "DocNro inválido"])
        c = self.f.emitir(vid, "ana")
        self.assertEqual(c["estado"], "rechazado")
        self.assertIn("10015", c["errores_json"])
        self.assertEqual(db.venta(self.con, vid)["estado"], "error")
        self.f.revalidar(vid, "ana")
        c2 = self.f.emitir(vid, "ana")
        self.assertEqual((c2["estado"], c2["numero"]), ("autorizado", 1))  # el rechazo no consume número

    def test_conflicto_numeracion_otro_sistema(self):
        vid = self.venta()
        self.sim.inyectar("otro_sistema", 2)
        with self.assertRaises(EmisionBloqueada) as e:
            self.f.emitir(vid, "ana")
        self.assertIn("Otra herramienta", str(e.exception))
        self.f.aceptar_numeracion_externa(5, fiscal.FACTURA_B, "ana")
        c = self.f.emitir(vid, "ana")
        self.assertEqual((c["estado"], c["numero"]), ("autorizado", 3))

    def test_conflicto_en_el_envio(self):
        # Otro sistema emite entre nuestra consulta de numeración y el envío -> ARCA rechaza 10016.
        vid = self.venta()
        orig = self.sim.ultimo_autorizado
        llamadas = {"n": 0}

        def ultimo(pv, t):
            llamadas["n"] += 1
            if llamadas["n"] == 2:  # la validación interna del simulador
                self.sim.inyectar("otro_sistema", 1)
            return orig(pv, t)
        self.sim.ultimo_autorizado = ultimo
        c = self.f.emitir(vid, "ana")
        self.sim.ultimo_autorizado = orig
        self.assertEqual(c["estado"], "rechazado")
        self.assertIn("10016", c["errores_json"])
        self.f.revalidar(vid, "ana")
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")  # detecta numeración ajena antes de reintentar

    def test_emision_concurrente_una_sola_factura(self):
        vid = self.venta()
        resultados = []

        def emitir():
            f = Facturador(self.cfg, db.conectar(self.cfg.db_path), ws=self.sim)
            try:
                resultados.append(f.emitir(vid, "hilo")["estado"])
            except (EmisionBloqueada, ErrorEmision, Exception) as e:  # noqa: BLE001
                resultados.append(type(e).__name__)
        hs = [threading.Thread(target=emitir) for _ in range(4)]
        [h.start() for h in hs]
        [h.join() for h in hs]
        n = self.con.execute("SELECT COUNT(*) FROM comprobantes WHERE estado='autorizado'").fetchone()[0]
        self.assertEqual(n, 1, resultados)
        self.assertEqual(self.sim.ultimo_autorizado(5, 6), 1)

    def test_numeracion_correlativa(self):
        nums = [self.f.emitir(self.venta(pedido=f"P{i}"), "ana")["numero"] for i in range(3)]
        self.assertEqual(nums, [1, 2, 3])

    def test_nota_de_credito(self):
        vid = self.venta()
        fac = self.f.emitir(vid, "ana")
        nc = self.f.nota_credito(fac["id"], "ana", "Devolución parcial", "12100")
        self.assertEqual((nc["estado"], nc["tipo"]), ("autorizado", fiscal.NOTA_CREDITO_B))
        s = json.loads(nc["solicitud_json"])
        self.assertEqual(s["asociados"][0]["nro"], fac["numero"])
        self.assertEqual((s["imp_neto"], s["imp_iva"]), ("10000.00", "2100.00"))
        with self.assertRaises(EmisionBloqueada):
            self.f.nota_credito(fac["id"], "ana", "Excede", "12100.01")
        nc2 = self.f.nota_credito(fac["id"], "ana", "Resto")
        self.assertEqual(json.loads(nc2["importes_json"])["imp_total"], "12100.00")
        with self.assertRaises(EmisionBloqueada):
            self.f.nota_credito(fac["id"], "ana", "Nada más")
        self.assertEqual(db.venta(self.con, vid)["estado"], "facturada")  # la factura no se borra

    def test_borrador_no_emitible(self):
        vid = self.venta(estado="borrador")
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")

    def test_cancelada_no_se_factura(self):
        vid = self.venta(estado_operacion="cancelada")
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")

    def test_automatico_desactivado(self):
        from barcofact import automatico
        self.venta(canal="csv")
        self.assertFalse(automatico.procesar(self.f)["habilitado"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM comprobantes").fetchone()[0], 0)
        vid = self.venta(pedido="P9", canal="csv")
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "auto", modo="automatico")


class TestAutomaticoHabilitado(Base):
    auto = "true"
    auto_conf = "Contador X, 2026-10-01"

    def test_emite_solo_lo_que_cumple(self):
        from barcofact import automatico
        ok = self.venta(pedido="A1", canal="csv")
        sin_confirmar = self.venta(pedido="A2", canal="csv", items=[{"descripcion": "x", "cantidad": "1",
                                   "precio_unitario_final": "100", "alicuota": "21", "alicuota_confirmada": False}])
        otro_canal = self.venta(pedido="A3", canal="whatsapp")
        res = automatico.procesar(self.f)
        self.assertEqual([p for p, _ in res["emitidas"]], ["A1"])
        self.assertEqual(db.venta(self.con, ok)["estado"], "facturada")
        self.assertNotEqual(db.venta(self.con, sin_confirmar)["estado"], "facturada")
        self.assertNotEqual(db.venta(self.con, otro_canal)["estado"], "facturada")
        c = db.comprobantes_de(self.con, ok)[0]
        self.assertEqual((c["modo"], c["aprobado_por"]), ("automatico", "automatico"))


class TestProduccionBloqueada(Base):
    entorno = "produccion"

    def test_no_emite_sin_habilitar(self):
        vid = self.venta()
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana", confirmacion=self.cfg.emisor.cuit)

    def test_simulador_prohibido_en_produccion(self):
        from barcofact import config
        with self.assertRaises(config.ErrorConfiguracion):
            config.cargar("produccion", datos=self.tmp, simulado=True)


class TestProduccionHabilitada(Base):
    entorno = "produccion"
    prod_hab = "true"

    def test_requiere_confirmacion_escrita(self):
        vid = self.venta()
        with self.assertRaises(EmisionBloqueada):
            self.f.emitir(vid, "ana")
        c = self.f.emitir(vid, "ana", confirmacion=self.cfg.emisor.cuit)
        self.assertEqual((c["estado"], c["pto_vta"], c["entorno"]), ("autorizado", 7, "produccion"))
