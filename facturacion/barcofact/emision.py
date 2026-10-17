"""Emisión idempotente de comprobantes ante ARCA.

Garantías:
- Una venta tiene a lo sumo una factura viva (índice único en la base).
- La numeración se toma de FECompUltimoAutorizado bajo un bloqueo exclusivo por
  CUIT + punto de venta + tipo, y se registra ANTES de enviar (escritura anticipada).
- Si la conexión se corta, el comprobante queda «incierto» y se concilia con
  FECompConsultar antes de cualquier reintento. Un timeout nunca se toma como
  «no emitido».
- Sólo se marca «autorizado» con un CAE devuelto por ARCA (FECAESolicitar o FECompConsultar).
- Los comprobantes autorizados no se borran: se anulan con nota de crédito.
"""
from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

from . import db, fiscal, importes
from .arca import transporte
from .arca.wsaa import TZ_AR
from .arca.wsfe import ErrorArca
from .config import Config, verificar_secreto
from .validacion import Resultado, validar

ERROR_NUMERACION = "10016"


class ErrorEmision(Exception):
    pass


class EmisionBloqueada(ErrorEmision):
    """La venta no puede emitirse (faltan datos, ya facturada, producción no habilitada...)."""

    def __init__(self, mensaje: str, resultado: Resultado | None = None):
        super().__init__(mensaje)
        self.resultado = resultado


def crear_cliente_ws(cfg: Config):
    if cfg.simulado:
        from .arca.simulador import SimuladorWsfe
        return SimuladorWsfe(cfg.dir_entorno / "simulador.json")
    from .arca.wsaa import Wsaa
    from .arca.wsfe import Wsfe
    cert = verificar_secreto(cfg.arca.certificado, "el certificado de ARCA")
    clave = verificar_secreto(cfg.arca.clave_privada, "la clave privada")
    wsaa = Wsaa(cfg.arca.wsaa_url, cert, clave, cfg.dir_ta, cfg.emisor.cuit)
    return Wsfe(cfg.arca.wsfe_url, cfg.emisor.cuit, lambda: wsaa.credenciales("wsfe"))


def _hoy() -> str:
    return datetime.now(TZ_AR).strftime("%Y%m%d")


def _d(x) -> Decimal:
    return Decimal(str(x))


class Facturador:
    def __init__(self, cfg: Config, con=None, ws=None):
        self.cfg = cfg
        self.con = con or db.conectar(cfg.db_path)
        self._ws = ws

    @property
    def ws(self):
        if self._ws is None:
            self._ws = crear_cliente_ws(self.cfg)
        return self._ws

    # ------------------------------------------------------------------ bloqueo
    @contextmanager
    def _bloqueo(self, pto_vta: int, tipo: int, espera: float = 30.0):
        ruta = self.cfg.dir_entorno / f"emision-{self.cfg.emisor.cuit}-{pto_vta}-{tipo}.lock"
        fh = open(ruta, "w")
        limite = time.monotonic() + espera
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > limite:
                    fh.close()
                    raise ErrorEmision("Hay otra emisión en curso para este punto de venta y tipo. Reintentá en unos segundos.")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    # ------------------------------------------------------------- vista previa
    def vista_previa(self, venta_id: str, automatico: bool = False) -> dict:
        v = db.venta(self.con, venta_id)
        if v is None:
            raise KeyError(venta_id)
        r = validar(v, self.cfg, automatico=automatico)
        return {"venta": v, "validacion": r, "detalle": self._detalle(v, r) if r.calculo and r.tipo else None,
                "entorno": self.cfg.entorno, "simulado": self.cfg.simulado}

    def _detalle(self, v: dict, r: Resultado) -> dict:
        cli = v["cliente"]
        e = self.cfg.emisor
        return {
            "emisor": {"razon_social": e.razon_social, "cuit": e.cuit, "domicilio": e.domicilio_comercial,
                       "ingresos_brutos": e.ingresos_brutos, "inicio_actividades": e.inicio_actividades,
                       "condicion_iva": "IVA Responsable Inscripto"},
            "receptor": {"nombre": cli.get("nombre", "") or "Consumidor Final", "doc_tipo": r.doc_tipo,
                         "doc_nro": r.doc_nro, "cond_iva": r.cond_iva,
                         "cond_iva_txt": fiscal.CONDICIONES_IVA.get(r.cond_iva, ""),
                         "domicilio": cli.get("domicilio", "")},
            "lineas": [{"descripcion": l.descripcion, "sku": l.sku, "cantidad": str(l.cantidad),
                        "alicuota": str(l.alicuota), "precio_unitario_final": str(l.precio_unitario_final),
                        "precio_unitario_neto": str(l.precio_unitario_neto),
                        "descuento_final": str(l.descuento_final), "total_final": str(l.total_final),
                        "neto": str(l.neto), "iva": str(l.iva)} for l in r.calculo.lineas],
            "importes": r.calculo.a_dict(),
            "tipo": r.tipo, "tipo_txt": fiscal.nombre_tipo(r.tipo), "motivo_tipo": r.motivo_tipo,
            "concepto": r.concepto, "canal": v["canal"], "pedido": v["pedido_externo"],
            "pto_vta": self.cfg.arca.punto_venta,
        }

    def revalidar(self, venta_id: str, usuario: str) -> Resultado:
        """Actualiza el estado de una venta no emitida según su validación."""
        v = db.venta(self.con, venta_id)
        r = validar(v, self.cfg)
        if v["estado"] in ("pendiente_datos", "lista", "error"):
            nuevo = "lista" if r.emitible else "pendiente_datos"
            if nuevo != v["estado"]:
                db.set_estado(self.con, venta_id, nuevo, usuario)
        return r

    # ------------------------------------------------------------------ emisión
    def emitir(self, venta_id: str, usuario: str, modo: str = "manual", confirmacion: str = "") -> dict:
        assert modo in ("manual", "automatico")
        v = db.venta(self.con, venta_id)
        if v is None:
            raise KeyError(venta_id)

        # 1) Comprobantes pendientes de conciliación: resolver antes de nada.
        for c in db.comprobantes_de(self.con, venta_id):
            if c["clase"] == "factura" and c["estado"] in ("enviando", "incierto"):
                c = self.conciliar(c["id"], usuario)
                if c["estado"] == "autorizado":
                    return c
                if c["estado"] in ("enviando", "incierto"):
                    raise ErrorEmision("Hay un comprobante sin conciliar para esta venta. No se reintenta "
                                       "hasta confirmar con ARCA si fue emitido.")
            if c["clase"] == "factura" and c["estado"] == "autorizado":
                raise EmisionBloqueada(f"La venta ya tiene factura autorizada (comprobante {c['id']}).")
            if c["clase"] == "factura" and c["estado"] == "conflicto":
                raise EmisionBloqueada("La venta tiene un conflicto de numeración sin resolver.")
        v = db.venta(self.con, venta_id)
        if v["estado"] in ("facturada", "facturada_externamente", "conciliar", "descartada"):
            raise EmisionBloqueada(f"La venta está en estado «{db.ESTADOS_VENTA[v['estado']]}».")

        # 2) Validación
        r = validar(v, self.cfg, automatico=(modo == "automatico"))
        if modo == "automatico" and r.problemas:
            raise EmisionBloqueada("La venta no cumple las reglas de emisión automática.", r)
        if not r.emitible:
            db.set_estado(self.con, venta_id, "pendiente_datos", usuario,
                          "; ".join(p.mensaje for p in r.bloqueantes))
            raise EmisionBloqueada("La venta tiene datos faltantes o inconsistentes.", r)

        # 3) Habilitación por entorno y modo
        if self.cfg.es_produccion:
            if not self.cfg.arca.habilitada:
                raise EmisionBloqueada("Producción no está habilitada.")
            if self.cfg.simulado:
                raise EmisionBloqueada("No se puede usar el simulador en producción.")
            if modo == "manual" and confirmacion.replace("-", "") != self.cfg.emisor.cuit:
                raise EmisionBloqueada("Para emitir en PRODUCCIÓN escribí el CUIT del emisor como confirmación.")
        if modo == "automatico":
            au = self.cfg.automatico
            if not au.habilitado or not au.reglas_confirmadas_por:
                raise EmisionBloqueada("La emisión automática está desactivada.")

        detalle = self._detalle(v, r)
        s = {
            "pto_vta": self.cfg.arca.punto_venta, "tipo": r.tipo, "concepto": r.concepto,
            "doc_tipo": r.doc_tipo, "doc_nro": r.doc_nro, "fecha": _hoy(),
            "imp_total": str(r.calculo.imp_total), "imp_tot_conc": str(r.calculo.imp_tot_conc),
            "imp_neto": str(r.calculo.imp_neto), "imp_op_ex": str(r.calculo.imp_op_ex),
            "imp_trib": str(r.calculo.imp_trib), "imp_iva": str(r.calculo.imp_iva),
            "mon_id": "PES", "mon_cotiz": "1", "cond_iva_receptor": r.cond_iva,
            "iva": [{"id": g.id_arca, "base": str(g.base), "importe": str(g.importe)} for g in r.calculo.grupos_iva],
        }
        return self._emitir(venta_id, "factura", s, detalle, usuario, modo)

    def _emitir(self, venta_id, clase, s, detalle, usuario, modo, asociado_id=None, motivo=None) -> dict:
        cuit, pv, tipo = self.cfg.emisor.cuit, s["pto_vta"], s["tipo"]
        detalle = dict(detalle, simulado=self.cfg.simulado, entorno=self.cfg.entorno)
        with self._bloqueo(pv, tipo):
            try:
                ultimo = self.ws.ultimo_autorizado(pv, tipo)
            except (transporte.ErrorTransporte, transporte.ErrorSoap, ErrorArca) as e:
                raise ErrorEmision(f"No se pudo consultar la numeración en ARCA: {e}. No se envió nada.") from e
            self._controlar_numeracion_externa(pv, tipo, ultimo)
            s = dict(s, numero=ultimo + 1)
            t = db.ahora()
            # Escritura anticipada: el intento queda registrado antes de enviarlo.
            self.con.execute("BEGIN IMMEDIATE")
            try:
                cur = self.con.execute(
                    """INSERT INTO comprobantes(venta_id,clase,entorno,cuit,pto_vta,tipo,numero,fecha_cbte,concepto,
                       doc_tipo,doc_nro,cond_iva_receptor,importes_json,detalle_json,solicitud_json,estado,
                       asociado_id,motivo,aprobado_por,aprobado_en,modo,simulado,creado,actualizado)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'enviando',?,?,?,?,?,?,?,?)""",
                    (venta_id, clase, self.cfg.entorno, cuit, pv, tipo, s["numero"], s["fecha"], s["concepto"],
                     s["doc_tipo"], s["doc_nro"], s["cond_iva_receptor"], json.dumps(detalle["importes"]),
                     json.dumps(detalle, ensure_ascii=False), json.dumps(s), asociado_id, motivo, usuario, t,
                     modo, int(self.cfg.simulado), t, t))
                cid = cur.lastrowid
                if clase == "factura":
                    self.con.execute("UPDATE ventas SET estado='en_proceso', actualizado=? WHERE id=?", (t, venta_id))
                db.evento(self.con, usuario, f"envio_{clase}", venta_id, cid,
                          f"{fiscal.nombre_tipo(tipo)} PV {pv} N° {s['numero']} ({modo})")
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

            try:
                resp = self.ws.solicitar_cae(s)
            except (transporte.ErrorTransporte, transporte.ErrorSoap, Exception) as e:  # noqa: BLE001
                self._marcar(cid, estado="incierto", errores_json=json.dumps([["transporte", str(e)]]))
                self._estado_venta(venta_id, clase, "conciliar", usuario, f"Sin respuesta segura de ARCA: {e}")
                db.evento(self.con, usuario, "respuesta_incierta", venta_id, cid, str(e))
        # Fuera del bloqueo: conciliación inmediata si quedó incierto.
        c = db.comprobante(self.con, cid)
        if c["estado"] == "incierto":
            try:
                return self.conciliar(cid, usuario)
            except ErrorEmision:
                return db.comprobante(self.con, cid)
        return self._procesar_respuesta(cid, venta_id, clase, s, resp, usuario)

    def _controlar_numeracion_externa(self, pv, tipo, ultimo_arca):
        fila = self.con.execute(
            "SELECT MAX(numero) m FROM comprobantes WHERE entorno=? AND cuit=? AND pto_vta=? AND tipo=? "
            "AND estado='autorizado'", (self.cfg.entorno, self.cfg.emisor.cuit, pv, tipo)).fetchone()
        ultimo_local = fila["m"] or 0
        ext = self.con.execute(
            "SELECT ultimo_aceptado FROM numeracion_externa WHERE entorno=? AND cuit=? AND pto_vta=? AND tipo=?",
            (self.cfg.entorno, self.cfg.emisor.cuit, pv, tipo)).fetchone()
        conocido = max(ultimo_local, ext["ultimo_aceptado"] if ext else 0)
        if ultimo_arca > conocido:
            raise EmisionBloqueada(
                f"ARCA registra hasta el N° {ultimo_arca} en PV {pv} ({fiscal.nombre_tipo(tipo)}), pero este sistema "
                f"sólo conoce hasta el N° {conocido}. Otra herramienta está emitiendo en este punto de venta. "
                "Usá un punto de venta exclusivo para este sistema, o confirmá los comprobantes externos con "
                "`barcofact aceptar-numeracion-externa`.")

    def _marcar(self, cid: int, **campos) -> None:
        campos["actualizado"] = db.ahora()
        sets = ", ".join(f"{k}=?" for k in campos)
        self.con.execute(f"UPDATE comprobantes SET {sets} WHERE id=?", (*campos.values(), cid))

    def _estado_venta(self, venta_id, clase, estado, usuario, detalle=""):
        if clase == "factura":
            db.set_estado(self.con, venta_id, estado, usuario, detalle)

    def _procesar_respuesta(self, cid, venta_id, clase, s, resp, usuario) -> dict:
        comunes = dict(resultado=resp.resultado, respuesta_xml=resp.xml,
                       observaciones_json=json.dumps(resp.observaciones + resp.eventos, ensure_ascii=False),
                       errores_json=json.dumps(resp.errores, ensure_ascii=False))
        if resp.resultado == "A" and resp.cae and resp.numero == s["numero"]:
            self._marcar(cid, estado="autorizado", cae=resp.cae, cae_vto=resp.cae_vto, **comunes)
            self._estado_venta(venta_id, clase, "facturada", usuario, f"CAE {resp.cae}")
            db.evento(self.con, usuario, f"{clase}_autorizada", venta_id, cid, f"CAE {resp.cae} vto {resp.cae_vto}")
            self._generar_pdf(cid)
        elif resp.resultado == "A":
            # Aprobado pero sin CAE o con otro número: no se da por autorizado sin verificar.
            self._marcar(cid, estado="incierto", **comunes)
            self._estado_venta(venta_id, clase, "conciliar", usuario, "Respuesta de ARCA inconsistente")
            return self.conciliar(cid, usuario)
        else:
            self._marcar(cid, estado="rechazado", **comunes)
            texto = "; ".join(f"[{c}] {m}" for c, m in resp.errores + resp.observaciones)
            self._estado_venta(venta_id, clase, "error", usuario, texto)
            db.evento(self.con, usuario, f"{clase}_rechazada", venta_id, cid, texto)
        return db.comprobante(self.con, cid)

    # ------------------------------------------------------------- conciliación
    def conciliar(self, cid: int, usuario: str) -> dict:
        c = db.comprobante(self.con, cid)
        if c["estado"] not in ("enviando", "incierto"):
            return c
        try:
            consultado = self.ws.consultar(c["pto_vta"], c["tipo"], c["numero"])
            ultimo = self.ws.ultimo_autorizado(c["pto_vta"], c["tipo"]) if consultado is None else None
        except (transporte.ErrorTransporte, transporte.ErrorSoap, ErrorArca) as e:
            db.evento(self.con, usuario, "conciliacion_fallida", c["venta_id"], cid, str(e))
            raise ErrorEmision(f"No se pudo consultar a ARCA ({e}). El comprobante sigue pendiente de conciliación.") from e
        s = json.loads(c["solicitud_json"])
        if consultado is None:
            if ultimo is not None and ultimo < c["numero"]:
                self._marcar(cid, estado="rechazado", resultado="N",
                             errores_json=json.dumps([["conciliacion", "ARCA confirma que este número no fue emitido."]]))
                self._estado_venta(c["venta_id"], c["clase"], "lista", usuario, "Verificado: no emitido. Puede reintentarse.")
                db.evento(self.con, usuario, "conciliado_no_emitido", c["venta_id"], cid)
            else:
                db.evento(self.con, usuario, "conciliacion_indeterminada", c["venta_id"], cid,
                          f"último ARCA {ultimo}, número {c['numero']}")
            return db.comprobante(self.con, cid)

        coincide = (consultado.doc_tipo == int(s["doc_tipo"]) and str(int(consultado.doc_nro or 0)) == str(int(s["doc_nro"] or 0))
                    and _d(consultado.imp_total) == _d(s["imp_total"]) and consultado.fecha == s["fecha"])
        if coincide and consultado.cae:
            self._marcar(cid, estado="autorizado", resultado="A", cae=consultado.cae, cae_vto=consultado.cae_vto,
                         respuesta_xml=consultado.xml)
            self._estado_venta(c["venta_id"], c["clase"], "facturada", usuario, f"Conciliado: CAE {consultado.cae}")
            db.evento(self.con, usuario, "conciliado_autorizado", c["venta_id"], cid, f"CAE {consultado.cae}")
            self._generar_pdf(cid)
        else:
            self._marcar(cid, estado="conflicto", respuesta_xml=consultado.xml,
                         errores_json=json.dumps([["conflicto", "El número está ocupado por otro comprobante en ARCA."]]))
            self._estado_venta(c["venta_id"], c["clase"], "conciliar", usuario, "Conflicto de numeración")
            db.evento(self.con, usuario, "conflicto_numeracion", c["venta_id"], cid)
        return db.comprobante(self.con, cid)

    def resolver_conflicto(self, cid: int, usuario: str, explicacion: str) -> None:
        """El operador confirma que el número en conflicto pertenece a otro comprobante, no a esta venta."""
        c = db.comprobante(self.con, cid)
        if c["estado"] != "conflicto":
            raise ErrorEmision("El comprobante no está en conflicto.")
        if not explicacion.strip():
            raise ErrorEmision("Indicá cómo se verificó el conflicto.")
        self._marcar(cid, estado="rechazado", motivo=f"Conflicto resuelto: {explicacion}")
        self._estado_venta(c["venta_id"], c["clase"], "lista", usuario, explicacion)
        db.evento(self.con, usuario, "conflicto_resuelto", c["venta_id"], cid, explicacion)

    def aceptar_numeracion_externa(self, pto_vta: int, tipo: int, usuario: str) -> int:
        ultimo = self.ws.ultimo_autorizado(pto_vta, tipo)
        self.con.execute(
            "INSERT OR REPLACE INTO numeracion_externa VALUES (?,?,?,?,?,?,?)",
            (self.cfg.entorno, self.cfg.emisor.cuit, pto_vta, tipo, ultimo, usuario, db.ahora()))
        db.evento(self.con, usuario, "numeracion_externa_aceptada", detalle=f"PV {pto_vta} tipo {tipo} hasta {ultimo}")
        return ultimo

    # ----------------------------------------------------------- nota de crédito
    def nota_credito(self, factura_id: int, usuario: str, motivo: str, importe_total: str | None = None,
                     confirmacion: str = "") -> dict:
        """NC total (por defecto) o parcial por importe con IVA incluido, prorrateado por alícuota."""
        f = db.comprobante(self.con, factura_id)
        if f is None or f["clase"] != "factura" or f["estado"] != "autorizado":
            raise EmisionBloqueada("Sólo se emite nota de crédito sobre una factura autorizada.")
        if not motivo.strip():
            raise EmisionBloqueada("Indicá el motivo de la nota de crédito.")
        if self.cfg.es_produccion and confirmacion.replace("-", "") != self.cfg.emisor.cuit:
            raise EmisionBloqueada("Para emitir en PRODUCCIÓN escribí el CUIT del emisor como confirmación.")
        for c in db.comprobantes_de(self.con, f["venta_id"]):
            if c["clase"] == "nota_credito" and c["estado"] in ("enviando", "incierto", "conflicto"):
                raise EmisionBloqueada("Hay una nota de crédito sin conciliar para esta venta.")
        acreditado = sum((_d(json.loads(c["importes_json"])["imp_total"]) for c in db.comprobantes_de(self.con, f["venta_id"])
                          if c["clase"] == "nota_credito" and c["estado"] == "autorizado"), Decimal("0"))
        imp_f = json.loads(f["importes_json"])
        total_f = _d(imp_f["imp_total"])
        importe = _d(importe_total) if importe_total else total_f - acreditado
        if importe <= 0 or acreditado + importe > total_f:
            raise EmisionBloqueada(f"Importe inválido: la factura es de {total_f} y ya se acreditaron {acreditado}.")

        # Prorrateo del importe por alícuota, respetando la proporción de la factura.
        grupos = imp_f["grupos_iva"]
        totales = [_d(g["base"]) + _d(g["importe"]) for g in grupos]
        partes = importes._repartir(importe, totales) if grupos else []
        lineas = [importes.LineaEntrada(f"Nota de crédito – {motivo}", Decimal("1"), p, _d(g["alicuota"]))
                  for g, p in zip(grupos, partes) if p > 0]
        resto = importe - sum(partes)
        if resto:
            lineas.append(importes.LineaEntrada(f"Nota de crédito – {motivo}", Decimal("1"), resto, importes.EXENTO))
        calc = importes.calcular(lineas)
        det_f = json.loads(f["detalle_json"])
        tipo = fiscal.NOTA_CREDITO_DE[f["tipo"]]
        detalle = dict(det_f, tipo=tipo, tipo_txt=fiscal.nombre_tipo(tipo), importes=calc.a_dict(),
                       lineas=[{"descripcion": l.descripcion, "sku": "", "cantidad": "1", "alicuota": str(l.alicuota),
                                "precio_unitario_final": str(l.total_final), "precio_unitario_neto": str(l.neto),
                                "descuento_final": "0", "total_final": str(l.total_final), "neto": str(l.neto),
                                "iva": str(l.iva)} for l in calc.lineas],
                       asociado={"tipo": f["tipo"], "pto_vta": f["pto_vta"], "numero": f["numero"]},
                       motivo=motivo)
        s = {
            "pto_vta": f["pto_vta"], "tipo": tipo, "concepto": f["concepto"], "doc_tipo": f["doc_tipo"],
            "doc_nro": f["doc_nro"], "fecha": _hoy(), "imp_total": str(calc.imp_total),
            "imp_tot_conc": str(calc.imp_tot_conc), "imp_neto": str(calc.imp_neto), "imp_op_ex": str(calc.imp_op_ex),
            "imp_trib": "0", "imp_iva": str(calc.imp_iva), "mon_id": "PES", "mon_cotiz": "1",
            "cond_iva_receptor": f["cond_iva_receptor"],
            "iva": [{"id": g.id_arca, "base": str(g.base), "importe": str(g.importe)} for g in calc.grupos_iva],
            "asociados": [{"tipo": f["tipo"], "pto_vta": f["pto_vta"], "nro": f["numero"], "cuit": f["cuit"],
                           "fecha": f["fecha_cbte"]}],
        }
        return self._emitir(f["venta_id"], "nota_credito", s, detalle, usuario, "manual", asociado_id=factura_id,
                            motivo=motivo)

    # ---------------------------------------------------------------------- PDF
    def _generar_pdf(self, cid: int) -> None:
        from .comprobante_pdf import generar
        try:
            ruta = generar(self.cfg, db.comprobante(self.con, cid))
            self._marcar(cid, pdf_path=str(ruta))
        except Exception as e:  # noqa: BLE001 - el CAE ya está guardado; el PDF se puede regenerar
            db.evento(self.con, "sistema", "pdf_error", comprobante_id=cid, detalle=str(e))

    def regenerar_pdf(self, cid: int) -> str:
        c = db.comprobante(self.con, cid)
        if c["estado"] != "autorizado":
            raise ErrorEmision("Sólo se genera el comprobante de una autorización válida.")
        self._generar_pdf(cid)
        return db.comprobante(self.con, cid)["pdf_path"]
