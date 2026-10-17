"""Cliente WSFEv1 (Factura electrónica A/B/M, mercado interno, RG 4291 y modif.)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from . import transporte

NS = "http://ar.gov.afip.dif.FEV1/"
ERROR_NO_EXISTE = "602"  # FECompConsultar: "No existen datos en nuestros registros..."


@dataclass
class RespuestaCAE:
    resultado: str  # A aprobado | R rechazado | P parcial
    numero: int | None = None
    cae: str = ""
    cae_vto: str = ""
    observaciones: list[tuple[str, str]] = field(default_factory=list)
    errores: list[tuple[str, str]] = field(default_factory=list)
    eventos: list[tuple[str, str]] = field(default_factory=list)
    xml: str = ""


@dataclass
class ComprobanteConsultado:
    numero: int
    fecha: str
    doc_tipo: int
    doc_nro: str
    imp_total: str
    resultado: str
    cae: str
    cae_vto: str
    xml: str = ""


class ErrorArca(Exception):
    def __init__(self, errores: list[tuple[str, str]]):
        self.errores = errores
        super().__init__("; ".join(f"[{c}] {m}" for c, m in errores))


def _codigos(raiz, contenedor, item) -> list[tuple[str, str]]:
    cont = transporte.buscar(raiz, contenedor)
    if cont is None:
        return []
    return [(transporte.texto(e, "Code"), transporte.texto(e, "Msg"))
            for e in cont if e.tag.split("}")[-1] == item]


def _x(tag: str, valor) -> str:
    return f"<ar:{tag}>{escape(str(valor))}</ar:{tag}>"


def xml_detalle(s: dict) -> str:
    """Arma FECAEDetRequest respetando el orden del esquema."""
    p = [_x("Concepto", s["concepto"]), _x("DocTipo", s["doc_tipo"]), _x("DocNro", s["doc_nro"]),
         _x("CbteDesde", s["numero"]), _x("CbteHasta", s["numero"]), _x("CbteFch", s["fecha"]),
         _x("ImpTotal", s["imp_total"]), _x("ImpTotConc", s["imp_tot_conc"]), _x("ImpNeto", s["imp_neto"]),
         _x("ImpOpEx", s["imp_op_ex"]), _x("ImpTrib", s["imp_trib"]), _x("ImpIVA", s["imp_iva"])]
    for k, tag in (("fch_serv_desde", "FchServDesde"), ("fch_serv_hasta", "FchServHasta"),
                   ("fch_vto_pago", "FchVtoPago")):
        if s.get(k):
            p.append(_x(tag, s[k]))
    p += [_x("MonId", s["mon_id"]), _x("MonCotiz", s["mon_cotiz"])]
    p.append(_x("CondicionIVAReceptorId", s["cond_iva_receptor"]))
    if s.get("asociados"):
        p.append("<ar:CbtesAsoc>" + "".join(
            "<ar:CbteAsoc>" + _x("Tipo", a["tipo"]) + _x("PtoVta", a["pto_vta"]) + _x("Nro", a["nro"])
            + _x("Cuit", a["cuit"]) + _x("CbteFch", a["fecha"]) + "</ar:CbteAsoc>"
            for a in s["asociados"]) + "</ar:CbtesAsoc>")
    if s.get("iva"):
        p.append("<ar:Iva>" + "".join(
            "<ar:AlicIva>" + _x("Id", i["id"]) + _x("BaseImp", i["base"]) + _x("Importe", i["importe"])
            + "</ar:AlicIva>" for i in s["iva"]) + "</ar:Iva>")
    return "<ar:FECAEDetRequest>" + "".join(p) + "</ar:FECAEDetRequest>"


class Wsfe:
    def __init__(self, url: str, cuit: str, credenciales):
        """`credenciales` es una función que devuelve (token, sign) vigentes."""
        self.url, self.cuit, self._cred = url, cuit, credenciales
        self.sesion = transporte.sesion()

    def _llamar(self, metodo: str, interior: str, auth: bool = True) -> ET.Element:
        a = ""
        if auth:
            token, sign = self._cred()
            a = f"<ar:Auth>{_x('Token', token)}{_x('Sign', sign)}{_x('Cuit', self.cuit)}</ar:Auth>"
        cuerpo = (f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ar="{NS}">'
                  f"<soapenv:Header/><soapenv:Body><ar:{metodo}>{a}{interior}</ar:{metodo}>"
                  "</soapenv:Body></soapenv:Envelope>")
        return transporte.post_soap(self.sesion, self.url, NS + metodo, cuerpo)

    def dummy(self) -> dict:
        r = self._llamar("FEDummy", "", auth=False)
        return {k: transporte.texto(r, k) for k in ("AppServer", "DbServer", "AuthServer")}

    def ultimo_autorizado(self, pto_vta: int, tipo: int) -> int:
        r = self._llamar("FECompUltimoAutorizado", _x("PtoVta", pto_vta) + _x("CbteTipo", tipo))
        errores = _codigos(r, "Errors", "Err")
        if errores:
            raise ErrorArca(errores)
        return int(transporte.texto(r, "CbteNro") or 0)

    def consultar(self, pto_vta: int, tipo: int, numero: int) -> ComprobanteConsultado | None:
        r = self._llamar("FECompConsultar", "<ar:FeCompConsReq>" + _x("CbteTipo", tipo) + _x("CbteNro", numero)
                         + _x("PtoVta", pto_vta) + "</ar:FeCompConsReq>")
        errores = _codigos(r, "Errors", "Err")
        if any(c == ERROR_NO_EXISTE for c, _ in errores):
            return None
        if errores:
            raise ErrorArca(errores)
        g = transporte.buscar(r, "ResultGet")
        if g is None:
            return None
        return ComprobanteConsultado(
            numero=int(transporte.texto(g, "CbteDesde")), fecha=transporte.texto(g, "CbteFch"),
            doc_tipo=int(transporte.texto(g, "DocTipo") or 0), doc_nro=transporte.texto(g, "DocNro"),
            imp_total=transporte.texto(g, "ImpTotal"), resultado=transporte.texto(g, "Resultado"),
            cae=transporte.texto(g, "CodAutorizacion"), cae_vto=transporte.texto(g, "FchVto"),
            xml=ET.tostring(g, encoding="unicode"))

    def solicitar_cae(self, s: dict) -> RespuestaCAE:
        interior = ("<ar:FeCAEReq><ar:FeCabReq>" + _x("CantReg", 1) + _x("PtoVta", s["pto_vta"])
                    + _x("CbteTipo", s["tipo"]) + "</ar:FeCabReq><ar:FeDetReq>" + xml_detalle(s)
                    + "</ar:FeDetReq></ar:FeCAEReq>")
        r = self._llamar("FECAESolicitar", interior)
        det = transporte.buscar(r, "FECAEDetResponse")
        resp = RespuestaCAE(
            resultado=transporte.texto(det, "Resultado") or transporte.texto(r, "Resultado") or "R",
            errores=_codigos(r, "Errors", "Err"),
            observaciones=_codigos(r, "Observaciones", "Obs"),
            eventos=_codigos(r, "Events", "Evt"),
            xml=ET.tostring(r, encoding="unicode"))
        if det is not None:
            nro = transporte.texto(det, "CbteDesde")
            resp.numero = int(nro) if nro else None
            resp.cae = transporte.texto(det, "CAE")
            resp.cae_vto = transporte.texto(det, "CAEFchVto")
        return resp

    def parametros(self, metodo: str, extra: str = "") -> list[dict]:
        """Ej.: FEParamGetTiposCbte, FEParamGetPtosVenta, FEParamGetCondicionIvaReceptor."""
        r = self._llamar(metodo, extra)
        res = transporte.buscar(r, "ResultGet")
        errores = _codigos(r, "Errors", "Err")
        if errores and res is None:
            raise ErrorArca(errores)
        if res is None:
            return []
        return [{c.tag.split("}")[-1]: (c.text or "") for c in item} for item in res]
