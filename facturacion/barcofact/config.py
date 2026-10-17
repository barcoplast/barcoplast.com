"""Carga de configuración local.

La configuración y los secretos viven FUERA del repositorio, en el directorio de
datos (por defecto ~/.barcofact, o la ruta de la variable BARCOFACT_DATOS).
Homologación y producción usan bases de datos, certificados, tickets de acceso y
carpetas de comprobantes completamente separados.
"""
from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ENTORNOS = ("homologacion", "produccion")

# Endpoints oficiales de ARCA (ex AFIP). Los dominios técnicos siguen siendo afip.gov.ar.
ENDPOINTS = {
    "homologacion": {
        "wsaa": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
        "wsfe": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
    },
    "produccion": {
        "wsaa": "https://wsaa.afip.gov.ar/ws/services/LoginCms",
        "wsfe": "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
    },
}

URL_QR = "https://www.afip.gob.ar/fe/qr/"

REPO_RAIZ = Path(__file__).resolve().parents[2]


class ErrorConfiguracion(Exception):
    pass


@dataclass
class Emisor:
    razon_social: str = ""
    cuit: str = ""
    condicion_iva: str = ""  # "responsable_inscripto"
    domicilio_comercial: str = ""
    ingresos_brutos: str = ""
    inicio_actividades: str = ""
    emite_clase_m: bool = False

    def faltantes(self) -> list[str]:
        faltan = []
        for campo in ("razon_social", "cuit", "condicion_iva", "domicilio_comercial",
                      "ingresos_brutos", "inicio_actividades"):
            if not str(getattr(self, campo)).strip():
                faltan.append(campo)
        return faltan


@dataclass
class EntornoArca:
    nombre: str
    punto_venta: int = 0
    certificado: str = ""
    clave_privada: str = ""
    habilitada: bool = False  # sólo se usa en producción
    wsaa_url: str = ""
    wsfe_url: str = ""


@dataclass
class Reglas:
    umbral_identificacion_cf: Decimal = Decimal("10000000")
    alicuota_envio: Decimal = Decimal("21")
    moneda: str = "PES"


@dataclass
class Automatico:
    habilitado: bool = False
    canales: list[str] = field(default_factory=list)
    importe_maximo: Decimal = Decimal("0")
    requiere_pago_acreditado: bool = True
    reglas_confirmadas_por: str = ""


@dataclass
class Config:
    datos: Path
    entorno: str
    emisor: Emisor
    arca: EntornoArca
    reglas: Reglas
    automatico: Automatico
    envio_clientes_habilitado: bool
    mercadolibre: dict
    simulado: bool = False

    @property
    def dir_entorno(self) -> Path:
        return self.datos / self.entorno

    @property
    def db_path(self) -> Path:
        return self.dir_entorno / "barcofact.sqlite"

    @property
    def dir_comprobantes(self) -> Path:
        return self.dir_entorno / "comprobantes"

    @property
    def dir_ta(self) -> Path:
        return self.dir_entorno / "ta"

    @property
    def dir_backups(self) -> Path:
        return self.datos / "backups" / self.entorno

    @property
    def es_produccion(self) -> bool:
        return self.entorno == "produccion"

    def preparar_directorios(self) -> None:
        self.datos.mkdir(parents=True, exist_ok=True)
        os.chmod(self.datos, 0o700)
        for d in (self.dir_entorno, self.dir_comprobantes, self.dir_ta, self.datos / "backups", self.dir_backups):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)


def dir_datos() -> Path:
    return Path(os.environ.get("BARCOFACT_DATOS", Path.home() / ".barcofact")).expanduser()


def _dec(v, defecto: str) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal(defecto)


def _dentro_del_repo(ruta: Path) -> bool:
    try:
        ruta.resolve().relative_to(REPO_RAIZ)
        return True
    except ValueError:
        return False


def verificar_secreto(ruta_txt: str, descripcion: str) -> Path:
    """Controla que un archivo sensible exista, esté fuera del repo y no sea legible por otros."""
    if not ruta_txt:
        raise ErrorConfiguracion(f"Falta configurar la ruta de {descripcion}.")
    ruta = Path(ruta_txt).expanduser()
    if _dentro_del_repo(ruta):
        raise ErrorConfiguracion(
            f"{descripcion} está dentro del repositorio ({ruta}). Movelo al directorio de datos.")
    if not ruta.is_file():
        raise ErrorConfiguracion(f"No existe {descripcion}: {ruta}")
    modo = ruta.stat().st_mode
    if modo & (stat.S_IRWXG | stat.S_IRWXO):
        raise ErrorConfiguracion(
            f"{descripcion} tiene permisos demasiado abiertos. Ejecutá: chmod 600 {ruta}")
    return ruta


def cargar(entorno: str | None = None, datos: Path | None = None, simulado: bool | None = None) -> Config:
    datos = (datos or dir_datos()).expanduser()
    if _dentro_del_repo(datos):
        raise ErrorConfiguracion("El directorio de datos no puede estar dentro del repositorio.")
    archivo = datos / "config.toml"
    crudo: dict = {}
    if archivo.exists():
        with open(archivo, "rb") as f:
            crudo = tomllib.load(f)

    entorno = entorno or os.environ.get("BARCOFACT_ENTORNO") or crudo.get("entorno", "homologacion")
    if entorno not in ENTORNOS:
        raise ErrorConfiguracion(f"Entorno desconocido: {entorno}")

    em = crudo.get("emisor", {})
    emisor = Emisor(
        razon_social=em.get("razon_social", ""),
        cuit=str(em.get("cuit", "")).replace("-", "").strip(),
        condicion_iva=em.get("condicion_iva", ""),
        domicilio_comercial=em.get("domicilio_comercial", ""),
        ingresos_brutos=str(em.get("ingresos_brutos", "")),
        inicio_actividades=str(em.get("inicio_actividades", "")),
        emite_clase_m=bool(em.get("emite_clase_m", False)),
    )

    ent = crudo.get(entorno, {})
    arca = EntornoArca(
        nombre=entorno,
        punto_venta=int(ent.get("punto_venta", 0) or 0),
        certificado=ent.get("certificado", ""),
        clave_privada=ent.get("clave_privada", ""),
        habilitada=bool(ent.get("habilitada", False)) if entorno == "produccion" else True,
        wsaa_url=ENDPOINTS[entorno]["wsaa"],
        wsfe_url=ENDPOINTS[entorno]["wsfe"],
    )

    rg = crudo.get("reglas", {})
    reglas = Reglas(
        umbral_identificacion_cf=_dec(rg.get("umbral_identificacion_consumidor_final"), "10000000"),
        alicuota_envio=_dec(rg.get("alicuota_envio"), "21"),
    )

    au = crudo.get("automatico", {})
    automatico = Automatico(
        habilitado=bool(au.get("habilitado", False)),
        canales=list(au.get("canales", [])),
        importe_maximo=_dec(au.get("importe_maximo"), "0"),
        requiere_pago_acreditado=bool(au.get("requiere_pago_acreditado", True)),
        reglas_confirmadas_por=au.get("reglas_confirmadas_por", ""),
    )

    if simulado is None:
        simulado = os.environ.get("BARCOFACT_SIMULADO") == "1"
    if simulado and entorno == "produccion":
        raise ErrorConfiguracion("El simulador sólo puede usarse en homologación.")

    cfg = Config(
        datos=datos,
        entorno=entorno,
        emisor=emisor,
        arca=arca,
        reglas=reglas,
        automatico=automatico,
        envio_clientes_habilitado=bool(crudo.get("envio_clientes", {}).get("habilitado", False)),
        mercadolibre=crudo.get("mercadolibre", {}),
        simulado=simulado,
    )
    cfg.preparar_directorios()
    return cfg
