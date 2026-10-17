"""Emisión automática (DESACTIVADA por defecto).

Se habilita en config.toml ([automatico] habilitado = true, reglas_confirmadas_por = "...").
En producción además requiere [produccion] habilitada = true. Sólo emite ventas que
cumplen TODAS las reglas sin ningún aviso; el resto queda para revisión manual.
"""
from __future__ import annotations

from . import db
from .emision import EmisionBloqueada, ErrorEmision, Facturador


def procesar(f: Facturador, limite: int = 50) -> dict:
    cfg = f.cfg
    if not cfg.automatico.habilitado or not cfg.automatico.reglas_confirmadas_por:
        return {"habilitado": False, "emitidas": [], "omitidas": []}
    emitidas, omitidas = [], []
    candidatas = [v for v in db.buscar_ventas(f.con, estado="pendiente_datos", limite=limite)
                  + db.buscar_ventas(f.con, estado="lista", limite=limite)
                  if v["canal"] in cfg.automatico.canales]
    for v in candidatas:
        try:
            c = f.emitir(v["id"], usuario="automatico", modo="automatico")
            emitidas.append((v["pedido_externo"], c["estado"]))
        except (EmisionBloqueada, ErrorEmision) as e:
            omitidas.append((v["pedido_externo"], str(e)))
    return {"habilitado": True, "emitidas": emitidas, "omitidas": omitidas}
