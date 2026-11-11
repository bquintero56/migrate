"""
Copia repositorios de GitLab (origen) a otro grupo/GitLab (destino) con el mismo nombre,
incluyendo TODAS las ramas, tags e historial.

- Si el repo ya existe en el destino, avisa y NO lo copia.
- Si no existe, crea el proyecto en el grupo destino (mismo nombre, descripcion y visibilidad),
  sube todas las ramas y tags, y deja la misma rama por defecto.

Requisitos:
    - git instalado
    - pip install requests
    - Token de GitLab (Personal Access Token) con permisos:
        origen : read_api + read_repository
        destino: api + write_repository

Uso:
    python migrar_gitlab.py                        # usa la lista REPOS de abajo
    python migrar_gitlab.py --repos https://gitlab.empresa.com/grupo/proyecto
    python migrar_gitlab.py --grupo pais/test/migracion --simular
"""

import argparse
import base64
import getpass
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote

import requests

# ======================= CONFIGURACION =======================
ORIGEN = {
    "url": "https://gitlab-origen.com",   # URL base del GitLab origen
    "token": "",                           # si queda vacio se pide al ejecutar
}
DESTINO = {
    "url": "https://gitlab-destino.com",  # puede ser el mismo GitLab que el origen
    "token": "",
}

# Grupo del GitLab destino donde se crearan los repos (ej: "pais/test/migracion").
# Vacio = en el espacio personal del usuario del token. Se puede sobreescribir con --grupo.
# El grupo debe existir en el destino.
GRUPO_DESTINO = ""

# Repos a copiar: URL completa o ruta (ej: "grupo/proyecto").
REPOS = [
    # "https://gitlab-origen.com/grupo/proyecto-1",
    # "grupo/sub/proyecto-2",
]

# Proxy usado para conectarse a los dos GitLab (API y git):
#   ""          -> usa el proxy del sistema (variables HTTP_PROXY / HTTPS_PROXY) si existe
#   "http://proxy.empresa.com:8080"              -> usa ese proxy
#   "http://usuario:clave@proxy.empresa.com:8080" -> proxy con autenticacion
#   "sin"       -> conexion directa, ignora cualquier proxy del sistema
PROXY = ""
VERIFICAR_SSL = True  # ponlo en False si usan certificados autofirmados
# =============================================================

VARIABLES_PROXY = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def descripcion_proxy():
    p = PROXY.strip()
    if p.lower() == "sin":
        return "sin proxy"
    if p:
        return "proxy " + p.split("@")[-1]  # no mostrar la clave del proxy
    return "proxy del sistema (si hay)"


class GitLab:
    def __init__(self, nombre, url, token):
        self.nombre = nombre
        self.url = url.rstrip("/")
        if not token:
            token = getpass.getpass(f"Token de GitLab para {nombre} ({self.url}): ")
        self.token = token
        self.basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
        self.usuario = None

        self.s = requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = token
        self.s.verify = VERIFICAR_SSL
        p = PROXY.strip()
        if p.lower() == "sin":
            self.s.trust_env = False
        elif p:
            self.s.proxies = {"http": p, "https": p}

    def api(self, metodo, ruta, **kw):
        return self.s.request(metodo, f"{self.url}/api/v4{ruta}", timeout=60, **kw)

    def conectar(self):
        try:
            r = self.api("GET", "/user")
        except requests.exceptions.ProxyError as e:
            sys.exit(f"[ERROR] {self.nombre}: fallo el proxy ({descripcion_proxy()}): {e}")
        except requests.exceptions.ConnectionError as e:
            sys.exit(f"[ERROR] {self.nombre}: no se pudo conectar a {self.url} ({descripcion_proxy()}): {e}")
        if r.status_code in (401, 403):
            sys.exit(f"[ERROR] {self.nombre}: token invalido o sin permisos (HTTP {r.status_code})")
        r.raise_for_status()
        self.usuario = r.json()["username"]
        print(f"  [OK] Conectado a {self.nombre}: {self.url} como {self.usuario}  ({descripcion_proxy()})")

    def _get(self, tipo, ruta_completa):
        r = self.api("GET", f"/{tipo}/{quote(ruta_completa, safe='')}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        r.raise_for_status()

    def proyecto(self, ruta):
        return self._get("projects", ruta)

    def grupo(self, ruta):
        return self._get("groups", ruta)

    def url_git(self, ruta):
        return f"{self.url}/{ruta}.git"

    def ocultar(self, texto):
        return texto.replace(self.token, "***").replace(self.basic, "***")


def git(gl, args, cwd=None):
    """Ejecuta git autenticando contra el GitLab indicado y usando el proxy configurado."""
    cmd = ["git",
           "-c", f"http.extraHeader=Authorization: Basic {gl.basic}",
           "-c", "credential.helper="]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    p = PROXY.strip()
    if p.lower() == "sin":
        cmd += ["-c", "http.proxy="]
        for v in VARIABLES_PROXY:
            env.pop(v, None)
    elif p:
        cmd += ["-c", f"http.proxy={p}"]
    if not VERIFICAR_SSL:
        cmd += ["-c", "http.sslVerify=false"]
    cmd += args

    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(gl.ocultar(r.stderr.strip() or r.stdout.strip()))
    return r.stdout


def ruta_desde_entrada(entrada, base_url):
    """Convierte 'https://gitlab/grupo/proy(.git)' o 'grupo/proy' en 'grupo/proy'."""
    e = entrada.strip()
    if e.lower().startswith(("http://", "https://")):
        if not e.lower().startswith(base_url.lower() + "/"):
            raise ValueError(f"la URL no pertenece al GitLab origen ({base_url})")
        e = e[len(base_url) + 1:]
    elif e.startswith("git@"):
        e = e.split(":", 1)[1]
    e = e.split("/-/")[0].strip("/")  # quita cosas como /-/tree/main
    if e.endswith(".git"):
        e = e[:-4]
    return e


def borrar_carpeta(ruta):
    def forzar(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)  # en Windows git deja archivos de solo lectura
        func(path)
    shutil.rmtree(ruta, onerror=forzar)


def migrar(origen, destino, entrada, grupo, simular):
    ruta_origen = ruta_desde_entrada(entrada, origen.url)
    print(f"\n>> {ruta_origen}")

    src = origen.proyecto(ruta_origen)
    if src is None:
        raise RuntimeError("no existe en el GitLab origen (o el token no tiene acceso)")

    namespace = grupo["full_path"] if grupo else destino.usuario
    ruta_destino = f"{namespace}/{src['path']}"
    print(f"    destino: {destino.url}/{ruta_destino}")

    if destino.proyecto(ruta_destino) is not None:
        print("    YA EXISTE en el destino -> no se copia")
        return "YA EXISTE (omitido)"

    if simular:
        print(f"    [SIMULACION] Se crearia '{src['name']}' y se subirian todas las ramas y tags "
              f"(rama por defecto: {src.get('default_branch')})")
        return "SE COPIARIA"

    # 1) Crear el proyecto vacio en el destino
    datos = {
        "name": src["name"],
        "path": src["path"],
        "description": src.get("description") or "",
        "visibility": src.get("visibility", "private"),
    }
    if grupo:
        datos["namespace_id"] = grupo["id"]
    r = destino.api("POST", "/projects", data=datos)
    if r.status_code != 201:
        raise RuntimeError(f"no se pudo crear el proyecto en destino: HTTP {r.status_code} {r.text[:300]}")
    nuevo = r.json()
    print("    Proyecto creado en destino")

    # 2) Clonar en espejo y subir ramas + tags
    tmp = tempfile.mkdtemp(prefix="migrar_gitlab_")
    try:
        carpeta = os.path.join(tmp, "repo.git")
        print("    Clonando desde origen (todas las ramas y tags)...")
        git(origen, ["clone", "--mirror", origen.url_git(ruta_origen), carpeta])
        ramas = len(git(origen, ["for-each-ref", "refs/heads"], cwd=carpeta).splitlines())
        tags = len(git(origen, ["for-each-ref", "refs/tags"], cwd=carpeta).splitlines())
        print(f"    Subiendo al destino {ramas} rama(s) y {tags} tag(s)...")
        if ramas == 0 and tags == 0:
            print("    (el repo origen esta vacio)")
        else:
            git(destino, ["push", destino.url_git(ruta_destino),
                          "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"], cwd=carpeta)
    except Exception as e:
        raise RuntimeError(f"{e}\n    (el proyecto vacio quedo creado en destino: {nuevo['web_url']})")
    finally:
        borrar_carpeta(tmp)

    # 3) Misma rama por defecto que en el origen
    rama_def = src.get("default_branch")
    if rama_def:
        destino.api("PUT", f"/projects/{nuevo['id']}", data={"default_branch": rama_def})

    print(f"    COPIADO -> {nuevo['web_url']}")
    return f"COPIADO ({ramas} ramas, {tags} tags)"


def main():
    ap = argparse.ArgumentParser(description="Copia repos de GitLab a otro grupo/GitLab con todas sus ramas")
    ap.add_argument("--repos", nargs="+", help="URLs o rutas de los repos a copiar")
    ap.add_argument("--grupo", default=GRUPO_DESTINO, help="Grupo destino (ej: pais/test/migracion)")
    ap.add_argument("--simular", action="store_true", help="No crea nada, solo muestra lo que haria")
    args = ap.parse_args()

    if not VERIFICAR_SSL:
        requests.packages.urllib3.disable_warnings()
    if shutil.which("git") is None:
        sys.exit("[ERROR] git no esta instalado o no esta en el PATH")

    print("=== Migracion de repositorios GitLab ===")
    if args.simular:
        print("*** MODO SIMULACION: no se creara nada en el destino ***")

    repos = [r for r in (args.repos or REPOS) if r.strip()]
    if not repos:
        sys.exit("No hay repos para copiar: llena la lista REPOS o usa --repos")

    origen = GitLab("GitLab origen", ORIGEN["url"], ORIGEN["token"])
    if DESTINO["url"].rstrip("/") == origen.url and not DESTINO["token"]:
        destino = GitLab("GitLab destino", DESTINO["url"], origen.token)  # mismo GitLab, mismo token
    else:
        destino = GitLab("GitLab destino", DESTINO["url"], DESTINO["token"])
    origen.conectar()
    destino.conectar()

    grupo_ruta = args.grupo.strip("/")
    grupo = None
    if grupo_ruta:
        grupo = destino.grupo(grupo_ruta)
        if grupo is None:
            sys.exit(f"[ERROR] El grupo '{grupo_ruta}' no existe en el GitLab destino (o no tienes acceso).")
        print(f"  Grupo destino: {grupo['full_path']}")
    else:
        print(f"  Grupo destino: espacio personal de {destino.usuario}")

    print(f"\nSe procesaran {len(repos)} repo(s):")
    for r in repos:
        print(f"  - {r}")
    if not args.simular and input("Continuar? (s/n): ").strip().lower() not in ("s", "si", "y", "yes"):
        print("Cancelado.")
        return

    resumen = []
    for entrada in repos:
        try:
            res = migrar(origen, destino, entrada, grupo, args.simular)
        except Exception as e:
            msg = origen.ocultar(destino.ocultar(str(e)))
            print(f"    ERROR: {msg}")
            res = "ERROR: " + msg.splitlines()[0]
        resumen.append((entrada, res))

    print("\n=== RESUMEN ===")
    ancho = max(len(n) for n, _ in resumen)
    for n, res in resumen:
        print(f"  {n.ljust(ancho)}  {res}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido.")
