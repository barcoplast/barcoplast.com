"""Tablas y reglas fiscales (WSFEv1 / ARCA).

Los códigos siguen las tablas de parámetros de WSFEv1 (FEParamGetTiposCbte,
FEParamGetTiposDoc, FEParamGetTiposIva, FEParamGetCondicionIvaReceptor).
Antes de producción se contrastan contra ARCA con `barcofact verificar-parametros`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Tipos de comprobante
FACTURA_A, NOTA_DEBITO_A, NOTA_CREDITO_A = 1, 2, 3
FACTURA_B, NOTA_DEBITO_B, NOTA_CREDITO_B = 6, 7, 8
FACTURA_M, NOTA_DEBITO_M, NOTA_CREDITO_M = 51, 52, 53

TIPOS_CBTE = {
    1: ("A", "FACTURA"), 2: ("A", "NOTA DE DÉBITO"), 3: ("A", "NOTA DE CRÉDITO"),
    6: ("B", "FACTURA"), 7: ("B", "NOTA DE DÉBITO"), 8: ("B", "NOTA DE CRÉDITO"),
    51: ("M", "FACTURA"), 52: ("M", "NOTA DE DÉBITO"), 53: ("M", "NOTA DE CRÉDITO"),
}

NOTA_CREDITO_DE = {FACTURA_A: NOTA_CREDITO_A, FACTURA_B: NOTA_CREDITO_B, FACTURA_M: NOTA_CREDITO_M}
NOTA_DEBITO_DE = {FACTURA_A: NOTA_DEBITO_A, FACTURA_B: NOTA_DEBITO_B, FACTURA_M: NOTA_DEBITO_M}

# Tipos de documento
DOC_CUIT, DOC_CUIL, DOC_CDI, DOC_PASAPORTE, DOC_DNI, DOC_SIN_IDENTIFICAR = 80, 86, 87, 94, 96, 99
TIPOS_DOC = {80: "CUIT", 86: "CUIL", 87: "CDI", 94: "Pasaporte", 96: "DNI", 99: "Sin identificar"}

# Alícuotas de IVA: porcentaje -> Id
ALICUOTAS_IVA = {
    Decimal("0"): 3, Decimal("2.5"): 9, Decimal("5"): 8,
    Decimal("10.5"): 4, Decimal("21"): 5, Decimal("27"): 6,
}

# Conceptos
CONCEPTO_PRODUCTOS, CONCEPTO_SERVICIOS, CONCEPTO_PRODUCTOS_Y_SERVICIOS = 1, 2, 3

# Condición frente al IVA del receptor (campo CondicionIVAReceptorId, RG 5616)
COND_RI = 1
COND_EXENTO = 4
COND_CONSUMIDOR_FINAL = 5
COND_MONOTRIBUTO = 6
COND_NO_CATEGORIZADO = 7
COND_PROVEEDOR_EXTERIOR = 8
COND_CLIENTE_EXTERIOR = 9
COND_LIBERADO_19640 = 10
COND_MONOTRIBUTO_SOCIAL = 13
COND_NO_ALCANZADO = 15
COND_MONOTRIBUTO_PROMOVIDO = 16

CONDICIONES_IVA = {
    1: "IVA Responsable Inscripto",
    4: "IVA Sujeto Exento",
    5: "Consumidor Final",
    6: "Responsable Monotributo",
    7: "Sujeto No Categorizado",
    8: "Proveedor del Exterior",
    9: "Cliente del Exterior",
    10: "IVA Liberado – Ley N° 19.640",
    13: "Monotributista Social",
    15: "IVA No Alcanzado",
    16: "Monotributo Trabajador Independiente Promovido",
}

# Receptores a los que un Responsable Inscripto emite clase A (o M).
# Monotributistas reciben clase A desde la RG 5003/2021. CONFIRMAR CON EL CONTADOR.
CONDICIONES_CLASE_A = {COND_RI, COND_MONOTRIBUTO, COND_MONOTRIBUTO_SOCIAL, COND_MONOTRIBUTO_PROMOVIDO}
CONDICIONES_CLASE_B = {COND_EXENTO, COND_CONSUMIDOR_FINAL, COND_NO_CATEGORIZADO,
                       COND_LIBERADO_19640, COND_NO_ALCANZADO}
# Clientes del exterior -> Factura E (WSFEXv1), fuera del alcance de esta versión.
CONDICIONES_FUERA_DE_ALCANCE = {COND_PROVEEDOR_EXTERIOR, COND_CLIENTE_EXTERIOR}

# Equivalencias de texto habituales (Mercado Libre, planillas)
ALIAS_CONDICION = {
    "responsable inscripto": COND_RI, "iva responsable inscripto": COND_RI, "ri": COND_RI,
    "monotributo": COND_MONOTRIBUTO, "responsable monotributo": COND_MONOTRIBUTO,
    "monotributista": COND_MONOTRIBUTO,
    "consumidor final": COND_CONSUMIDOR_FINAL, "cf": COND_CONSUMIDOR_FINAL,
    "exento": COND_EXENTO, "iva sujeto exento": COND_EXENTO, "iva exento": COND_EXENTO,
    "no alcanzado": COND_NO_ALCANZADO, "iva no alcanzado": COND_NO_ALCANZADO,
    "monotributista social": COND_MONOTRIBUTO_SOCIAL,
}


def condicion_desde_texto(texto: str | int | None) -> int | None:
    if texto in (None, ""):
        return None
    if isinstance(texto, int) or str(texto).isdigit():
        n = int(texto)
        return n if n in CONDICIONES_IVA else None
    return ALIAS_CONDICION.get(str(texto).strip().lower())


def cuit_valido(cuit: str) -> bool:
    """Valida el dígito verificador de CUIT/CUIL (módulo 11)."""
    c = "".join(ch for ch in str(cuit) if ch.isdigit())
    if len(c) != 11:
        return False
    pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    s = sum(int(d) * p for d, p in zip(c[:10], pesos))
    dv = 11 - (s % 11)
    if dv == 11:
        dv = 0
    elif dv == 10:
        return False
    return dv == int(c[10])


def dni_valido(dni: str) -> bool:
    d = "".join(ch for ch in str(dni) if ch.isdigit())
    return 6 <= len(d) <= 8 and int(d) > 0


@dataclass
class TipoDeterminado:
    tipo: int | None
    motivo: str
    error: str | None = None


def determinar_tipo(condicion_emisor: str, emite_clase_m: bool, condicion_receptor: int | None) -> TipoDeterminado:
    """Factura según emisor y receptor. Nunca asume A o B por defecto."""
    if condicion_emisor != "responsable_inscripto":
        return TipoDeterminado(None, "", "Esta versión sólo factura como Responsable Inscripto. "
                               "Confirmá la condición del emisor en la configuración.")
    if condicion_receptor is None:
        return TipoDeterminado(None, "", "Falta la condición frente al IVA del receptor.")
    if condicion_receptor in CONDICIONES_FUERA_DE_ALCANCE:
        return TipoDeterminado(None, "", "Receptor del exterior: corresponde Factura E (WSFEX), "
                               "no implementada todavía. Emitir manualmente y registrar.")
    if condicion_receptor in CONDICIONES_CLASE_A:
        if emite_clase_m:
            return TipoDeterminado(FACTURA_M, "Emisor encuadrado en clase M; receptor "
                                   f"{CONDICIONES_IVA[condicion_receptor]}")
        return TipoDeterminado(FACTURA_A, f"Emisor RI y receptor {CONDICIONES_IVA[condicion_receptor]}")
    if condicion_receptor in CONDICIONES_CLASE_B:
        return TipoDeterminado(FACTURA_B, f"Emisor RI y receptor {CONDICIONES_IVA[condicion_receptor]}")
    return TipoDeterminado(None, "", f"Condición de receptor {condicion_receptor} no contemplada.")


def letra(tipo: int) -> str:
    return TIPOS_CBTE[tipo][0]


def nombre_tipo(tipo: int) -> str:
    l, n = TIPOS_CBTE[tipo]
    return f"{n} {l}"


def es_clase_a_o_m(tipo: int) -> bool:
    return letra(tipo) in ("A", "M")
