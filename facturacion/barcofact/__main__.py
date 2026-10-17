"""Línea de comandos: python -m barcofact <comando>."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from . import backup, config, db, fiscal, usuarios

PLANTILLA = """# Configuración de BarcoFact. Este archivo NO va al repositorio.
entorno = "homologacion"   # "homologacion" o "produccion"

[emisor]
# Completar con los datos EXACTOS de la constancia de inscripción del emisor actual.
razon_social = ""
cuit = ""                   # sin guiones
condicion_iva = "responsable_inscripto"
domicilio_comercial = ""
ingresos_brutos = ""        # n° de IIBB / Convenio Multilateral
inicio_actividades = ""     # DD/MM/AAAA
emite_clase_m = false       # true sólo si ARCA encuadró al emisor para emitir clase M

[homologacion]
punto_venta = 0             # PV de homologación (en homologación cualquier PV habilitado sirve)
certificado = "{datos}/certs/homologacion.crt"
clave_privada = "{datos}/certs/homologacion.key"

[produccion]
habilitada = false          # se activa SOLO tras aprobar el primer comprobante real
punto_venta = 0             # PV "RECE para aplicativo y web services", exclusivo de este sistema
certificado = "{datos}/certs/produccion.crt"
clave_privada = "{datos}/certs/produccion.key"

[reglas]
umbral_identificacion_consumidor_final = "10000000"   # RG 5700/2025 y RG 5866/2026: confirmar con el contador
alicuota_envio = "21"

[automatico]
habilitado = false
canales = []                # ej. ["mercadolibre"]
importe_maximo = "0"
requiere_pago_acreditado = true
reglas_confirmadas_por = "" # nombre y fecha de quien confirmó las reglas

[envio_clientes]
habilitado = false          # no se envían comprobantes a clientes hasta habilitarlo expresamente
"""


def cmd_init(a):
    d = config.dir_datos()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    (d / "certs").mkdir(exist_ok=True)
    os.chmod(d / "certs", 0o700)
    cfg_path = d / "config.toml"
    if not cfg_path.exists():
        cfg_path.write_text(PLANTILLA.format(datos=d))
        os.chmod(cfg_path, 0o600)
        print(f"Creado {cfg_path}")
    else:
        print(f"Ya existe {cfg_path} (no se modificó)")
    cat = d / "catalogo.csv"
    if not cat.exists():
        cat.write_text((Path(__file__).parents[1] / "catalogo.ejemplo.csv").read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Creado {cat} (alícuotas SIN confirmar: revisarlas con el contador)")
    print(f"Directorio de datos: {d}")


def cmd_generar_csr(a):
    """Genera clave privada (local, nunca sale del equipo) y CSR para subir a ARCA."""
    cfg = config.cargar(a.entorno)
    if not cfg.emisor.cuit or not fiscal.cuit_valido(cfg.emisor.cuit):
        sys.exit("Configurá primero un CUIT de emisor válido en config.toml.")
    certs = cfg.datos / "certs"
    certs.mkdir(exist_ok=True)
    key = certs / f"{a.entorno}.key"
    csr = certs / f"{a.entorno}.csr"
    if key.exists():
        sys.exit(f"Ya existe {key}. No se sobrescribe una clave privada.")
    old = os.umask(0o077)
    try:
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True)
        subj = f"/C=AR/O={a.organizacion}/CN={a.alias}/serialNumber=CUIT {cfg.emisor.cuit}"
        subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", subj, "-out", str(csr)], check=True)
    finally:
        os.umask(old)
    os.chmod(key, 0o600)
    print(f"Clave privada: {key}  (NO compartirla, NO subirla a ningún lado)")
    print(f"CSR para ARCA: {csr}")


def cmd_crear_usuario(a):
    cfg = config.cargar(a.entorno)
    con = db.conectar(cfg.db_path)
    clave = getpass.getpass("Clave nueva (mín. 10 caracteres): ")
    if clave != getpass.getpass("Repetir clave: "):
        sys.exit("Las claves no coinciden.")
    usuarios.crear(con, a.usuario, clave)
    print(f"Usuario {a.usuario} creado en {cfg.entorno}.")


def cmd_web(a):
    from .web import servir
    cfg = config.cargar(a.entorno, simulado=a.simulado)
    servir(cfg, a.puerto)


def cmd_verificar(a):
    """Chequeo de configuración y conexión con ARCA."""
    cfg = config.cargar(a.entorno, simulado=a.simulado)
    print(f"Entorno: {cfg.entorno}{' (SIMULADOR)' if cfg.simulado else ''}")
    print(f"Emisor: {cfg.emisor.razon_social or '—'} CUIT {cfg.emisor.cuit or '—'}")
    falt = cfg.emisor.faltantes()
    print("Datos del emisor completos" if not falt else f"FALTAN datos del emisor: {', '.join(falt)}")
    if cfg.emisor.cuit and not fiscal.cuit_valido(cfg.emisor.cuit):
        print("CUIT del emisor INVÁLIDO")
    from .emision import crear_cliente_ws
    ws = crear_cliente_ws(cfg)
    print("FEDummy:", ws.dummy())
    if cfg.arca.punto_venta:
        for t in (fiscal.FACTURA_A, fiscal.FACTURA_B, fiscal.NOTA_CREDITO_A, fiscal.NOTA_CREDITO_B):
            print(f"Último {fiscal.nombre_tipo(t)} PV {cfg.arca.punto_venta}: {ws.ultimo_autorizado(cfg.arca.punto_venta, t)}")
    if a.parametros and not cfg.simulado:
        for m in ("FEParamGetPtosVenta", "FEParamGetTiposCbte", "FEParamGetTiposIva", "FEParamGetTiposDoc"):
            print(m, json.dumps(ws.parametros(m), ensure_ascii=False))
        for clase in ("A", "B"):
            print(f"FEParamGetCondicionIvaReceptor {clase}",
                  json.dumps(ws.parametros("FEParamGetCondicionIvaReceptor", f"<ar:ClaseCmp>{clase}</ar:ClaseCmp>"),
                             ensure_ascii=False))


def cmd_aceptar_externa(a):
    from .emision import Facturador
    cfg = config.cargar(a.entorno, simulado=a.simulado)
    f = Facturador(cfg)
    n = f.aceptar_numeracion_externa(a.pto_vta, a.tipo, a.usuario)
    print(f"Aceptado: los comprobantes hasta el N° {n} fueron emitidos por otra herramienta.")


def cmd_conciliar(a):
    from .emision import Facturador
    cfg = config.cargar(a.entorno, simulado=a.simulado)
    f = Facturador(cfg)
    for c in f.con.execute("SELECT id FROM comprobantes WHERE estado IN ('enviando','incierto')").fetchall():
        r = f.conciliar(c["id"], a.usuario)
        print(f"Comprobante {c['id']}: {r['estado']}")


def cmd_automatico(a):
    from .automatico import procesar
    from .emision import Facturador
    cfg = config.cargar(a.entorno)
    print(procesar(Facturador(cfg)))


def cmd_backup(a):
    cfg = config.cargar(a.entorno)
    p = backup.crear(cfg, Path(a.copiar_a) if a.copiar_a else None)
    print(f"Copia creada: {p}  {backup.verificar(p)}")


def cmd_restaurar(a):
    cfg = config.cargar(a.entorno)
    backup.restaurar(cfg, Path(a.archivo), a.forzar)
    print("Restaurado. Ejecutá `conciliar` para verificar contra ARCA los comprobantes pendientes.")


def cmd_ml_configurar(a):
    from .canales.mercadolibre import ClienteML
    d = config.dir_datos()
    client_id = input("Client ID de tu aplicación de Mercado Libre: ").strip()
    secret = getpass.getpass("Client secret (no se muestra): ").strip()
    redirect = input("Redirect URI registrada en la aplicación: ").strip()
    print("\nAbrí este enlace, iniciá sesión en Mercado Libre y autorizá la aplicación:\n")
    print(ClienteML.url_autorizacion(client_id, redirect))
    code = getpass.getpass("\nPegá el parámetro «code» de la URL de retorno (no se muestra): ").strip()
    ClienteML.canjear_codigo(d / "mercadolibre.json", client_id, secret, redirect, code)
    print("Mercado Libre vinculado. Credenciales guardadas con permisos 600.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="barcofact", description="Facturación electrónica BARCOPLAST (ARCA WSFEv1)")
    p.add_argument("--entorno", choices=config.ENTORNOS, default=None)
    p.add_argument("--simulado", action="store_true", help="usar el simulador local (sólo homologación)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(f=cmd_init)
    s = sub.add_parser("generar-csr")
    s.add_argument("--alias", default="barcofact")
    s.add_argument("--organizacion", required=True, help="razón social del emisor")
    s.set_defaults(f=cmd_generar_csr)
    s = sub.add_parser("crear-usuario")
    s.add_argument("usuario")
    s.set_defaults(f=cmd_crear_usuario)
    s = sub.add_parser("web")
    s.add_argument("--puerto", type=int, default=8765)
    s.set_defaults(f=cmd_web)
    s = sub.add_parser("verificar")
    s.add_argument("--parametros", action="store_true")
    s.set_defaults(f=cmd_verificar)
    s = sub.add_parser("aceptar-numeracion-externa")
    s.add_argument("--pto-vta", type=int, required=True)
    s.add_argument("--tipo", type=int, required=True)
    s.add_argument("--usuario", required=True)
    s.set_defaults(f=cmd_aceptar_externa)
    s = sub.add_parser("conciliar")
    s.add_argument("--usuario", required=True)
    s.set_defaults(f=cmd_conciliar)
    sub.add_parser("automatico").set_defaults(f=cmd_automatico)
    s = sub.add_parser("backup")
    s.add_argument("--copiar-a")
    s.set_defaults(f=cmd_backup)
    s = sub.add_parser("restaurar")
    s.add_argument("archivo")
    s.add_argument("--forzar", action="store_true")
    s.set_defaults(f=cmd_restaurar)
    sub.add_parser("ml-configurar").set_defaults(f=cmd_ml_configurar)
    a = p.parse_args(argv)
    try:
        a.f(a)
    except config.ErrorConfiguracion as e:
        sys.exit(f"Configuración: {e}")


if __name__ == "__main__":
    main()
