"""Utilidades de prueba. TODAS estas pruebas son SIMULADAS (sin conexión a ARCA)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from barcofact import config, db
from barcofact.arca.simulador import SimuladorWsfe
from barcofact.emision import Facturador


def cuit_prueba(base10: str) -> str:
    """Arma un CUIT con dígito verificador correcto a partir de 10 dígitos (sólo para pruebas)."""
    pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    for _ in range(100):
        s = sum(int(d) * p for d, p in zip(base10, pesos))
        dv = 11 - s % 11
        if dv == 11:
            return base10 + "0"
        if dv != 10:
            return base10 + str(dv)
        base10 = base10[:-1] + str((int(base10[-1]) + 1) % 10)
    raise AssertionError


CUIT_EMISOR = cuit_prueba("3099999999")   # ficticio, sólo pruebas
CUIT_CLIENTE = cuit_prueba("2088888888")  # ficticio, sólo pruebas

CONFIG = """
entorno = "homologacion"
[emisor]
razon_social = "EMISOR DE PRUEBA"
cuit = "{cuit}"
condicion_iva = "responsable_inscripto"
domicilio_comercial = "Calle Falsa 123"
ingresos_brutos = "901-000000-0"
inicio_actividades = "01/01/2020"
[homologacion]
punto_venta = 5
[produccion]
habilitada = {prod_hab}
punto_venta = 7
[automatico]
habilitado = {auto}
canales = ["csv"]
importe_maximo = "100000"
reglas_confirmadas_por = "{auto_conf}"
"""


class Base(unittest.TestCase):
    entorno = "homologacion"
    prod_hab = "false"
    auto = "false"
    auto_conf = ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="barcofact-test-"))
        (self.tmp / "config.toml").write_text(CONFIG.format(cuit=CUIT_EMISOR, prod_hab=self.prod_hab,
                                                            auto=self.auto, auto_conf=self.auto_conf))
        self.cfg = config.cargar(self.entorno, datos=self.tmp, simulado=(self.entorno == "homologacion"))
        self.con = db.conectar(self.cfg.db_path)
        self.sim = SimuladorWsfe(self.cfg.dir_entorno / "simulador.json")
        self.f = Facturador(self.cfg, self.con, ws=self.sim)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def venta(self, **kw) -> str:
        datos = {
            "canal": "manual", "pedido_externo": kw.pop("pedido", "P-1"), "fecha_venta": kw.pop("fecha", None),
            "estado_pago": "acreditado", "estado": "pendiente_datos",
            "cliente": {"nombre": "", "tipo_doc": None, "nro_doc": "", "condicion_iva": "Consumidor Final",
                        "domicilio": ""},
            "items": [{"descripcion": "Resina poliéster común 1 kg", "sku": "RES", "cantidad": "2",
                       "precio_unitario_final": "12100", "alicuota": "21", "alicuota_confirmada": True}],
        }
        from datetime import date
        datos["fecha_venta"] = datos["fecha_venta"] or date.today().isoformat()
        datos.update(kw)
        vid, _ = db.guardar_venta(self.con, datos, "prueba")
        self.f.revalidar(vid, "prueba")
        return vid
