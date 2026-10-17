import random
import unittest
from decimal import Decimal as D

from barcofact import fiscal, importes
from barcofact.importes import LineaEntrada as L


class TestImportes(unittest.TestCase):
    def test_iva_incluido_no_se_suma(self):
        c = importes.calcular([L("Resina", D(1), D("12100"), D("21"))])
        self.assertEqual((c.imp_neto, c.imp_iva, c.imp_total), (D("10000.00"), D("2100.00"), D("12100.00")))

    def test_alicuotas_mixtas(self):
        c = importes.calcular([L("A", D(1), D("1210"), D("21")), L("B", D(2), D("552.50"), D("10.5"))])
        self.assertEqual(c.imp_total, D("2315.00"))
        g = {x.alicuota: x for x in c.grupos_iva}
        self.assertEqual((g[D("21")].base, g[D("21")].importe), (D("1000.00"), D("210.00")))
        self.assertEqual((g[D("10.5")].base, g[D("10.5")].importe), (D("1000.00"), D("105.00")))
        self.assertEqual(c.imp_neto + c.imp_iva, c.imp_total)

    def test_redondeo(self):
        c = importes.calcular([L("x", D(3), D("33.33"), D("21"))])
        self.assertEqual(c.imp_total, D("99.99"))
        self.assertEqual(c.imp_neto, D("82.64"))
        self.assertEqual(c.imp_iva, D("17.35"))

    def test_descuentos_y_envio(self):
        c = importes.calcular([L("A", D(2), D("1000"), D("21"), descuento_final=D("100")),
                               L("Envío", D(1), D("2500"), D("21"), es_envio=True)],
                              descuento_general_final=D("190"))
        self.assertEqual(c.imp_total, D("4210.00"))  # 2000-100-190 + 2500
        self.assertEqual(c.lineas[1].total_final, D("2500"))  # el descuento general no toca el envío

    def test_exento(self):
        c = importes.calcular([L("x", D(1), D("500"), importes.EXENTO)])
        self.assertEqual((c.imp_op_ex, c.imp_neto, c.imp_iva, c.imp_total), (D("500"), 0, 0, D("500")))

    def test_errores(self):
        with self.assertRaises(importes.ErrorImportes):
            importes.calcular([L("x", D(0), D("1"), D("21"))])
        with self.assertRaises(importes.ErrorImportes):
            importes.calcular([L("x", D(1), D("1"), D("19"))])
        with self.assertRaises(importes.ErrorImportes):
            importes.calcular([L("x", D(1), D("10"), D("21"), descuento_final=D("11"))])
        with self.assertRaises(importes.ErrorImportes):
            importes.dec(1.1)

    def test_formatos_de_numero(self):
        self.assertEqual(importes.dec("1.234,56"), D("1234.56"))
        self.assertEqual(importes.dec("1234,5"), D("1234.5"))
        self.assertEqual(importes.dec("$ 1234.50"), D("1234.50"))

    def test_propiedades_aleatorias(self):
        rnd = random.Random(42)
        alics = [D("21"), D("10.5"), D("27"), D("0")]
        for _ in range(3000):
            ls = [L(f"i{k}", D(rnd.randint(1, 7)), D(rnd.randint(1, 2_000_000)) / 100, rnd.choice(alics))
                  for k in range(rnd.randint(1, 5))]
            bruto = sum(importes.r2(l.cantidad * l.precio_unitario_final) for l in ls)
            desc = D(rnd.randint(0, int(bruto * 10))) / 100
            c = importes.calcular(ls, desc)
            self.assertEqual(c.imp_total, bruto - importes.r2(desc))
            self.assertEqual(c.imp_neto + c.imp_iva, c.imp_total)
            self.assertEqual(sum(l.neto for l in c.lineas), c.imp_neto)
            for g in c.grupos_iva:
                self.assertLessEqual(abs(g.importe - g.base * g.alicuota / 100), D("0.01"))


class TestFiscal(unittest.TestCase):
    def test_tipo_segun_receptor(self):
        T = lambda c, m=False: fiscal.determinar_tipo("responsable_inscripto", m, c).tipo  # noqa: E731
        self.assertEqual(T(fiscal.COND_RI), fiscal.FACTURA_A)
        self.assertEqual(T(fiscal.COND_MONOTRIBUTO), fiscal.FACTURA_A)
        self.assertEqual(T(fiscal.COND_CONSUMIDOR_FINAL), fiscal.FACTURA_B)
        self.assertEqual(T(fiscal.COND_EXENTO), fiscal.FACTURA_B)
        self.assertEqual(T(fiscal.COND_RI, True), fiscal.FACTURA_M)
        self.assertIsNone(T(None))
        self.assertIsNone(T(fiscal.COND_CLIENTE_EXTERIOR))
        self.assertIsNone(fiscal.determinar_tipo("monotributo", False, fiscal.COND_RI).tipo)

    def test_cuit(self):
        from tests.base import CUIT_CLIENTE
        self.assertTrue(fiscal.cuit_valido(CUIT_CLIENTE))
        self.assertFalse(fiscal.cuit_valido(CUIT_CLIENTE[:-1] + str((int(CUIT_CLIENTE[-1]) + 1) % 10)))
        self.assertFalse(fiscal.cuit_valido("123"))
