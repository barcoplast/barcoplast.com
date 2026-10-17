"""Transporte SOAP mínimo hacia los web services de ARCA."""
from __future__ import annotations

import os
import ssl
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter

TIMEOUT = (10, 60)  # conexión, lectura (segundos)


class ErrorTransporte(Exception):
    """No hubo respuesta completa: el resultado de la operación es DESCONOCIDO."""


class ErrorSoap(Exception):
    """ARCA respondió con un SOAP Fault."""


class _AdaptadorTlsLegado(HTTPAdapter):
    # Algunos servidores de ARCA usan parámetros DH que OpenSSL 3 rechaza por defecto.
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def sesion() -> requests.Session:
    s = requests.Session()
    if os.environ.get("BARCOFACT_TLS_LEGADO") == "1":
        s.mount("https://", _AdaptadorTlsLegado())
    return s


def post_soap(s: requests.Session, url: str, accion: str, cuerpo: str) -> ET.Element:
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": accion}
    try:
        resp = s.post(url, data=cuerpo.encode("utf-8"), headers=headers, timeout=TIMEOUT)
    except (requests.Timeout, requests.ConnectionError) as e:
        raise ErrorTransporte(f"Sin respuesta de {url}: {e.__class__.__name__}") from e
    try:
        raiz = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise ErrorTransporte(f"Respuesta ilegible de {url} (HTTP {resp.status_code})") from e
    fault = raiz.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        raise ErrorSoap(f"{_txt(fault, 'faultcode')}: {_txt(fault, 'faultstring')}")
    return raiz


def _txt(el: ET.Element, tag: str) -> str:
    for e in el.iter():
        if e.tag.split("}")[-1] == tag:
            return (e.text or "").strip()
    return ""


def buscar(el: ET.Element, tag: str) -> ET.Element | None:
    for e in el.iter():
        if e.tag.split("}")[-1] == tag:
            return e
    return None


def buscar_todos(el: ET.Element, tag: str) -> list[ET.Element]:
    return [e for e in el.iter() if e.tag.split("}")[-1] == tag]


def texto(el: ET.Element | None, tag: str) -> str:
    return _txt(el, tag) if el is not None else ""
