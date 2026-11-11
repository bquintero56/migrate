"""
Migra pipelines de un Jenkins (origen) a otro (destino) copiando su config.xml.

- Lista los jobs del Jenkins origen (incluye los que estan dentro de carpetas).
- Te deja elegir cuales copiar.
- Si el job ya existe en el destino, avisa y NO lo copia.
- Si no existe, lo crea en el destino con la misma configuracion
  (crea las carpetas padre si hacen falta).

Requisitos:  pip install requests

Uso:
    python migrar_jenkins.py                       # elegir de la lista
    python migrar_jenkins.py --jobs job1 carpeta/job2
    python migrar_jenkins.py --simular             # no crea nada, solo muestra
"""

import argparse
import getpass
import sys
from urllib.parse import quote

import requests

# ======================= CONFIGURACION =======================
ORIGEN = {
    "url": "http://jenkins-origen:8080",
    "usuario": "admin",
    "token": "",  # API token (si queda vacio se pide al ejecutar)
}
DESTINO = {
    "url": "http://jenkins-destino:8080",
    "usuario": "admin",
    "token": "",
}
VERIFICAR_SSL = True  # ponlo en False si usan certificados autofirmados

# Carpeta del Jenkins 2 donde se copiaran los jobs (ej: "equipo/backend").
# Vacio = en la raiz. Se puede sobreescribir con --carpeta. La carpeta debe existir en el destino.
CARPETA_DESTINO = ""

# Pipelines a migrar (nombre tal como aparece en el Jenkins 1).
# Si la lista queda vacia, el script muestra todos los jobs del origen para elegir.
PIPELINES = [
    # "pipeline-1",
    # "pipeline-2",
]
# =============================================================

FOLDER_CLASS = "com.cloudbees.hudson.plugins.folder.Folder"


class Jenkins:
    def __init__(self, nombre, url, usuario, token):
        self.nombre = nombre
        self.url = url.rstrip("/")
        if not token:
            token = getpass.getpass(f"API token de {usuario} en {nombre} ({url}): ")
        self.s = requests.Session()
        self.s.auth = (usuario, token)
        self.s.verify = VERIFICAR_SSL

    def conectar(self):
        r = self.s.get(f"{self.url}/api/json", params={"tree": "mode"}, timeout=30)
        if r.status_code in (401, 403):
            sys.exit(f"[ERROR] {self.nombre}: usuario o token invalidos (HTTP {r.status_code})")
        r.raise_for_status()
        # Crumb CSRF (si esta habilitado)
        c = self.s.get(f"{self.url}/crumbIssuer/api/json", timeout=30)
        if c.status_code == 200:
            d = c.json()
            self.s.headers[d["crumbRequestField"]] = d["crumb"]
        print(f"  [OK] Conectado a {self.nombre}: {self.url}")

    @staticmethod
    def ruta(nombre_completo):
        """'carpeta/sub/job' -> '/job/carpeta/job/sub/job/job'"""
        if not nombre_completo:
            return ""
        return "".join(f"/job/{quote(p, safe='')}" for p in nombre_completo.split("/"))

    def listar_jobs(self, carpeta=""):
        r = self.s.get(f"{self.url}{self.ruta(carpeta)}/api/json",
                       params={"tree": "jobs[name,_class]"}, timeout=60)
        r.raise_for_status()
        jobs = []
        for j in r.json().get("jobs", []):
            full = f"{carpeta}/{j['name']}" if carpeta else j["name"]
            if j.get("_class") == FOLDER_CLASS:
                jobs.extend(self.listar_jobs(full))
            else:
                jobs.append(full)
        return jobs

    def existe(self, nombre_completo):
        r = self.s.get(f"{self.url}{self.ruta(nombre_completo)}/api/json",
                       params={"tree": "name"}, timeout=30)
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        r.raise_for_status()

    def get_config(self, nombre_completo):
        r = self.s.get(f"{self.url}{self.ruta(nombre_completo)}/config.xml", timeout=60)
        r.raise_for_status()
        return r.content  # bytes, se mantiene el XML tal cual

    def crear(self, nombre_completo, config_xml):
        padre, _, nombre = nombre_completo.rpartition("/")
        r = self.s.post(f"{self.url}{self.ruta(padre)}/createItem",
                        params={"name": nombre},
                        data=config_xml,
                        headers={"Content-Type": "application/xml"},
                        timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")


def unir(carpeta, nombre):
    return f"{carpeta}/{nombre}" if carpeta else nombre


def asegurar_carpetas(origen, destino, nombre_origen, carpeta_destino, simular):
    """Si el job del origen esta dentro de carpetas, las crea (si faltan) bajo la carpeta destino,
    copiando su config del origen."""
    partes = nombre_origen.split("/")[:-1]
    acumulado = ""
    for p in partes:
        acumulado = unir(acumulado, p)
        ruta_dest = unir(carpeta_destino, acumulado)
        if not destino.existe(ruta_dest):
            if simular:
                print(f"    [SIMULACION] Se crearia la carpeta '{ruta_dest}'")
            else:
                print(f"    Creando carpeta '{ruta_dest}' en destino...")
                destino.crear(ruta_dest, origen.get_config(acumulado))


def parsear_seleccion(texto, maximo):
    idx = set()
    for p in texto.replace(" ", ",").split(","):
        if not p:
            continue
        if "-" in p:
            a, b = p.split("-", 1)
            a, b = int(a), int(b)
            idx.update(range(min(a, b), max(a, b) + 1))
        else:
            idx.add(int(p))
    return sorted(i for i in idx if 1 <= i <= maximo)


def elegir_jobs(jobs):
    filtro = ""
    while True:
        vista = [j for j in jobs if filtro.lower() in j.lower()]
        print(f"\nJobs en el ORIGEN{f' (filtro: {filtro!r})' if filtro else ''}:")
        for i, j in enumerate(vista, 1):
            print(f"  [{i:3}] {j}")
        print("\nNumeros (ej: 1,3,5-8) | 'todos' | '/texto' para filtrar | '/' quitar filtro | Enter para salir")
        resp = input("Seleccion: ").strip()
        if not resp:
            return []
        if resp.lower() == "todos":
            return vista
        if resp.startswith("/"):
            filtro = resp[1:]
            continue
        try:
            sel = parsear_seleccion(resp, len(vista))
        except ValueError:
            sel = []
        if not sel:
            print("Entrada no valida.")
            continue
        return [vista[i - 1] for i in sel]


def main():
    ap = argparse.ArgumentParser(description="Migra pipelines de un Jenkins a otro")
    ap.add_argument("--jobs", nargs="+", help="Jobs a migrar (ruta completa, ej carpeta/job)")
    ap.add_argument("--simular", action="store_true", help="No crea nada, solo muestra lo que haria")
    ap.add_argument("--carpeta", default=CARPETA_DESTINO,
                    help="Carpeta del Jenkins 2 donde copiar los jobs (ej: equipo/backend)")
    args = ap.parse_args()
    carpeta_destino = args.carpeta.strip("/")

    if not VERIFICAR_SSL:
        requests.packages.urllib3.disable_warnings()

    print("=== Migracion de pipelines Jenkins ===")
    if args.simular:
        print("*** MODO SIMULACION: no se creara nada en el destino ***")

    origen = Jenkins("Jenkins 1 (origen)", **ORIGEN)
    destino = Jenkins("Jenkins 2 (destino)", **DESTINO)
    origen.conectar()
    destino.conectar()

    if carpeta_destino:
        if not destino.existe(carpeta_destino):
            sys.exit(f"[ERROR] La carpeta '{carpeta_destino}' no existe en {destino.nombre}. Revisa la ruta.")
        print(f"  Carpeta destino: {carpeta_destino}")
    else:
        print("  Carpeta destino: (raiz)")

    if args.jobs:
        seleccion = [j.strip("/") for j in args.jobs]
    elif PIPELINES:
        seleccion = [j.strip().strip("/") for j in PIPELINES if j.strip()]
    else:
        print("Leyendo jobs del origen...")
        todos = sorted(origen.listar_jobs())
        if not todos:
            print("No hay jobs en el origen.")
            return
        seleccion = elegir_jobs(todos)

    if not seleccion:
        print("No se selecciono ningun job.")
        return

    print(f"\nSe procesaran {len(seleccion)} job(s):")
    for j in seleccion:
        print(f"  - {j}  ->  {unir(carpeta_destino, j)}")
    if not args.simular and input("Continuar? (s/n): ").strip().lower() not in ("s", "si", "y", "yes"):
        print("Cancelado.")
        return

    resumen = []
    for nombre in seleccion:
        nombre_dest = unir(carpeta_destino, nombre)
        print(f"\n>> {nombre}  ->  {nombre_dest}")
        try:
            if destino.existe(nombre_dest):
                print("    YA EXISTE en el destino -> no se copia")
                resumen.append((nombre, "YA EXISTE (omitido)"))
                continue

            xml = origen.get_config(nombre)
            asegurar_carpetas(origen, destino, nombre, carpeta_destino, args.simular)

            if args.simular:
                print("    [SIMULACION] Se copiaria")
                resumen.append((nombre, "SE COPIARIA"))
                continue

            destino.crear(nombre_dest, xml)
            print("    COPIADO")
            resumen.append((nombre, "COPIADO"))
        except Exception as e:
            print(f"    ERROR: {e}")
            resumen.append((nombre, f"ERROR: {str(e).splitlines()[0]}"))

    print("\n=== RESUMEN ===")
    ancho = max(len(n) for n, _ in resumen)
    for n, res in resumen:
        print(f"  {n.ljust(ancho)}  {res}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido.")
