"""Persistencia en SQLite (un archivo por entorno, permisos 600)."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS ventas (
    id TEXT PRIMARY KEY,
    canal TEXT NOT NULL,                 -- mercadolibre | whatsapp | manual | csv
    pedido_externo TEXT NOT NULL,        -- n° de pedido del canal (o generado para manual)
    fecha_venta TEXT NOT NULL,           -- AAAA-MM-DD
    estado_pago TEXT NOT NULL DEFAULT 'desconocido',      -- acreditado | pendiente | desconocido
    estado_operacion TEXT NOT NULL DEFAULT 'activa',      -- activa | cancelada | devuelta
    estado TEXT NOT NULL DEFAULT 'borrador',
    cliente_json TEXT NOT NULL DEFAULT '{}',
    items_json TEXT NOT NULL DEFAULT '[]',
    descuento_general TEXT NOT NULL DEFAULT '0',
    moneda TEXT NOT NULL DEFAULT 'PES',
    total_informado TEXT,                -- total que el canal dice que pagó el comprador
    datos_canal_json TEXT NOT NULL DEFAULT '{}',
    facturada_externamente TEXT,         -- referencia si la facturó otra herramienta
    notas TEXT NOT NULL DEFAULT '',
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL,
    creado_por TEXT NOT NULL,
    UNIQUE (canal, pedido_externo)
);

CREATE TABLE IF NOT EXISTS comprobantes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venta_id TEXT NOT NULL REFERENCES ventas(id),
    clase TEXT NOT NULL,                 -- factura | nota_credito | nota_debito
    entorno TEXT NOT NULL,
    cuit TEXT NOT NULL,
    pto_vta INTEGER NOT NULL,
    tipo INTEGER NOT NULL,
    numero INTEGER,
    fecha_cbte TEXT NOT NULL,
    concepto INTEGER NOT NULL,
    doc_tipo INTEGER NOT NULL,
    doc_nro TEXT NOT NULL,
    cond_iva_receptor INTEGER NOT NULL,
    importes_json TEXT NOT NULL,
    detalle_json TEXT NOT NULL,          -- líneas, receptor y emisor tal como se facturan
    solicitud_json TEXT,
    estado TEXT NOT NULL,                -- enviando | autorizado | rechazado | incierto | conflicto
    resultado TEXT,
    cae TEXT,
    cae_vto TEXT,
    observaciones_json TEXT,
    errores_json TEXT,
    respuesta_xml TEXT,
    asociado_id INTEGER REFERENCES comprobantes(id),
    motivo TEXT,
    aprobado_por TEXT NOT NULL,
    aprobado_en TEXT NOT NULL,
    modo TEXT NOT NULL,                  -- manual | automatico
    simulado INTEGER NOT NULL DEFAULT 0, -- 1 = emitido contra el simulador local (sin validez)
    pdf_path TEXT,
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL
);

-- Numeración única por entorno, CUIT, punto de venta y tipo. Los intentos rechazados
-- (que ARCA no autorizó) conservan su número como historial y no bloquean el reintento.
CREATE UNIQUE INDEX IF NOT EXISTS ux_numeracion_viva
    ON comprobantes(entorno, cuit, pto_vta, tipo, numero)
    WHERE numero IS NOT NULL AND estado IN ('enviando', 'autorizado', 'incierto', 'conflicto');

-- Una venta no puede tener dos facturas vivas (enviando/autorizada/incierta/conflicto).
CREATE UNIQUE INDEX IF NOT EXISTS ux_factura_viva
    ON comprobantes(venta_id) WHERE clase = 'factura'
    AND estado IN ('enviando', 'autorizado', 'incierto', 'conflicto');

-- Último número emitido por OTRA herramienta en un punto de venta, aceptado expresamente.
CREATE TABLE IF NOT EXISTS numeracion_externa (
    entorno TEXT NOT NULL, cuit TEXT NOT NULL, pto_vta INTEGER NOT NULL, tipo INTEGER NOT NULL,
    ultimo_aceptado INTEGER NOT NULL, aceptado_por TEXT NOT NULL, aceptado_en TEXT NOT NULL,
    PRIMARY KEY (entorno, cuit, pto_vta, tipo)
);

CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    usuario TEXT NOT NULL,
    accion TEXT NOT NULL,
    venta_id TEXT,
    comprobante_id INTEGER,
    detalle TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_eventos_venta ON eventos(venta_id);

CREATE TABLE IF NOT EXISTS usuarios (
    usuario TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    sal TEXT NOT NULL,
    creado TEXT NOT NULL
);
"""

ESTADOS_VENTA = {
    "borrador": "Borrador (no emitible)",
    "pendiente_datos": "Faltan datos",
    "lista": "Lista para revisar",
    "en_proceso": "En proceso",
    "facturada": "Facturada",
    "error": "Error / rechazo",
    "conciliar": "Pendiente de conciliación",
    "facturada_externamente": "Facturada por otra herramienta",
    "descartada": "Descartada",
}


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def conectar(path: Path) -> sqlite3.Connection:
    nuevo = not path.exists()
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    if nuevo:
        os.chmod(path, 0o600)
    return con


def evento(con, usuario: str, accion: str, venta_id=None, comprobante_id=None, detalle="") -> None:
    con.execute("INSERT INTO eventos(ts,usuario,accion,venta_id,comprobante_id,detalle) VALUES (?,?,?,?,?,?)",
                (ahora(), usuario, accion, venta_id, comprobante_id, detalle))


def nuevo_id() -> str:
    return uuid.uuid4().hex


def venta(con, venta_id: str) -> dict | None:
    row = con.execute("SELECT * FROM ventas WHERE id=?", (venta_id,)).fetchone()
    return _venta_dict(row) if row else None


def _venta_dict(row) -> dict:
    d = dict(row)
    d["cliente"] = json.loads(d.pop("cliente_json"))
    d["items"] = json.loads(d.pop("items_json"))
    d["datos_canal"] = json.loads(d.pop("datos_canal_json"))
    return d


def buscar_ventas(con, estado=None, texto=None, desde=None, hasta=None, limite=500) -> list[dict]:
    sql, args = "SELECT * FROM ventas WHERE 1=1", []
    if estado:
        sql += " AND estado=?"
        args.append(estado)
    if texto:
        sql += " AND (pedido_externo LIKE ? OR cliente_json LIKE ? OR id LIKE ?)"
        args += [f"%{texto}%"] * 3
    if desde:
        sql += " AND fecha_venta>=?"
        args.append(desde)
    if hasta:
        sql += " AND fecha_venta<=?"
        args.append(hasta)
    sql += " ORDER BY fecha_venta DESC, creado DESC LIMIT ?"
    args.append(limite)
    return [_venta_dict(r) for r in con.execute(sql, args)]


def comprobantes_de(con, venta_id: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM comprobantes WHERE venta_id=? ORDER BY id", (venta_id,))]


def comprobante(con, cid: int) -> dict | None:
    r = con.execute("SELECT * FROM comprobantes WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def guardar_venta(con, datos: dict, usuario: str) -> tuple[str, bool]:
    """Inserta una venta. Si (canal, pedido) ya existe devuelve el id existente y False."""
    existente = con.execute("SELECT id FROM ventas WHERE canal=? AND pedido_externo=?",
                            (datos["canal"], str(datos["pedido_externo"]))).fetchone()
    if existente:
        return existente["id"], False
    vid = nuevo_id()
    t = ahora()
    con.execute(
        """INSERT INTO ventas(id,canal,pedido_externo,fecha_venta,estado_pago,estado_operacion,estado,
           cliente_json,items_json,descuento_general,moneda,total_informado,datos_canal_json,
           facturada_externamente,notas,creado,actualizado,creado_por)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (vid, datos["canal"], str(datos["pedido_externo"]), datos["fecha_venta"],
         datos.get("estado_pago", "desconocido"), datos.get("estado_operacion", "activa"),
         datos.get("estado", "pendiente_datos"),
         json.dumps(datos.get("cliente", {}), ensure_ascii=False),
         json.dumps(datos.get("items", []), ensure_ascii=False),
         str(datos.get("descuento_general", "0")), datos.get("moneda", "PES"),
         None if datos.get("total_informado") is None else str(datos["total_informado"]),
         json.dumps(datos.get("datos_canal", {}), ensure_ascii=False),
         datos.get("facturada_externamente"), datos.get("notas", ""), t, t, usuario))
    evento(con, usuario, "venta_creada", vid, detalle=f"{datos['canal']} {datos['pedido_externo']}")
    return vid, True


CAMPOS_EDITABLES = {"cliente", "items", "descuento_general", "estado_pago", "estado_operacion",
                    "fecha_venta", "notas", "facturada_externamente", "moneda", "total_informado"}


def actualizar_venta(con, venta_id: str, cambios: dict, usuario: str) -> None:
    v = venta(con, venta_id)
    if v is None:
        raise KeyError(venta_id)
    if v["estado"] in ("facturada", "en_proceso", "conciliar"):
        raise PermissionError("No se puede editar una venta facturada o en proceso.")
    sets, args = [], []
    for k, val in cambios.items():
        if k not in CAMPOS_EDITABLES:
            raise KeyError(f"Campo no editable: {k}")
        col = {"cliente": "cliente_json", "items": "items_json"}.get(k, k)
        if k in ("cliente", "items"):
            val = json.dumps(val, ensure_ascii=False)
        sets.append(f"{col}=?")
        args.append(None if val is None else str(val) if not isinstance(val, str) else val)
    sets.append("actualizado=?")
    args += [ahora(), venta_id]
    con.execute(f"UPDATE ventas SET {', '.join(sets)} WHERE id=?", args)
    evento(con, usuario, "venta_editada", venta_id, detalle=", ".join(sorted(cambios)))


def set_estado(con, venta_id: str, estado: str, usuario: str, detalle: str = "") -> None:
    assert estado in ESTADOS_VENTA, estado
    con.execute("UPDATE ventas SET estado=?, actualizado=? WHERE id=?", (estado, ahora(), venta_id))
    evento(con, usuario, f"estado:{estado}", venta_id, detalle=detalle)


def registrar_verificacion_manual(con, venta_id: str, usuario: str, nota: str) -> None:
    """El operador declara haber verificado que ninguna otra herramienta facturó la venta."""
    if not nota.strip():
        raise ValueError("Indicá cómo verificaste (por ejemplo: revisé el pedido en ML y no tiene factura).")
    v = venta(con, venta_id)
    dc = dict(v["datos_canal"], verificacion_manual={"usuario": usuario, "fecha": ahora(), "nota": nota})
    con.execute("UPDATE ventas SET datos_canal_json=?, actualizado=? WHERE id=?",
                (json.dumps(dc, ensure_ascii=False), ahora(), venta_id))
    evento(con, usuario, "verificacion_manual_no_facturada", venta_id, detalle=nota)
