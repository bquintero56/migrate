"""
Copia repositorios de GitLab a un grupo del mismo GitLab, con el mismo nombre,
incluyendo TODAS las ramas, tags e historial.

- Pones la URL de cada repo a copiar (REPOS) y la URL del grupo destino (DESTINO).
- Si el repo ya existe en el grupo destino, avisa y NO lo copia.
- Si no existe, lo crea (mismo nombre, descripcion y visibilidad), sube todas las ramas
  y tags, y deja la misma rama por defecto.

Requisitos:
    - git instalado
    - pip install requests
    - Token de GitLab (Personal Access Token) con permisos: api + write_repository
      y rol Maintainer (o mas) en el grupo destino.

Uso:
    python migrar_gitlab.py --simular     # no crea nada, solo muestra lo que haria
    python migrar_gitlab.py
    python migrar_gitlab.py --destino https://gitlab.empresa.com/pais/test/migracion \
                            --repos https://gitlab.empresa.com/grupo/proyecto
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
from urllib.parse import quote, urlparse

import requests

# ======================= CONFIGURACION =======================
# URL del GRUPO donde se van a crear los repos copiados (el grupo debe existir).
DESTINO = "https://gitlab.empresa.com/pais/test/migracion"

# URLs de los repos a copiar (tal como se ven en el navegador).
REPOS = [
    # "https://gitlab.empresa.com/grupo/proyecto-1",
    # "https://gitlab.empresa.com/otro-grupo/sub/proyecto-2",
]

# Token de GitLab (si queda vacio se pide al ejecutar).
TOKEN = ""

# URL base de tu GitLab (donde esta instalado). Ejemplos:
#   "https://gitlab.empresa.com"
#   "https://servidor.empresa.com/gitlab"   (si esta montado en una subruta)
# Vacio = se detecta solo a partir de la URL de DESTINO.
# Para confirmarla: abre <GITLAB_URL>/api/v4/version en el navegador; debe responder GitLab.
GITLAB_URL = ""

# Proxy:
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


def validar_url(url):
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"URL no valida: {url!r} (debe empezar con http:// o https://)")
    return u


def ruta_relativa(url, base):
    """Ruta del grupo/repo dentro del GitLab.
    ('https://srv.com/gitlab/grupo/proy.git', 'https://srv.com/gitlab') -> 'grupo/proy'"""
    ruta = validar_url(url).path
    prefijo = urlparse(base).path.rstrip("/")
    if prefijo and (ruta + "/").lower().startswith(prefijo.lower() + "/"):
        ruta = ruta[len(prefijo):]
    ruta = ruta.split("/-/")[0].strip("/")  # quita cosas como /-/tree/main
    if ruta.startswith("groups/"):          # URLs antiguas de grupo: /groups/pais/test
        ruta = ruta[len("groups/"):]
    if ruta.endswith(".git"):
        ruta = ruta[:-4]
    if not ruta:
        raise ValueError(f"a la URL le falta la ruta del grupo/repo: {url!r}")
    return ruta


def detectar_gitlab_url(url_destino, token):
    """Prueba 'https://host', 'https://host/seg1', ... hasta encontrar donde responde la API de GitLab."""
    u = validar_url(url_destino)
    host = f"{u.scheme}://{u.netloc}"
    segmentos = [s for s in u.path.split("/-/")[0].split("/") if s]
    for i in range(len(segmentos)):
        candidata = host + "".join("/" + s for s in segmentos[:i])
        try:
            r = GitLab(candidata, token).api("GET", "/version")
        except requests.exceptions.RequestException:
            continue
        if r.status_code in (200, 401, 403) and "json" in r.headers.get("Content-Type", ""):
            try:
                datos = r.json()
            except ValueError:
                continue
            if isinstance(datos, dict) and ("version" in datos or "message" in datos):
                return candidata
    return None


class GitLab:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        if not token:
            token = getpass.getpass(f"Token de GitLab ({self.url}): ")
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
            sys.exit(f"[ERROR] fallo el proxy ({descripcion_proxy()}): {e}")
        except requests.exceptions.ConnectionError as e:
            sys.exit(f"[ERROR] no se pudo conectar a {self.url} ({descripcion_proxy()}): {e}")
        if r.status_code in (401, 403):
            detalle = self.ocultar(r.text.strip())[:500] or "(sin contenido)"
            es_json = "json" in r.headers.get("Content-Type", "")
            print(f"[ERROR] HTTP {r.status_code} al validar el token en {self.url}/api/v4/user")
            print(f"        Servidor : {r.headers.get('Server', '?')}")
            print(f"        Via      : {r.headers.get('Via', '-')}")
            print(f"        Respuesta: {detalle}")
            if not es_json:
                print("        -> La respuesta NO es de la API de GitLab (no es JSON). Probablemente la")
                print("           bloquea el PROXY o un firewall. Prueba con PROXY = \"sin\" o con otro proxy.")
            elif r.status_code == 401:
                print("        -> Token invalido, expirado o mal copiado.")
            else:
                print("        -> GitLab rechazo el token: revisa que tenga el scope 'api' (o 'read_user').")
            sys.exit(1)
        r.raise_for_status()
        self.usuario = r.json()["username"]
        print(f"  [OK] Conectado a {self.url} como {self.usuario}  ({descripcion_proxy()})")

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
    """Ejecuta git autenticando con el token y usando el proxy configurado."""
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


def borrar_carpeta(ruta):
    def forzar(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)  # en Windows git deja archivos de solo lectura
        func(path)
    shutil.rmtree(ruta, onerror=forzar)


def migrar(gl, url_repo, grupo, simular):
    ruta_origen = ruta_relativa(url_repo, gl.url)

    src = gl.proyecto(ruta_origen)
    if src is None:
        raise RuntimeError("el repo no existe (o el token no tiene acceso)")

    ruta_destino = f"{grupo['full_path']}/{src['path']}"
    print(f"    {ruta_origen}  ->  {ruta_destino}")

    if gl.proyecto(ruta_destino) is not None:
        print("    YA EXISTE en el grupo destino -> no se copia")
        return "YA EXISTE (omitido)"

    if simular:
        print(f"    [SIMULACION] Se crearia '{src['name']}' con todas sus ramas y tags "
              f"(rama por defecto: {src.get('default_branch')})")
        return "SE COPIARIA"

    # 1) Crear el proyecto vacio en el grupo destino
    r = gl.api("POST", "/projects", data={
        "name": src["name"],
        "path": src["path"],
        "namespace_id": grupo["id"],
        "description": src.get("description") or "",
        "visibility": src.get("visibility", "private"),
    })
    if r.status_code != 201:
        raise RuntimeError(f"no se pudo crear el proyecto: HTTP {r.status_code} {r.text[:300]}")
    nuevo = r.json()
    print("    Proyecto creado")

    # 2) Clonar en espejo y subir todas las ramas + tags
    tmp = tempfile.mkdtemp(prefix="migrar_gitlab_")
    try:
        carpeta = os.path.join(tmp, "repo.git")
        print("    Clonando (todas las ramas y tags)...")
        git(gl, ["clone", "--mirror", gl.url_git(ruta_origen), carpeta])
        ramas = len(git(gl, ["for-each-ref", "refs/heads"], cwd=carpeta).splitlines())
        tags = len(git(gl, ["for-each-ref", "refs/tags"], cwd=carpeta).splitlines())
        print(f"    Subiendo {ramas} rama(s) y {tags} tag(s)...")
        if ramas or tags:
            git(gl, ["push", gl.url_git(ruta_destino),
                     "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"], cwd=carpeta)
        else:
            print("    (el repo origen esta vacio)")
    except Exception as e:
        raise RuntimeError(f"{e}\n    (el proyecto quedo creado vacio: {nuevo['web_url']})")
    finally:
        borrar_carpeta(tmp)

    # 3) Misma rama por defecto que en el origen
    if src.get("default_branch"):
        gl.api("PUT", f"/projects/{nuevo['id']}", data={"default_branch": src["default_branch"]})

    print(f"    COPIADO -> {nuevo['web_url']}")
    return f"COPIADO ({ramas} ramas, {tags} tags)"


def main():
    ap = argparse.ArgumentParser(description="Copia repos de GitLab a un grupo con todas sus ramas")
    ap.add_argument("--destino", default=DESTINO, help="URL del grupo destino")
    ap.add_argument("--repos", nargs="+", help="URLs de los repos a copiar")
    ap.add_argument("--simular", action="store_true", help="No crea nada, solo muestra lo que haria")
    args = ap.parse_args()

    if not VERIFICAR_SSL:
        requests.packages.urllib3.disable_warnings()
    if shutil.which("git") is None:
        sys.exit("[ERROR] git no esta instalado o no esta en el PATH")

    print("=== Migracion de repositorios GitLab ===")
    if args.simular:
        print("*** MODO SIMULACION: no se creara nada ***")

    repos = [r.strip() for r in (args.repos or REPOS) if r.strip()]
    if not repos:
        sys.exit("No hay repos para copiar: llena la lista REPOS o usa --repos")

    try:
        destino_url = validar_url(args.destino)
    except ValueError as e:
        sys.exit(f"[ERROR] DESTINO: {e}")

    token = TOKEN or getpass.getpass("Token de GitLab: ")

    if GITLAB_URL.strip():
        base = GITLAB_URL.strip().rstrip("/")
    else:
        print("Detectando la URL de la API de GitLab...")
        base = detectar_gitlab_url(args.destino, token)
        if base is None:
            base = f"{destino_url.scheme}://{destino_url.netloc}"
            print(f"  [!] No se pudo detectar; se usara {base}. Si falla, configura GITLAB_URL.")
    print(f"  API de GitLab: {base}/api/v4")

    try:
        ruta_grupo = ruta_relativa(args.destino, base)
    except ValueError as e:
        sys.exit(f"[ERROR] DESTINO: {e}")

    gl = GitLab(base, token)
    gl.conectar()

    grupo = gl.grupo(ruta_grupo)
    if grupo is None:
        sys.exit(f"[ERROR] El grupo destino '{ruta_grupo}' no existe (o el token no tiene acceso).")
    print(f"  Grupo destino: {grupo['full_path']}")

    print(f"\nSe copiaran {len(repos)} repo(s) a '{grupo['full_path']}':")
    for r in repos:
        print(f"  - {r}")
    if not args.simular and input("Continuar? (s/n): ").strip().lower() not in ("s", "si", "y", "yes"):
        print("Cancelado.")
        return

    resumen = []
    for url_repo in repos:
        print(f"\n>> {url_repo}")
        try:
            res = migrar(gl, url_repo, grupo, args.simular)
        except Exception as e:
            msg = gl.ocultar(str(e))
            print(f"    ERROR: {msg}")
            res = "ERROR: " + msg.splitlines()[0]
        resumen.append((url_repo, res))

    print("\n=== RESUMEN ===")
    ancho = max(len(n) for n, _ in resumen)
    for n, res in resumen:
        print(f"  {n.ljust(ancho)}  {res}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido.")
