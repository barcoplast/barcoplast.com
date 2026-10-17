"""Usuarios locales para registrar quién aprueba y emite cada comprobante."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from . import db

ITER = 390_000


def _hash(clave: str, sal: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", clave.encode(), bytes.fromhex(sal), ITER).hex()


def crear(con, usuario: str, clave: str) -> None:
    if len(clave) < 10:
        raise ValueError("La clave debe tener al menos 10 caracteres.")
    sal = secrets.token_hex(16)
    con.execute("INSERT OR REPLACE INTO usuarios VALUES (?,?,?,?)", (usuario, _hash(clave, sal), sal, db.ahora()))
    db.evento(con, usuario, "usuario_creado")


def verificar(con, usuario: str, clave: str) -> bool:
    r = con.execute("SELECT hash, sal FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
    if not r:
        _hash(clave, "00" * 16)  # tiempo constante aproximado
        return False
    return hmac.compare_digest(r["hash"], _hash(clave, r["sal"]))


def hay_usuarios(con) -> bool:
    return con.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is not None
