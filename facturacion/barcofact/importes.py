"""Cálculo de importes con aritmética decimal.

Regla central: los precios de venta al público INCLUYEN IVA. El total que paga el
comprador se respeta al centavo y se descompone en neto + IVA por alícuota:

    neto_alicuota = redondear(total_con_iva_alicuota / (1 + alícuota))
    iva_alicuota  = total_con_iva_alicuota - neto_alicuota

El IVA nunca se vuelve a sumar sobre un precio que ya lo contiene.
Redondeo: ROUND_HALF_UP a 2 decimales, siempre en el mismo orden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from . import fiscal

CENT = Decimal("0.01")
EXENTO = "exento"
NO_GRAVADO = "no_gravado"


class ErrorImportes(ValueError):
    pass


def r2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        raise ErrorImportes("No se aceptan float para importes; usar texto o Decimal.")
    s = str(x).strip().replace("$", "").replace(" ", "")
    if s.count(",") == 1 and s.count(".") >= 1 and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")  # 1.234,56
    elif s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")  # 1234,56
    try:
        return Decimal(s)
    except Exception as e:  # noqa: BLE001
        raise ErrorImportes(f"Importe inválido: {x!r}") from e


@dataclass
class LineaEntrada:
    descripcion: str
    cantidad: Decimal
    precio_unitario_final: Decimal  # con IVA incluido
    alicuota: Decimal | str  # porcentaje, o "exento"/"no_gravado"
    descuento_final: Decimal = Decimal("0")  # importe con IVA incluido
    sku: str = ""
    es_envio: bool = False


@dataclass
class LineaCalculada:
    descripcion: str
    sku: str
    cantidad: Decimal
    alicuota: Decimal | str
    precio_unitario_final: Decimal
    descuento_final: Decimal
    total_final: Decimal
    neto: Decimal = Decimal("0")
    iva: Decimal = Decimal("0")
    es_envio: bool = False

    @property
    def precio_unitario_neto(self) -> Decimal:
        if self.cantidad == 0:
            return Decimal("0")
        return r2((self.neto + self.descuento_neto) / self.cantidad)

    @property
    def descuento_neto(self) -> Decimal:
        if self.descuento_final == 0 or not isinstance(self.alicuota, Decimal):
            return self.descuento_final
        return r2(self.descuento_final / (1 + self.alicuota / 100))


@dataclass
class GrupoIva:
    alicuota: Decimal
    id_arca: int
    base: Decimal
    importe: Decimal


@dataclass
class Calculo:
    lineas: list[LineaCalculada]
    grupos_iva: list[GrupoIva]
    imp_neto: Decimal
    imp_iva: Decimal
    imp_op_ex: Decimal
    imp_tot_conc: Decimal
    imp_trib: Decimal
    imp_total: Decimal
    descuento_general_final: Decimal = Decimal("0")
    avisos: list[str] = field(default_factory=list)

    def a_dict(self) -> dict:
        return {
            "imp_neto": str(self.imp_neto), "imp_iva": str(self.imp_iva),
            "imp_op_ex": str(self.imp_op_ex), "imp_tot_conc": str(self.imp_tot_conc),
            "imp_trib": str(self.imp_trib), "imp_total": str(self.imp_total),
            "grupos_iva": [{"alicuota": str(g.alicuota), "id": g.id_arca, "base": str(g.base),
                            "importe": str(g.importe)} for g in self.grupos_iva],
        }


def _repartir(total: Decimal, pesos: list[Decimal]) -> list[Decimal]:
    """Reparte `total` proporcionalmente; el residuo de redondeo va al peso mayor."""
    suma = sum(pesos)
    if suma == 0:
        return [Decimal("0")] * len(pesos)
    partes = [r2(total * p / suma) for p in pesos]
    diferencia = total - sum(partes)
    if diferencia:
        i = max(range(len(pesos)), key=lambda k: pesos[k])
        partes[i] += diferencia
    return partes


def calcular(lineas: list[LineaEntrada], descuento_general_final: Decimal = Decimal("0")) -> Calculo:
    if not lineas:
        raise ErrorImportes("La venta no tiene ítems.")
    calc: list[LineaCalculada] = []
    for ln in lineas:
        if ln.cantidad <= 0:
            raise ErrorImportes(f"Cantidad inválida en «{ln.descripcion}».")
        if ln.precio_unitario_final < 0 or ln.descuento_final < 0:
            raise ErrorImportes(f"Importes negativos en «{ln.descripcion}».")
        if isinstance(ln.alicuota, Decimal) and ln.alicuota not in fiscal.ALICUOTAS_IVA:
            raise ErrorImportes(f"Alícuota {ln.alicuota}% no admitida en «{ln.descripcion}».")
        bruto = r2(ln.cantidad * ln.precio_unitario_final)
        total = bruto - r2(ln.descuento_final)
        if total < 0:
            raise ErrorImportes(f"El descuento supera el importe en «{ln.descripcion}».")
        calc.append(LineaCalculada(ln.descripcion, ln.sku, ln.cantidad, ln.alicuota,
                                   ln.precio_unitario_final, r2(ln.descuento_final), total,
                                   es_envio=ln.es_envio))

    # Descuento general (con IVA incluido) prorrateado entre ítems no envío.
    descuento_general_final = r2(descuento_general_final)
    if descuento_general_final:
        if descuento_general_final < 0:
            raise ErrorImportes("Descuento general negativo.")
        idx = [i for i, l in enumerate(calc) if not l.es_envio]
        partes = _repartir(descuento_general_final, [calc[i].total_final for i in idx])
        for i, p in zip(idx, partes):
            if p > calc[i].total_final:
                raise ErrorImportes("El descuento general supera el importe de los ítems.")
            calc[i].descuento_final += p
            calc[i].total_final -= p

    # Descomposición por alícuota preservando el total cobrado.
    grupos: list[GrupoIva] = []
    for alic in sorted({l.alicuota for l in calc if isinstance(l.alicuota, Decimal)}):
        del_grupo = [l for l in calc if l.alicuota == alic]
        total_final = sum(l.total_final for l in del_grupo)
        base = r2(total_final / (1 + alic / 100))
        iva = total_final - base
        # neto por línea, ajustando el residuo para que la suma coincida con la base
        netos = _repartir(base, [l.total_final for l in del_grupo])
        for l, n in zip(del_grupo, netos):
            l.neto = n
            l.iva = l.total_final - n
        grupos.append(GrupoIva(alic, fiscal.ALICUOTAS_IVA[alic], base, iva))

    for l in calc:
        if not isinstance(l.alicuota, Decimal):
            if l.alicuota not in (EXENTO, NO_GRAVADO):
                raise ErrorImportes(f"Tratamiento de IVA desconocido: {l.alicuota}")
            l.neto, l.iva = l.total_final, Decimal("0")

    imp_neto = sum((g.base for g in grupos), Decimal("0"))
    imp_iva = sum((g.importe for g in grupos), Decimal("0"))
    imp_op_ex = sum((l.total_final for l in calc if l.alicuota == EXENTO), Decimal("0"))
    imp_tot_conc = sum((l.total_final for l in calc if l.alicuota == NO_GRAVADO), Decimal("0"))
    imp_total = imp_neto + imp_iva + imp_op_ex + imp_tot_conc
    assert imp_total == sum(l.total_final for l in calc), "descuadre interno"
    resultado = Calculo(calc, grupos, imp_neto, imp_iva, imp_op_ex, imp_tot_conc, Decimal("0"),
                        imp_total, descuento_general_final)
    if imp_total == 0:
        resultado.avisos.append("El total es cero.")
    return resultado
