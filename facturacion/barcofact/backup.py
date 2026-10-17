"""Copias de seguridad y recuperación.

La copia incluye la base del entorno, los PDF y config.toml. NO incluye
certificados, claves privadas ni tokens: guardalos aparte en un lugar seguro
(por ejemplo, un gestor de contraseñas o un pendrive cifrado).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from .config import Config

CONSERVAR = 60


def crear(cfg: Config, destino_extra: Path | None = None) -> Path:
    marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    cfg.dir_backups.mkdir(parents=True, exist_ok=True)
    tmp_db = cfg.dir_backups / f".tmp-{marca}.sqlite"
    from . import db
    src = db.conectar(cfg.db_path)  # crea el esquema si la base todavía no existe
    dst = sqlite3.connect(tmp_db)
    with dst:
        src.backup(dst)  # copia consistente aunque la base esté en uso
    src.close()
    dst.close()
    salida = cfg.dir_backups / f"barcofact-{cfg.entorno}-{marca}.zip"
    fd = os.open(salida, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp_db, "barcofact.sqlite")
        for pdf in cfg.dir_comprobantes.glob("*.pdf"):
            z.write(pdf, f"comprobantes/{pdf.name}")
        for nombre in ("config.toml", "catalogo.csv"):
            if (cfg.datos / nombre).exists():
                z.write(cfg.datos / nombre, nombre)
    tmp_db.unlink()
    copias = sorted(cfg.dir_backups.glob(f"barcofact-{cfg.entorno}-*.zip"))
    for vieja in copias[:-CONSERVAR]:
        vieja.unlink()
    if destino_extra:
        shutil.copy2(salida, destino_extra)
    return salida


def verificar(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        tmp = zip_path.with_suffix(".verif.sqlite")
        tmp.write_bytes(z.read("barcofact.sqlite"))
    try:
        con = sqlite3.connect(tmp)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        n = con.execute("SELECT COUNT(*) FROM comprobantes WHERE estado='autorizado'").fetchone()[0]
        con.close()
    finally:
        tmp.unlink()
    return {"integridad": ok, "autorizados": n}


def restaurar(cfg: Config, zip_path: Path, forzar: bool = False) -> None:
    info = verificar(zip_path)
    if info["integridad"] != "ok":
        raise RuntimeError(f"La copia está dañada: {info['integridad']}")
    if cfg.db_path.exists():
        con = sqlite3.connect(cfg.db_path)
        actuales = con.execute("SELECT COUNT(*) FROM comprobantes WHERE estado='autorizado'").fetchone()[0]
        con.close()
        if actuales > info["autorizados"] and not forzar:
            raise RuntimeError(
                f"La base actual tiene {actuales} comprobantes autorizados y la copia {info['autorizados']}. "
                "Restaurarla perdería registros: después habría que conciliar con ARCA. Usá --forzar si estás seguro.")
        crear(cfg)  # copia de lo actual antes de pisarlo
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n == "barcofact.sqlite":
                cfg.db_path.write_bytes(z.read(n))
                os.chmod(cfg.db_path, 0o600)
                for sufijo in ("-wal", "-shm"):
                    p = Path(str(cfg.db_path) + sufijo)
                    if p.exists():
                        p.unlink()
            elif n.startswith("comprobantes/"):
                (cfg.dir_comprobantes / Path(n).name).write_bytes(z.read(n))
