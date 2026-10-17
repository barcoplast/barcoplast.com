"""Catálogo local de productos con su tratamiento de IVA.

Archivo: <datos>/catalogo.csv con columnas
    sku,nombre,alicuota,alicuota_confirmada,ml_item_id
`alicuota` en porcentaje (21, 10.5, ...) o "exento". `alicuota_confirmada` = si/no:
sólo el contador la pasa a «si».
"""
from __future__ import annotations

import csv
import difflib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Producto:
    sku: str
    nombre: str
    alicuota: str
    confirmada: bool
    ml_item_id: str = ""


class Catalogo:
    def __init__(self, productos: list[Producto]):
        self.productos = productos

    @classmethod
    def cargar(cls, ruta: Path) -> "Catalogo":
        if not ruta.exists():
            return cls([])
        with open(ruta, newline="", encoding="utf-8") as f:
            return cls([Producto(r["sku"].strip(), r["nombre"].strip(), r.get("alicuota", "").strip(),
                                 r.get("alicuota_confirmada", "").strip().lower() in ("si", "sí", "true", "1"),
                                 (r.get("ml_item_id") or "").strip())
                        for r in csv.DictReader(f) if r.get("sku")])

    def por_sku(self, sku: str) -> Producto | None:
        return next((p for p in self.productos if sku and p.sku.lower() == sku.lower()), None)

    def por_item_ml(self, item_id: str) -> Producto | None:
        return next((p for p in self.productos if item_id and p.ml_item_id == item_id), None)

    def parecido(self, texto: str) -> Producto | None:
        nombres = {p.nombre.lower(): p for p in self.productos}
        m = difflib.get_close_matches(texto.lower(), list(nombres), n=1, cutoff=0.55)
        return nombres[m[0]] if m else None

    def completar_item(self, item: dict, producto: Producto | None) -> dict:
        """Agrega alícuota del catálogo. Si no hay producto, la alícuota queda vacía (bloquea)."""
        if producto:
            item.setdefault("sku", producto.sku)
            if not item.get("alicuota"):
                item["alicuota"] = producto.alicuota
            item["alicuota_confirmada"] = producto.confirmada and item["alicuota"] == producto.alicuota
        else:
            item["alicuota_confirmada"] = False
        return item
