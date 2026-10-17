"""WSAA: autenticación con certificado (LoginCms).

El ticket de acceso (TA) se guarda en el directorio de datos con permisos 600 y se
reutiliza mientras esté vigente: WSAA rechaza pedir uno nuevo si hay otro válido.
"""
from __future__ import annotations

import base64
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7

from . import transporte

TZ_AR = timezone(timedelta(hours=-3))


class ErrorWsaa(Exception):
    pass


def crear_tra(servicio: str, ahora: datetime | None = None) -> bytes:
    ahora = ahora or datetime.now(TZ_AR)
    gen = (ahora - timedelta(minutes=10)).isoformat(timespec="seconds")
    exp = (ahora + timedelta(minutes=10)).isoformat(timespec="seconds")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<loginTicketRequest version="1.0"><header>'
        f"<uniqueId>{int(time.time())}</uniqueId>"
        f"<generationTime>{gen}</generationTime><expirationTime>{exp}</expirationTime>"
        f"</header><service>{escape(servicio)}</service></loginTicketRequest>"
    ).encode()


def firmar_cms(tra: bytes, cert_path: Path, key_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    clave = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    cms = (pkcs7.PKCS7SignatureBuilder().set_data(tra)
           .add_signer(cert, clave, hashes.SHA256())
           .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary]))
    return base64.b64encode(cms).decode()


class Wsaa:
    def __init__(self, url: str, cert_path: Path, key_path: Path, dir_ta: Path, cuit: str):
        self.url, self.cert, self.key, self.dir_ta, self.cuit = url, cert_path, key_path, dir_ta, cuit
        self.sesion = transporte.sesion()

    def _archivo(self, servicio: str) -> Path:
        return self.dir_ta / f"ta-{servicio}-{self.cuit}.json"

    def credenciales(self, servicio: str = "wsfe") -> tuple[str, str]:
        f = self._archivo(servicio)
        if f.exists():
            ta = json.loads(f.read_text())
            if datetime.fromisoformat(ta["expiracion"]) > datetime.now(TZ_AR) + timedelta(minutes=5):
                return ta["token"], ta["sign"]
        cms = firmar_cms(crear_tra(servicio), self.cert, self.key)
        cuerpo = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov"><soapenv:Header/><soapenv:Body>'
            f"<wsaa:loginCms><wsaa:in0>{cms}</wsaa:in0></wsaa:loginCms></soapenv:Body></soapenv:Envelope>"
        )
        try:
            raiz = transporte.post_soap(self.sesion, self.url, "", cuerpo)
        except transporte.ErrorSoap as e:
            if "alreadyAuthenticated" in str(e):
                raise ErrorWsaa("WSAA indica que ya existe un ticket vigente que no está en este equipo. "
                                "Esperá a que venza (hasta 12 h) o usá el equipo que lo obtuvo.") from e
            raise ErrorWsaa(f"WSAA rechazó la autenticación: {e}") from e
        ret = transporte.buscar(raiz, "loginCmsReturn")
        if ret is None or not ret.text:
            raise ErrorWsaa("Respuesta de WSAA sin ticket.")
        tr = ET.fromstring(ret.text.encode())
        ta = {
            "token": transporte.texto(tr, "token"),
            "sign": transporte.texto(tr, "sign"),
            "expiracion": transporte.texto(tr, "expirationTime"),
        }
        tmp = f.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(ta, fh)
        os.replace(tmp, f)
        return ta["token"], ta["sign"]
