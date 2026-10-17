"""Pantalla de control local (http://127.0.0.1:8765). Sólo escucha en este equipo."""
from __future__ import annotations

import html
import json
import secrets
import threading
import traceback
import urllib.parse
from datetime import date
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import automatico, backup, db, exportar, fiscal, usuarios
from .canales import csv_import, whatsapp
from .catalogo import Catalogo
from .comprobante_pdf import fmt
from .config import Config
from .emision import EmisionBloqueada, ErrorEmision, Facturador

E = html.escape
SESIONES: dict[str, dict] = {}
_lock_sesiones = threading.Lock()

CSS = """
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f5f6f8;color:#1d2330}
a{color:#0b5cad}.banner{padding:12px 20px;font-weight:800;font-size:18px;letter-spacing:.5px;text-align:center}
.homo{background:#ffcc00;color:#3a2c00}.sim{background:#ff9f1a;color:#2b1700}.prod{background:#c8102e;color:#fff}
nav{background:#1d2330;padding:8px 20px;display:flex;gap:16px;flex-wrap:wrap}nav a{color:#fff;text-decoration:none}
main{padding:20px;max-width:1200px;margin:auto}table{border-collapse:collapse;width:100%;background:#fff}
th,td{padding:7px 9px;border-bottom:1px solid #e3e6ea;text-align:left;font-size:14px;vertical-align:top}
th{background:#eef0f3}.num{text-align:right;font-variant-numeric:tabular-nums}.card{background:#fff;border-radius:8px;padding:16px;margin:12px 0;box-shadow:0 1px 2px #0001}
.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{background:#fff;border:1px solid #d5d9df;border-radius:20px;padding:6px 12px;text-decoration:none;color:#1d2330}
.chip b{margin-left:6px}.err{color:#a40e26}.ok{color:#137333}.warn{color:#8a5a00}
input,select,textarea{font:inherit;padding:6px;border:1px solid #c5cad1;border-radius:4px}textarea{width:100%}
button{font:inherit;padding:8px 14px;border-radius:5px;border:0;background:#0b5cad;color:#fff;cursor:pointer}
button.peligro{background:#c8102e}button.sec{background:#5b6573}.estado{font-size:12px;padding:2px 8px;border-radius:10px;background:#eef0f3}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}label{display:block;font-size:13px;margin-top:6px}
.flash{padding:10px;border-radius:6px;margin:10px 0}.flash.e{background:#fde7ea}.flash.o{background:#e6f4ea}
"""


def pagina(cfg: Config, titulo: str, cuerpo: str, usuario: str | None = None, flash: str = "") -> bytes:
    if cfg.es_produccion:
        banner = '<div class="banner prod">⚠ PRODUCCIÓN — LOS COMPROBANTES SON REALES ANTE ARCA</div>'
    elif cfg.simulado:
        banner = '<div class="banner sim">SIMULADOR LOCAL — sin conexión a ARCA, nada tiene validez</div>'
    else:
        banner = '<div class="banner homo">HOMOLOGACIÓN (pruebas ARCA) — comprobantes sin validez fiscal</div>'
    nav = ""
    if usuario:
        nav = ('<nav><a href="/">Pedidos</a><a href="/nueva">Cargar venta</a><a href="/importar">Importar CSV</a>'
               '<a href="/whatsapp">Borrador WhatsApp</a><a href="/mercadolibre">Mercado Libre</a>'
               '<a href="/exportar">Exportar</a><a href="/sistema">Sistema</a>'
               f'<span style="margin-left:auto;color:#aab">{E(usuario)} · <a href="/salir">salir</a></span></nav>')
    return (f"<!doctype html><html lang=es><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{E(titulo)} · BarcoFact</title><style>{CSS}</style>{banner}{nav}<main>{flash}"
            f"<h1>{E(titulo)}</h1>{cuerpo}</main></html>").encode()


def estado_chip(estado: str) -> str:
    color = {"facturada": "#e6f4ea", "error": "#fde7ea", "conciliar": "#fff1d6", "pendiente_datos": "#fff8e1",
             "lista": "#e8f0fe", "autorizado": "#e6f4ea", "rechazado": "#fde7ea", "incierto": "#fff1d6",
             "conflicto": "#fde7ea"}.get(estado, "#eef0f3")
    return f'<span class="estado" style="background:{color}">{E(db.ESTADOS_VENTA.get(estado, estado))}</span>'


def total_venta(v: dict) -> str:
    try:
        from .validacion import lineas_de
        from . import importes
        ls, err = lineas_de(v)
        if err or not ls:
            return "—"
        return fmt(importes.calcular(ls, importes.dec(v["descuento_general"] or "0")).imp_total)
    except Exception:  # noqa: BLE001
        return "—"


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def catalogo(self) -> Catalogo:
        return Catalogo.cargar(self.cfg.datos / "catalogo.csv")


def crear_handler(app: App):
    cfg = app.cfg

    class H(BaseHTTPRequestHandler):
        server_version = "BarcoFact"

        def log_message(self, fmt_, *args):  # sin datos personales en los logs
            pass

        # ---------------------------------------------------------- utilidades
        def _sesion(self):
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            sid = c.get("sid")
            with _lock_sesiones:
                return SESIONES.get(sid.value) if sid else None

        def _enviar(self, cuerpo: bytes, tipo="text/html; charset=utf-8", status=200, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(cuerpo)

        def _redir(self, url, flash=None, sesion=None):
            if flash and sesion is not None:
                sesion["flash"] = flash
            self.send_response(303)
            self.send_header("Location", url)
            self.end_headers()

        def _form(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if n > 5_000_000:
                raise ValueError("Formulario demasiado grande")
            datos = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"), keep_blank_values=True)
            return {k: v if len(v) > 1 or k.endswith("[]") else v[0] for k, v in datos.items()}

        def _flash(self, s) -> str:
            f = s.pop("flash", None)
            if not f:
                return ""
            ok, txt = f
            return f'<div class="flash {"o" if ok else "e"}">{txt}</div>'

        def _csrf(self, s) -> str:
            return f'<input type=hidden name=csrf value="{s["csrf"]}">'

        # --------------------------------------------------------------- GET
        def do_GET(self):
            try:
                self._get()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._enviar(b"Error interno (ver consola).", "text/plain; charset=utf-8", 500)

        def do_POST(self):
            try:
                self._post()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._enviar(b"Error interno (ver consola).", "text/plain; charset=utf-8", 500)

        def _get(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            s = self._sesion()
            if u.path == "/login" or s is None:
                return self._login_form()
            if u.path == "/salir":
                with _lock_sesiones:
                    SESIONES.pop(s["sid"], None)
                return self._redir("/login")
            con = db.conectar(cfg.db_path)
            try:
                rutas = {"/": self.p_inicio, "/venta": self.p_venta, "/nueva": self.p_nueva,
                         "/importar": self.p_importar, "/whatsapp": self.p_whatsapp,
                         "/mercadolibre": self.p_ml, "/exportar": self.p_exportar, "/sistema": self.p_sistema,
                         "/pdf": self.p_pdf, "/descargar": self.p_descargar}
                f = rutas.get(u.path)
                if f is None:
                    return self._enviar(b"No encontrado", "text/plain", 404)
                f(con, s, q)
            finally:
                con.close()

        def _login_form(self, error=""):
            con = db.conectar(cfg.db_path)
            hay = usuarios.hay_usuarios(con)
            con.close()
            aviso = "" if hay else ('<p class=err>No hay usuarios. Creá uno con: '
                                    '<code>python -m barcofact crear-usuario TU_NOMBRE</code></p>')
            cuerpo = (f'{aviso}<form method=post action=/login class=card style="max-width:360px">'
                      f'<label>Usuario<input name=usuario autofocus></label><label>Clave<input type=password name=clave></label>'
                      f'<p class=err>{E(error)}</p><button>Ingresar</button></form>')
            self._enviar(pagina(cfg, "Ingresar", cuerpo))

        def p_inicio(self, con, s, q):
            estado = q.get("estado", "")
            ventas = db.buscar_ventas(con, estado or None, q.get("q") or None, q.get("desde") or None, q.get("hasta") or None)
            conteo = {r["estado"]: r["n"] for r in con.execute("SELECT estado, COUNT(*) n FROM ventas GROUP BY estado")}
            orden = ["pendiente_datos", "lista", "conciliar", "error", "facturada", "facturada_externamente", "borrador",
                     "descartada"]
            chips = "".join(f'<a class=chip href="/?estado={k}">{E(db.ESTADOS_VENTA[k])}<b>{conteo.get(k, 0)}</b></a>'
                            for k in orden)
            filas = "".join(
                f'<tr><td>{E(v["fecha_venta"])}</td><td>{E(v["canal"])}</td><td><a href="/venta?id={v["id"]}">{E(v["pedido_externo"])}</a></td>'
                f'<td>{E(v["cliente"].get("nombre") or "—")}</td><td class=num>{total_venta(v)}</td><td>{estado_chip(v["estado"])}</td></tr>'
                for v in ventas)
            cuerpo = (f'<div class=chips><a class=chip href="/">Todos<b>{sum(conteo.values())}</b></a>{chips}</div>'
                      f'<form class=card method=get><div class=grid><label>Cliente, pedido o documento<input name=q value="{E(q.get("q", ""))}"></label>'
                      f'<label>Desde<input type=date name=desde value="{E(q.get("desde", ""))}"></label>'
                      f'<label>Hasta<input type=date name=hasta value="{E(q.get("hasta", ""))}"></label>'
                      f'<input type=hidden name=estado value="{E(estado)}"></div><p><button>Buscar</button></p></form>'
                      f'<table><tr><th>Fecha</th><th>Canal</th><th>Pedido</th><th>Cliente</th><th class=num>Total</th><th>Estado</th></tr>'
                      f'{filas or "<tr><td colspan=6>Sin resultados</td></tr>"}</table>')
            self._enviar(pagina(cfg, "Pedidos", cuerpo, s["usuario"], self._flash(s)))

        def p_venta(self, con, s, q):
            f = Facturador(cfg, con)
            try:
                vp = f.vista_previa(q["id"])
            except KeyError:
                return self._enviar(b"Venta inexistente", "text/plain", 404)
            v, r, det = vp["venta"], vp["validacion"], vp["detalle"]
            cli = v["cliente"]
            csrf = self._csrf(s)
            partes = [f'<p>{estado_chip(v["estado"])} · Canal <b>{E(v["canal"])}</b> · Pedido <b>{E(v["pedido_externo"])}</b> · '
                      f'ID interno <code>{v["id"]}</code> · Pago: {E(v["estado_pago"])} · Operación: {E(v["estado_operacion"])}</p>']
            if v["notas"]:
                partes.append(f'<p class=warn>{E(v["notas"])}</p>')
            if r.problemas:
                lis = "".join(f'<li class={"err" if p.bloqueante else "warn"}><b>{"BLOQUEA" if p.bloqueante else "Aviso"}:</b> '
                              f'{E(p.mensaje)}<br><small>Cómo resolverlo: {E(p.como_resolver)}</small></li>' for p in r.problemas)
                partes.append(f'<div class=card><h3>Validación</h3><ul>{lis}</ul></div>')
            else:
                partes.append('<div class=card ok>Validación sin observaciones.</div>')

            editable = v["estado"] not in ("facturada", "en_proceso", "conciliar")
            if det:
                partes.append(self._vista_previa_html(det, v, r, csrf, editable and r.emitible))

            # Comprobantes
            comps = db.comprobantes_de(con, v["id"])
            if comps:
                filas = ""
                for c in comps:
                    imp = json.loads(c["importes_json"])
                    acciones = ""
                    if c["estado"] in ("enviando", "incierto"):
                        acciones = (f'<form method=post action=/conciliar>{csrf}<input type=hidden name=cid value={c["id"]}>'
                                    f'<button class=sec>Consultar estado en ARCA</button></form>')
                    elif c["estado"] == "conflicto":
                        acciones = (f'<form method=post action=/resolver>{csrf}<input type=hidden name=cid value={c["id"]}>'
                                    f'<input name=explicacion placeholder="Cómo verificaste el conflicto" size=30 required>'
                                    f'<button class=sec>Liberar venta</button></form>')
                    elif c["estado"] == "autorizado":
                        acciones = f'<a href="/pdf?id={c["id"]}">Descargar PDF</a>'
                        if c["clase"] == "factura":
                            conf = '<input name=confirmacion placeholder="CUIT emisor" size=13 required>' if cfg.es_produccion else ""
                            acciones += (f'<details><summary>Nota de crédito…</summary><form method=post action=/nc>{csrf}'
                                         f'<input type=hidden name=cid value={c["id"]}><label>Motivo<input name=motivo required></label>'
                                         f'<label>Importe con IVA (vacío = total)<input name=importe></label>{conf}'
                                         f'<button class=peligro>Emitir nota de crédito</button></form></details>')
                    errores = json.loads(c["errores_json"] or "[]") + json.loads(c["observaciones_json"] or "[]")
                    err_html = "<br>".join(E(f"[{a}] {b}") for a, b in errores)
                    num = f'{c["pto_vta"]:05d}-{c["numero"]:08d}' if c["numero"] else "—"
                    filas += (f'<tr><td>{c["id"]}</td><td>{E(fiscal.nombre_tipo(c["tipo"]))}</td><td>{num}</td>'
                              f'<td>{estado_chip(c["estado"])}{" (SIMULADO)" if c["simulado"] else ""}</td>'
                              f'<td>{E(c["cae"] or "")}<br><small>{E(c["cae_vto"] or "")}</small></td><td class=num>{fmt(imp["imp_total"])}</td>'
                              f'<td><small>{E(c["aprobado_por"])} · {E(c["aprobado_en"])} · {E(c["modo"])}</small></td>'
                              f'<td><small class=err>{err_html}</small>{acciones}</td></tr>')
                partes.append(f'<div class=card><h3>Comprobantes</h3><table><tr><th>#</th><th>Tipo</th><th>Número</th><th>Estado</th>'
                              f'<th>CAE / Vto.</th><th class=num>Total</th><th>Aprobó</th><th></th></tr>{filas}</table></div>')

            if editable:
                partes.append(self._form_edicion(v, csrf))
                acciones = []
                if v["estado"] == "borrador":
                    acciones.append(f'<form method=post action=/confirmar>{csrf}<input type=hidden name=id value={v["id"]}>'
                                    f'<button class=sec>Confirmar borrador (revisé todos los datos)</button></form>')
                if v["canal"] == "mercadolibre":
                    acciones.append(f'<form method=post action=/verificacion>{csrf}<input type=hidden name=id value={v["id"]}>'
                                    f'<input name=nota size=50 placeholder="Revisé el pedido en ML y no tiene factura" required>'
                                    f'<button class=sec>Registrar verificación manual</button></form>')
                acciones.append(f'<form method=post action=/externa>{csrf}<input type=hidden name=id value={v["id"]}>'
                                f'<input name=referencia size=40 placeholder="Ej.: Factura B 0003-00001234 emitida en otra herramienta" required>'
                                f'<button class=sec>Marcar como facturada en otra herramienta</button></form>')
                acciones.append(f'<form method=post action=/descartar>{csrf}<input type=hidden name=id value={v["id"]}>'
                                f'<input name=motivo placeholder="Motivo" required><button class=sec>Descartar (no facturar)</button></form>')
                partes.append('<div class=card><h3>Otras acciones</h3>' + "".join(acciones) + '</div>')

            ev = "".join(f'<tr><td><small>{E(e["ts"])}</small></td><td>{E(e["usuario"])}</td><td>{E(e["accion"])}</td>'
                         f'<td><small>{E(e["detalle"])}</small></td></tr>'
                         for e in con.execute("SELECT * FROM eventos WHERE venta_id=? ORDER BY id DESC", (v["id"],)))
            partes.append(f'<div class=card><h3>Registro</h3><table>{ev}</table></div>')
            self._enviar(pagina(cfg, f"Venta {v['pedido_externo']}", "".join(partes), s["usuario"], self._flash(s)))

        def _vista_previa_html(self, det, v, r, csrf, emitible) -> str:
            imp = det["importes"]
            clase_a = fiscal.es_clase_a_o_m(det["tipo"])
            rc = det["receptor"]
            filas = "".join(
                f'<tr><td>{E(l["descripcion"])}</td><td class=num>{E(l["cantidad"])}</td>'
                f'<td class=num>{fmt(l["precio_unitario_neto"] if clase_a else l["precio_unitario_final"])}</td>'
                f'<td class=num>{fmt(l["descuento_final"])}</td><td class=num>{E(l["alicuota"])}%</td>'
                f'<td class=num>{fmt(l["neto"])}</td><td class=num>{fmt(l["iva"])}</td><td class=num>{fmt(l["total_final"])}</td></tr>'
                for l in det["lineas"])
            ivas = "".join(f'<tr><td colspan=7 class=num>IVA {E(g["alicuota"])}% sobre {fmt(g["base"])}</td>'
                           f'<td class=num>{fmt(g["importe"])}</td></tr>' for g in imp["grupos_iva"])
            boton = ""
            if emitible:
                conf = ""
                if cfg.es_produccion:
                    conf = ('<label class=err><b>PRODUCCIÓN:</b> escribí el CUIT del emisor para confirmar '
                            '<input name=confirmacion required size=13></label>')
                boton = (f'<form method=post action=/emitir onsubmit="return confirm(\'¿Emitir este comprobante ante ARCA?\')">{csrf}'
                         f'<input type=hidden name=id value={v["id"]}>{conf}'
                         f'<p><button class={"peligro" if cfg.es_produccion else ""}>Aprobar y emitir {E(det["tipo_txt"])}</button></p></form>')
            return (f'<div class=card><h3>Vista previa: {E(det["tipo_txt"])} · PV {det["pto_vta"]:05d} · N° (lo asigna ARCA al emitir)</h3>'
                    f'<p><small>{E(det["motivo_tipo"])}</small></p><div class=grid>'
                    f'<div><b>Emisor</b><br>{E(det["emisor"]["razon_social"])}<br>CUIT {E(det["emisor"]["cuit"])}<br>{E(det["emisor"]["domicilio"])}</div>'
                    f'<div><b>Receptor</b><br>{E(rc["nombre"])}<br>{E(fiscal.TIPOS_DOC.get(rc["doc_tipo"], ""))} {E(rc["doc_nro"])}<br>'
                    f'{E(rc["cond_iva_txt"])}<br>{E(rc["domicilio"] or "")}</div></div>'
                    f'<table><tr><th>Descripción</th><th class=num>Cant.</th><th class=num>P. unit. {"neto" if clase_a else "final"}</th>'
                    f'<th class=num>Desc.</th><th class=num>IVA</th><th class=num>Neto</th><th class=num>IVA $</th><th class=num>Total</th></tr>'
                    f'{filas}<tr><td colspan=7 class=num>Neto gravado</td><td class=num>{fmt(imp["imp_neto"])}</td></tr>{ivas}'
                    f'<tr><td colspan=7 class=num><b>TOTAL</b></td><td class=num><b>{fmt(imp["imp_total"])}</b></td></tr></table>'
                    f'{"" if clase_a else "<p><small>Factura B: el PDF muestra «IVA contenido» (Ley 27.743).</small></p>"}{boton}</div>')

        def _form_edicion(self, v, csrf) -> str:
            cli = v["cliente"]
            conds = "".join(f'<option value={k} {"selected" if fiscal.condicion_desde_texto(cli.get("condicion_iva")) == k else ""}>{E(n)}</option>'
                            for k, n in fiscal.CONDICIONES_IVA.items())
            docs = "".join(f'<option value={k} {"selected" if str(cli.get("tipo_doc")) == str(k) else ""}>{E(n)}</option>'
                           for k, n in fiscal.TIPOS_DOC.items())
            items = v["items"] + [{}] * 3
            filas = "".join(
                f'<tr><td><input name="desc[]" value="{E(str(it.get("descripcion", "")))}" size=30></td>'
                f'<td><input name="sku[]" value="{E(str(it.get("sku", "")))}" size=10></td>'
                f'<td><input name="cant[]" value="{E(str(it.get("cantidad", "")))}" size=5></td>'
                f'<td><input name="precio[]" value="{E(str(it.get("precio_unitario_final", "")))}" size=10></td>'
                f'<td><input name="descu[]" value="{E(str(it.get("descuento_final", "0") if it else ""))}" size=7></td>'
                f'<td><input name="alic[]" value="{E(str(it.get("alicuota", "")))}" size=6></td>'
                f'<td><select name="envio[]"><option value=0>No</option><option value=1 {"selected" if it.get("es_envio") else ""}>Sí</option></select></td></tr>'
                for it in items)
            return (f'<form method=post action=/editar class=card>{csrf}<input type=hidden name=id value={v["id"]}><h3>Editar datos</h3>'
                    f'<div class=grid><label>Nombre / razón social<input name=nombre value="{E(cli.get("nombre", ""))}"></label>'
                    f'<label>Tipo doc.<select name=tipo_doc><option value="">—</option>{docs}</select></label>'
                    f'<label>N° documento<input name=nro_doc value="{E(str(cli.get("nro_doc", "")))}"></label>'
                    f'<label>Condición IVA<select name=condicion_iva><option value="">— sin dato —</option>{conds}</select></label>'
                    f'<label>Domicilio<input name=domicilio value="{E(cli.get("domicilio", ""))}"></label>'
                    f'<label>Fecha venta<input type=date name=fecha_venta value="{E(v["fecha_venta"])}"></label>'
                    f'<label>Estado de pago<select name=estado_pago>' + "".join(
                        f'<option {"selected" if v["estado_pago"] == x else ""}>{x}</option>' for x in ("acreditado", "pendiente", "desconocido"))
                    + f'</select></label><label>Descuento general (con IVA)<input name=descuento_general value="{E(v["descuento_general"])}"></label>'
                    f'<label>Total cobrado al comprador (control)<input name=total_informado value="{E(v["total_informado"] or "")}"></label></div>'
                    f'<p><small>Precios y descuentos CON IVA incluido. Alícuota en %: 21, 10.5, 27, 5, 2.5, 0 o «exento». '
                    f'Ítems sin descripción se eliminan. Las alícuotas editadas a mano quedan «sin confirmar por el contador».</small></p>'
                    f'<table><tr><th>Descripción</th><th>SKU</th><th>Cant.</th><th>Precio unit. final</th><th>Desc.</th><th>Alíc. %</th><th>Envío</th></tr>{filas}</table>'
                    f'<p><button>Guardar y revalidar</button></p></form>')

        def p_nueva(self, con, s, q):
            hoy = date.today().isoformat()
            cuerpo = (f'<form method=post action=/nueva class=card>{self._csrf(s)}<div class=grid>'
                      f'<label>Canal<select name=canal><option>whatsapp</option><option>manual</option><option>mercadolibre</option></select></label>'
                      f'<label>N° de pedido (vacío = se genera)<input name=pedido></label>'
                      f'<label>Fecha<input type=date name=fecha value={hoy}></label></div>'
                      f'<p>Después de crearla vas a poder cargar cliente e ítems.</p><button>Crear venta</button></form>')
            self._enviar(pagina(cfg, "Cargar venta", cuerpo, s["usuario"], self._flash(s)))

        def p_importar(self, con, s, q):
            cab = ("canal,pedido,fecha,estado_pago,cliente_nombre,tipo_doc,nro_doc,condicion_iva,domicilio,email,"
                   "sku,descripcion,cantidad,precio_unitario_final,descuento,alicuota,envio,descuento_general,total_informado")
            cuerpo = (f'<form method=post action=/importar class=card>{self._csrf(s)}'
                      f'<p>Una fila por ítem. Precios con IVA incluido. Columnas:</p><pre style="white-space:pre-wrap">{cab}</pre>'
                      f'<p><input type=file accept=.csv onchange="const r=new FileReader();r.onload=e=>document.getElementById(\'t\').value=e.target.result;r.readAsText(this.files[0])"></p>'
                      f'<textarea id=t name=csv rows=12 placeholder="Pegá el CSV o elegí un archivo"></textarea><p><button>Importar</button></p></form>')
            self._enviar(pagina(cfg, "Importar CSV", cuerpo, s["usuario"], self._flash(s)))

        def p_whatsapp(self, con, s, q):
            cuerpo = (f'<form method=post action=/whatsapp class=card>{self._csrf(s)}'
                      f'<p>Pegá el mensaje del cliente. Se crea un <b>borrador</b>: precios y datos fiscales se completan y confirman a mano. '
                      f'Un mensaje o presupuesto no alcanza para emitir.</p>'
                      f'<div class=grid><label>Teléfono / contacto<input name=telefono></label><label>N° de pedido (opcional)<input name=pedido></label></div>'
                      f'<textarea name=mensaje rows=10></textarea><p><button>Crear borrador</button></p></form>')
            self._enviar(pagina(cfg, "Borrador desde WhatsApp", cuerpo, s["usuario"], self._flash(s)))

        def p_ml(self, con, s, q):
            conf = (cfg.datos / "mercadolibre.json").exists()
            estado = ('<p class=ok>Cuenta vinculada.</p>' if conf else
                      '<p class=err>No vinculado. Ejecutá <code>python -m barcofact ml-configurar</code> en este equipo.</p>')
            cuerpo = (f'<div class=card>{estado}<form method=post action=/ml/sincronizar>{self._csrf(s)}'
                      f'<label>Traer pedidos desde<input type=date name=desde value="{date.today().replace(day=1).isoformat()}"></label>'
                      f'<p><button {"disabled" if not conf else ""}>Sincronizar pedidos</button></p></form>'
                      f'<p><small>Sólo lectura. Importe a facturar = lo pagado por el comprador (ítems + envío a su cargo), nunca el neto de comisiones. '
                      f'Se marca como facturado externamente todo pedido que ya tenga documento fiscal en ML. '
                      f'Las cancelaciones se marcan para revisión; no generan nota de crédito automática.</small></p></div>')
            self._enviar(pagina(cfg, "Mercado Libre", cuerpo, s["usuario"], self._flash(s)))

        def p_exportar(self, con, s, q):
            hoy = date.today()
            cuerpo = (f'<form method=get action=/descargar class=card><div class=grid>'
                      f'<label>Desde<input type=date name=desde value="{hoy.replace(day=1).isoformat()}"></label>'
                      f'<label>Hasta<input type=date name=hasta value="{hoy.isoformat()}"></label>'
                      f'<label>Formato<select name=formato><option value=csv>CSV para el contador</option>'
                      f'<option value=zip>ZIP con CSV + PDF</option></select></label></div><p><button>Descargar</button></p></form>'
                      f'<p><small>Las notas de crédito se exportan con importes negativos. La columna «simulado» distingue pruebas.</small></p>')
            self._enviar(pagina(cfg, "Exportar", cuerpo, s["usuario"], self._flash(s)))

        def p_sistema(self, con, s, q):
            e = cfg.emisor
            faltan = e.faltantes()
            au = cfg.automatico
            cuerpo = (f'<div class=card><h3>Emisor</h3><p>{E(e.razon_social or "SIN CONFIGURAR")} · CUIT {E(e.cuit or "—")} · '
                      f'{E(e.condicion_iva or "—")} {"· clase M" if e.emite_clase_m else ""}</p>'
                      f'{"<p class=err>Faltan: " + E(", ".join(faltan)) + "</p>" if faltan else ""}'
                      f'<p>Entorno: <b>{E(cfg.entorno)}</b>{" (simulador local)" if cfg.simulado else ""} · Punto de venta: <b>{cfg.arca.punto_venta or "—"}</b></p>'
                      f'<p>Emisión automática: <b>{"ACTIVADA" if au.habilitado and au.reglas_confirmadas_por else "desactivada"}</b> · '
                      f'Envío de comprobantes a clientes: <b>{"habilitado" if cfg.envio_clientes_habilitado else "deshabilitado"}</b></p></div>'
                      f'<form method=post action=/arca/probar class=card>{self._csrf(s)}<button class=sec>Probar conexión con ARCA (FEDummy + último comprobante)</button></form>'
                      f'<form method=post action=/automatico class=card>{self._csrf(s)}<button class=sec {"disabled" if not au.habilitado else ""}>Procesar emisión automática ahora</button></form>'
                      f'<form method=post action=/backup class=card>{self._csrf(s)}<button class=sec>Crear copia de seguridad</button></form>')
            self._enviar(pagina(cfg, "Sistema", cuerpo, s["usuario"], self._flash(s)))

        def p_pdf(self, con, s, q):
            c = db.comprobante(con, int(q["id"]))
            if not c or c["estado"] != "autorizado":
                return self._enviar(b"Comprobante no autorizado", "text/plain", 404)
            if not c["pdf_path"] or not Path(c["pdf_path"]).exists():
                Facturador(cfg, con).regenerar_pdf(c["id"])
                c = db.comprobante(con, c["id"])
            db.evento(con, s["usuario"], "pdf_descargado", c["venta_id"], c["id"])
            self._enviar(Path(c["pdf_path"]).read_bytes(), "application/pdf",
                         extra={"Content-Disposition": f'attachment; filename="{Path(c["pdf_path"]).name}"'})

        def p_descargar(self, con, s, q):
            d, h = q.get("desde", "2000-01-01"), q.get("hasta", "2999-12-31")
            db.evento(con, s["usuario"], "exportacion", detalle=f"{d} a {h} {q.get('formato')}")
            if q.get("formato") == "zip":
                return self._enviar(exportar.zip_pdfs(con, d, h), "application/zip",
                                    extra={"Content-Disposition": f'attachment; filename="comprobantes_{cfg.entorno}_{d}_{h}.zip"'})
            self._enviar(("﻿" + exportar.csv_contador(con, d, h)).encode(), "text/csv; charset=utf-8",
                         extra={"Content-Disposition": f'attachment; filename="comprobantes_{cfg.entorno}_{d}_{h}.csv"'})

        # -------------------------------------------------------------- POST
        def _post(self):
            u = urllib.parse.urlparse(self.path)
            form = self._form()
            if u.path == "/login":
                con = db.conectar(cfg.db_path)
                ok = usuarios.verificar(con, form.get("usuario", ""), form.get("clave", ""))
                if ok:
                    db.evento(con, form["usuario"], "ingreso")
                con.close()
                if not ok:
                    return self._login_form("Usuario o clave incorrectos.")
                sid = secrets.token_urlsafe(32)
                with _lock_sesiones:
                    SESIONES[sid] = {"sid": sid, "usuario": form["usuario"], "csrf": secrets.token_urlsafe(24)}
                self.send_response(303)
                self.send_header("Set-Cookie", f"sid={sid}; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/")
                self.end_headers()
                return
            s = self._sesion()
            if s is None:
                return self._redir("/login")
            if not secrets.compare_digest(form.get("csrf", ""), s["csrf"]):
                return self._enviar(b"Solicitud invalida (CSRF)", "text/plain", 403)
            con = db.conectar(cfg.db_path)
            try:
                self._acciones(u.path, form, con, s)
            finally:
                con.close()

        def _acciones(self, ruta, form, con, s):
            us = s["usuario"]
            f = Facturador(cfg, con)
            vid = form.get("id", "")
            volver = f"/venta?id={vid}"
            try:
                if ruta == "/emitir":
                    c = f.emitir(vid, us, "manual", form.get("confirmacion", ""))
                    msg = {"autorizado": (True, f"Autorizado. CAE {E(c['cae'] or '')} — <a href='/pdf?id={c['id']}'>descargar PDF</a>"),
                           "rechazado": (False, "ARCA rechazó el comprobante. Revisá los errores, corregí y volvé a emitir."),
                           "incierto": (False, "Sin respuesta segura de ARCA. Quedó pendiente de conciliación: no se reintenta solo."),
                           "conflicto": (False, "Conflicto de numeración: el número está usado por otro comprobante.")}
                    return self._redir(volver, msg.get(c["estado"], (False, c["estado"])), s)
                if ruta == "/conciliar":
                    c = f.conciliar(int(form["cid"]), us)
                    return self._redir(f"/venta?id={c['venta_id']}", (c["estado"] == "autorizado", f"Resultado de la consulta: {E(c['estado'])}"), s)
                if ruta == "/resolver":
                    c = db.comprobante(con, int(form["cid"]))
                    f.resolver_conflicto(c["id"], us, form.get("explicacion", ""))
                    return self._redir(f"/venta?id={c['venta_id']}", (True, "Venta liberada para reemitir."), s)
                if ruta == "/nc":
                    fac = db.comprobante(con, int(form["cid"]))
                    c = f.nota_credito(fac["id"], us, form.get("motivo", ""), form.get("importe") or None, form.get("confirmacion", ""))
                    return self._redir(f"/venta?id={fac['venta_id']}", (c["estado"] == "autorizado", f"Nota de crédito: {E(c['estado'])}"), s)
                if ruta == "/editar":
                    self._guardar_edicion(con, form, us)
                    f.revalidar(vid, us)
                    return self._redir(volver, (True, "Datos guardados y revalidados."), s)
                if ruta == "/confirmar":
                    db.set_estado(con, vid, "pendiente_datos", us, "Borrador confirmado por el operador")
                    f.revalidar(vid, us)
                    return self._redir(volver, (True, "Borrador confirmado."), s)
                if ruta == "/verificacion":
                    db.registrar_verificacion_manual(con, vid, us, form.get("nota", ""))
                    f.revalidar(vid, us)
                    return self._redir(volver, (True, "Verificación registrada."), s)
                if ruta == "/externa":
                    db.actualizar_venta(con, vid, {"facturada_externamente": form["referencia"]}, us)
                    db.set_estado(con, vid, "facturada_externamente", us, form["referencia"])
                    return self._redir(volver, (True, "Marcada como facturada en otra herramienta."), s)
                if ruta == "/descartar":
                    db.set_estado(con, vid, "descartada", us, form.get("motivo", ""))
                    return self._redir(volver, (True, "Venta descartada."), s)
                if ruta == "/nueva":
                    pedido = form.get("pedido") or f"{form['canal'][:2].upper()}-{date.today():%Y%m%d}-{secrets.token_hex(3)}"
                    nid, creado = db.guardar_venta(con, {"canal": form["canal"], "pedido_externo": pedido,
                                                         "fecha_venta": form.get("fecha") or date.today().isoformat()}, us)
                    return self._redir(f"/venta?id={nid}", (creado, "Venta creada." if creado else "Ese pedido ya existía."), s)
                if ruta == "/importar":
                    res = csv_import.importar(con, form.get("csv", ""), self.server.app.catalogo(), us)
                    for p in con.execute("SELECT id FROM ventas WHERE estado='pendiente_datos'"):
                        f.revalidar(p["id"], us)
                    txt = (f"Nuevos: {len(res['nuevos'])}. Ya existentes (no duplicados): {len(res['repetidos'])}. "
                           + (" Errores: " + E(" | ".join(res["errores"])) if res["errores"] else ""))
                    return self._redir("/", (not res["errores"], txt), s)
                if ruta == "/whatsapp":
                    b = whatsapp.borrador(form.get("mensaje", ""), self.server.app.catalogo(), form.get("telefono", ""))
                    b["pedido_externo"] = form.get("pedido") or f"WA-{date.today():%Y%m%d}-{secrets.token_hex(3)}"
                    nid, creado = db.guardar_venta(con, b, us)
                    return self._redir(f"/venta?id={nid}", (creado, "Borrador creado: completá y confirmá los datos."), s)
                if ruta == "/ml/sincronizar":
                    from .canales.mercadolibre import ClienteML, sincronizar
                    cli = ClienteML(cfg.datos / "mercadolibre.json")
                    res = sincronizar(con, cli, self.server.app.catalogo(), form["desde"] + "T00:00:00.000-03:00", us)
                    for p in con.execute("SELECT id FROM ventas WHERE estado='pendiente_datos'"):
                        f.revalidar(p["id"], us)
                    return self._redir("/mercadolibre", (True, E(f"Nuevos {len(res['nuevos'])}, actualizados {len(res['actualizados'])}, "
                                                                f"cancelados con factura a revisar: {', '.join(res['revisar_cancelacion']) or 'ninguno'}")), s)
                if ruta == "/arca/probar":
                    ws = f.ws
                    d = ws.dummy()
                    extra = ""
                    if cfg.arca.punto_venta:
                        for t in (fiscal.FACTURA_A, fiscal.FACTURA_B):
                            extra += f" · Último {fiscal.nombre_tipo(t)}: {ws.ultimo_autorizado(cfg.arca.punto_venta, t)}"
                    return self._redir("/sistema", (True, E(f"ARCA responde: {d}{extra}")), s)
                if ruta == "/automatico":
                    res = automatico.procesar(f)
                    return self._redir("/sistema", (True, E(f"Emitidas: {res['emitidas']} · Omitidas: {len(res['omitidas'])}")), s)
                if ruta == "/backup":
                    p = backup.crear(cfg)
                    db.evento(con, us, "backup", detalle=p.name)
                    return self._redir("/sistema", (True, E(f"Copia creada: {p}")), s)
                self._enviar(b"No encontrado", "text/plain", 404)
            except EmisionBloqueada as e:
                det = ""
                if e.resultado:
                    det = "<ul>" + "".join(f"<li>{E(p.mensaje)} — {E(p.como_resolver)}</li>" for p in e.resultado.bloqueantes) + "</ul>"
                self._redir(volver, (False, E(str(e)) + det), s)
            except (ErrorEmision, PermissionError, ValueError, KeyError) as e:
                self._redir(volver if vid else "/", (False, E(str(e))), s)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._redir(volver if vid else "/sistema", (False, E(f"{e.__class__.__name__}: {e}")), s)

        def _guardar_edicion(self, con, form, us):
            vid = form["id"]
            v = db.venta(con, vid)
            L = lambda k: form.get(k, []) if isinstance(form.get(k), list) else [form.get(k, "")]  # noqa: E731
            anteriores = {(it.get("descripcion"), str(it.get("alicuota"))): it.get("alicuota_confirmada", False) for it in v["items"]}
            cat = self.server.app.catalogo()
            items = []
            for d_, sku, cant, precio, descu, alic, envio in zip(L("desc[]"), L("sku[]"), L("cant[]"), L("precio[]"),
                                                                  L("descu[]"), L("alic[]"), L("envio[]")):
                if not d_.strip():
                    continue
                it = {"descripcion": d_.strip(), "sku": sku.strip(), "cantidad": cant.strip() or "1",
                      "precio_unitario_final": precio.strip(), "descuento_final": descu.strip() or "0",
                      "alicuota": alic.strip().replace("%", ""), "es_envio": envio == "1"}
                prod = cat.por_sku(it["sku"])
                if prod:
                    cat.completar_item(it, prod)
                else:
                    it["alicuota_confirmada"] = anteriores.get((it["descripcion"], it["alicuota"]), False)
                items.append(it)
            cond = form.get("condicion_iva", "")
            db.actualizar_venta(con, vid, {
                "cliente": dict(v["cliente"], nombre=form.get("nombre", "").strip(),
                                tipo_doc=int(form["tipo_doc"]) if form.get("tipo_doc") else None,
                                nro_doc=form.get("nro_doc", "").strip(), condicion_iva=int(cond) if cond else "",
                                domicilio=form.get("domicilio", "").strip()),
                "items": items, "fecha_venta": form.get("fecha_venta") or v["fecha_venta"],
                "estado_pago": form.get("estado_pago", v["estado_pago"]),
                "descuento_general": form.get("descuento_general") or "0",
                "total_informado": form.get("total_informado") or None,
            }, us)

    return H


def servir(cfg: Config, puerto: int = 8765) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", puerto), crear_handler(App(cfg)))
    srv.app = App(cfg)
    print(f"BarcoFact [{cfg.entorno.upper()}{' · SIMULADOR' if cfg.simulado else ''}] en http://127.0.0.1:{puerto}")
    srv.serve_forever()
