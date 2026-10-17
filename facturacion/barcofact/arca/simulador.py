"""Simulador LOCAL de WSFEv1 para pruebas sin conexión a ARCA.

Todo lo emitido con el simulador queda marcado como SIMULADO: no es una prueba
real de homologación ni un comprobante válido. Permite inyectar fallas
(cortes de conexión, rechazos, numeración tomada por otro sistema).
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from . import transporte
from .wsfe import ComprobanteConsultado, ErrorArca, RespuestaCAE


class SimuladorWsfe:
    def __init__(self, archivo: Path | None = None):
        self.archivo = archivo
        self.estado = {"emitidos": {}, "fallas": {}}
        if archivo and archivo.exists():
            self.estado = json.loads(archivo.read_text())

    # --- inyección de fallas (para pruebas) ---
    def inyectar(self, falla: str, valor=1) -> None:
        """Fallas: corte_antes, corte_despues, rechazo (valor=[codigo, msg]), otro_sistema (valor=n)."""
        self.estado["fallas"][falla] = valor
        self._guardar()

    def _tomar_falla(self, falla: str):
        v = self.estado["fallas"].pop(falla, None)
        self._guardar()
        return v

    def _guardar(self):
        if self.archivo:
            self.archivo.write_text(json.dumps(self.estado))

    def _clave(self, pv, tipo):
        return f"{pv}-{tipo}"

    def _lista(self, pv, tipo) -> dict:
        return self.estado["emitidos"].setdefault(self._clave(pv, tipo), {})

    # --- interfaz WSFEv1 ---
    def dummy(self):
        return {"AppServer": "SIMULADO", "DbServer": "SIMULADO", "AuthServer": "SIMULADO"}

    def ultimo_autorizado(self, pto_vta: int, tipo: int) -> int:
        n = self._tomar_falla("otro_sistema")
        if n:
            lista = self._lista(pto_vta, tipo)
            for _ in range(int(n)):
                nro = max(map(int, lista), default=0) + 1
                lista[str(nro)] = {"fecha": date.today().strftime("%Y%m%d"), "doc_tipo": 99, "doc_nro": "0",
                                   "imp_total": "1.00", "cae": "7" * 14, "cae_vto": "", "otro": True}
            self._guardar()
        return max(map(int, self._lista(pto_vta, tipo)), default=0)

    def consultar(self, pto_vta, tipo, numero):
        c = self._lista(pto_vta, tipo).get(str(numero))
        if not c:
            return None
        return ComprobanteConsultado(numero, c["fecha"], c["doc_tipo"], str(c["doc_nro"]), c["imp_total"],
                                     "A", c["cae"], c["cae_vto"], xml="<simulado/>")

    def solicitar_cae(self, s: dict) -> RespuestaCAE:
        if self._tomar_falla("corte_antes"):
            raise transporte.ErrorTransporte("SIMULADO: corte de conexión antes de llegar a ARCA")
        rechazo = self._tomar_falla("rechazo")
        if rechazo:
            return RespuestaCAE("R", s["numero"], errores=[tuple(rechazo)], xml="<simulado/>")
        errores = self._validar(s)
        if errores:
            return RespuestaCAE("R", s["numero"], errores=errores, xml="<simulado/>")
        cae = "".join(random.choice("0123456789") for _ in range(14))
        vto = (date.today() + timedelta(days=10)).strftime("%Y%m%d")
        self._lista(s["pto_vta"], s["tipo"])[str(s["numero"])] = {
            "fecha": s["fecha"], "doc_tipo": s["doc_tipo"], "doc_nro": str(s["doc_nro"]),
            "imp_total": s["imp_total"], "cae": cae, "cae_vto": vto}
        self._guardar()
        if self._tomar_falla("corte_despues"):
            raise transporte.ErrorTransporte("SIMULADO: corte de conexión después de autorizar")
        return RespuestaCAE("A", s["numero"], cae, vto, xml="<simulado/>")

    def parametros(self, metodo, extra=""):
        return [{"simulado": "sí"}]

    def _validar(self, s: dict) -> list[tuple[str, str]]:
        e = []
        if s["numero"] != self.ultimo_autorizado(s["pto_vta"], s["tipo"]) + 1:
            e.append(("10016", "El numero o fecha del comprobante no se corresponde con el proximo a autorizar."))
        D = Decimal
        suma = D(s["imp_tot_conc"]) + D(s["imp_neto"]) + D(s["imp_op_ex"]) + D(s["imp_trib"]) + D(s["imp_iva"])
        if suma != D(s["imp_total"]):
            e.append(("10048", "El importe total no coincide con la suma de sus componentes."))
        if s.get("iva"):
            if sum(D(i["importe"]) for i in s["iva"]) != D(s["imp_iva"]):
                e.append(("10023", "La suma de los importes de IVA no coincide con ImpIVA."))
            if sum(D(i["base"]) for i in s["iva"]) != D(s["imp_neto"]):
                e.append(("10018", "La suma de las bases imponibles no coincide con ImpNeto."))
        if s["tipo"] in (1, 2, 3, 51, 52, 53) and s["doc_tipo"] != 80:
            e.append(("10013", "Para comprobantes clase A el receptor debe identificarse con CUIT."))
        if not s.get("cond_iva_receptor"):
            e.append(("10246", "Falta CondicionIVAReceptorId."))
        return e
