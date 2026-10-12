#!/usr/bin/env python3
"""DeckSpace: panel de espacio en disco de la Steam Deck y limpieza segura de restos."""

import datetime as dt
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

HOME = os.path.expanduser("~")
STEAM = os.path.join(HOME, ".local/share/Steam")
BACKUP_ROOT = os.path.join(HOME, "Backups/DeckSpace")
NAMES_CACHE = os.path.join(HOME, ".cache/DeckSpace/names.json")
STEAM_SHORTCUT_MIN = 2 ** 31  # los IDs de juegos no de Steam son >= 2^31
TOOL_RE = re.compile(r"^(Proton|Steam Linux Runtime|Steamworks Common)", re.I)
OFFICIAL_PROTON_RE = re.compile(r"^Proton (\d+)\.0$")
EXTERNAL_TOOLS = ("ULWGL", "UMU")  # Protons para lanzadores externos (umu/Heroic/Lutris), no los usa Steam
STALE_DAYS = 180

# Tema oscuro inspirado en la Steam Deck
BG, SURFACE, SURFACE2, BORDER = "#0e1319", "#161d26", "#1e2732", "#2b3644"
FG, MUTED, ACCENT, ACCENT_DARK = "#e6edf3", "#8b98a5", "#1a9fff", "#1d4f7c"
OK, WARN, DANGER = "#3ecf8e", "#f2b84b", "#ff6b6b"

COLORS = {
    "Juegos de Steam": "#1a9fff",
    "Proton y runtimes": "#6c7cf0",
    "Steam (programa)": "#5fb3d9",
    "Datos de Proton": "#a879f7",
    "Caché de shaders": "#f29d38",
    "Descargas/temporales": "#ff6b6b",
    "Emulación": "#3ecf8e",
    "Flatpak": "#e07bb5",
    "Copias (Backups)": "#9aa5b1",
    "Otros": "#4a5868",
}
FREE_COLOR = "#232d39"

GROUPS = {  # orden, título y riesgo de cada tipo de resto
    "compat": ("Datos de Proton de juegos desinstalados", "backup"),
    "shader": ("Cachés de shaders huérfanas", "safe"),
    "temp": ("Descargas abandonadas y temporales", "safe"),
    "proton_custom": ("Versiones de Proton personalizadas sin usar", "review"),
    "proton_official": ("Versiones oficiales de Proton sin usar", "review"),
}
RISK = {
    "safe": ("● Sin riesgo", OK),
    "backup": ("● Guarda partidas", ACCENT),
    "review": ("● Revisar", WARN),
}


# ---------------------------------------------------------------- utilidades

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def ago(ts):
    if not ts:
        return "Nunca"
    days = (time.time() - ts) / 86400
    if days < 1:
        return "Hoy"
    if days < 2:
        return "Ayer"
    if days < 30:
        return f"Hace {int(days)} días"
    if days < 365:
        m = int(days / 30)
        return f"Hace {m} mes" + ("es" if m > 1 else "")
    y = int(days / 365)
    return f"Hace {y} año" + ("s" if y > 1 else "")


def du(path):
    """Tamaño real en disco (bloques), sin seguir enlaces ni repetir hardlinks."""
    if not os.path.lexists(path):
        return 0
    try:
        out = subprocess.run(["du", "-s", "-B1", path], capture_output=True, text=True, timeout=600).stdout
        return int(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return 0


def du_many(paths):
    """{ruta: tamaño} calculando en paralelo (el disco aguanta varias lecturas a la vez)."""
    paths = list(dict.fromkeys(paths))
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(paths, pool.map(du, paths)))


def du_children(path):
    """[(nombre, ruta, tamaño, es_carpeta)] de los hijos directos, de mayor a menor."""
    try:
        out = subprocess.run(["du", "-a", "-x", "-B1", "--max-depth=1", path],
                             capture_output=True, text=True, timeout=900).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        size, _, p = line.partition("\t")
        if p and os.path.normpath(p) != os.path.normpath(path):
            rows.append((os.path.basename(p), p, int(size), os.path.isdir(p) and not os.path.islink(p)))
    return sorted(rows, key=lambda r: -r[2])


def mount_of(path):
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def drive_label(path):
    return "microSD" if mount_of(path).startswith("/run/media") else "SSD"


def vdf_value(text, key):
    m = re.search(rf'"{key}"\s+"([^"]*)"', text, re.I)
    return m.group(1) if m else ""


def vdf_block(text, key):
    """Contenido entre llaves de la primera clave `key` en un VDF de texto."""
    i = text.find(f'"{key}"')
    if i < 0:
        return ""
    start = text.find("{", i)
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:j]
    return ""


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


# ---------------------------------------------------------------- Steam

def steam_libraries():
    libs = []
    try:
        with open(os.path.join(STEAM, "steamapps/libraryfolders.vdf")) as f:
            paths = re.findall(r'"path"\s+"([^"]+)"', f.read())
    except OSError:
        paths = [STEAM]
    for p in paths:
        sa = os.path.realpath(os.path.join(p, "steamapps"))
        if os.path.isdir(sa) and sa not in libs:
            libs.append(sa)
    return libs


def installed_games():
    games = {}
    for lib in steam_libraries():
        for f in os.listdir(lib):
            if not (f.startswith("appmanifest_") and f.endswith(".acf")):
                continue
            try:
                with open(os.path.join(lib, f), encoding="utf-8", errors="replace") as fh:
                    t = fh.read()
            except OSError:
                continue
            appid = vdf_value(t, "appid")
            if not appid:
                continue
            name = vdf_value(t, "name") or f"App {appid}"
            games[appid] = {
                "appid": appid, "name": name, "library": lib, "drive": drive_label(lib),
                "installdir": vdf_value(t, "installdir"),
                "size": int(vdf_value(t, "SizeOnDisk") or 0),
                "last_played": int(vdf_value(t, "LastPlayed") or 0),
                "state": int(vdf_value(t, "StateFlags") or 0),
                "tool": bool(TOOL_RE.match(name)),
            }
    return games


def parse_binary_vdf(d, i=0):
    out = {}
    while i < len(d):
        t = d[i]
        i += 1
        if t == 8:
            return out, i
        j = d.index(b"\0", i)
        k = d[i:j].decode("utf-8", "replace")
        i = j + 1
        if t == 0:
            v, i = parse_binary_vdf(d, i)
        elif t == 1:
            j = d.index(b"\0", i)
            v = d[i:j].decode("utf-8", "replace")
            i = j + 1
        elif t == 2:
            v = struct.unpack("<I", d[i:i + 4])[0]
            i += 4
        else:
            raise ValueError(f"tipo VDF desconocido {t}")
        out[k] = v
    return out, i


def shortcuts():
    """{appid: nombre} de los juegos no de Steam (solo lectura de shortcuts.vdf)."""
    result = {}
    base = os.path.join(STEAM, "userdata")
    for u in os.listdir(base) if os.path.isdir(base) else []:
        try:
            with open(os.path.join(base, u, "config/shortcuts.vdf"), "rb") as f:
                root, _ = parse_binary_vdf(f.read())
        except (OSError, ValueError):
            continue
        for e in root.get("shortcuts", {}).values():
            lower = {k.lower(): v for k, v in e.items()}
            if isinstance(lower.get("appid"), int):
                result[str(lower["appid"])] = lower.get("appname", "")
    return result


def compat_mapping():
    """{appid: nombre interno de la herramienta Proton forzada} según config.vdf."""
    try:
        with open(os.path.join(STEAM, "config/config.vdf"), encoding="utf-8", errors="replace") as f:
            block = vdf_block(f.read(), "CompatToolMapping")
    except OSError:
        return {}
    return dict(re.findall(r'"(\d+)"\s*\{\s*"name"\s*"([^"]*)"', block))


def custom_compat_tools():
    """[(carpeta, nombre interno)] de compatibilitytools.d."""
    base = os.path.join(STEAM, "compatibilitytools.d")
    tools = []
    for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        p = os.path.join(base, d)
        try:
            with open(os.path.join(p, "compatibilitytool.vdf"), encoding="utf-8", errors="replace") as f:
                name = re.search(r'"([^"]+)"', vdf_block(f.read(), "compat_tools")).group(1)
        except (OSError, AttributeError):
            continue
        tools.append((p, name))
    return tools


def store_names(appids):
    """Nombres de juegos desinstalados desde la tienda de Steam (con caché)."""
    cache = load_json(NAMES_CACHE, {})
    missing = [a for a in appids if a not in cache and a.isdigit() and 0 < int(a) < STEAM_SHORTCUT_MIN]
    for a in missing:
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={a}&filters=basic"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.load(r).get(a, {})
            if data.get("success"):
                cache[a] = data["data"]["name"]
        except Exception:  # sin red: se muestra el ID
            break
    if missing:
        save_json(NAMES_CACHE, cache)
    return cache


def steam_running():
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0


# ---------------------------------------------------------------- análisis

def analyze(progress):
    progress("Leyendo la biblioteca de Steam…")
    games = installed_games()
    sc = shortcuts()
    libs = steam_libraries()

    folders = {"compatdata": {}, "shadercache": {}}
    for lib in libs:
        for kind, store in folders.items():
            base = os.path.join(lib, kind)
            for appid in os.listdir(base) if os.path.isdir(base) else []:
                p = os.path.join(base, appid)
                if os.path.isdir(p) and not os.path.islink(p):
                    store.setdefault(appid, []).append(p)

    # Todo lo que hay que medir, en paralelo
    extra = [(os.path.join(HOME, ".local/share/flatpak"), "Flatpak"), ("/var/lib/flatpak", "Flatpak"),
             (os.path.join(HOME, ".var/app"), "Flatpak"), (os.path.join(HOME, "Backups"), "Copias (Backups)"),
             (os.path.join(STEAM, "compatibilitytools.d"), "Proton y runtimes")]
    for name in os.listdir(STEAM) if os.path.isdir(STEAM) else []:
        p = os.path.join(STEAM, name)
        if name not in ("steamapps", "compatibilitytools.d") and not os.path.islink(p):
            extra.append((p, "Steam (programa)"))
    mounts = sorted({mount_of(HOME)} | {mount_of(l) for l in libs})
    for m in mounts:
        extra += [(os.path.join(m, "Emulation"), "Emulación"), (os.path.join(m, "SaveVault"), "Copias (Backups)")]
    extra = [(p, c) for p, c in extra if os.path.isdir(p)]
    tools = custom_compat_tools()

    progress("Midiendo carpetas…")
    all_paths = [p for store in folders.values() for ps in store.values() for p in ps]
    all_paths += [p for p, _ in extra] + [p for p, _ in tools]
    sizes = du_many(all_paths)

    compat = {a: [(p, sizes[p]) for p in ps] for a, ps in folders["compatdata"].items()}
    shader = {a: [(p, sizes[p]) for p in ps] for a, ps in folders["shadercache"].items()}
    for g in games.values():
        g["compat"] = sum(s for _, s in compat.get(g["appid"], []))
        g["shader"] = sum(s for _, s in shader.get(g["appid"], []))
        g["total"] = g["size"] + g["compat"] + g["shader"]

    progress("Buscando restos que se pueden limpiar…")
    junk = []
    orphan_ids = [a for a in set(compat) | set(shader) if a not in games and a not in sc]
    names = store_names(orphan_ids)
    for appid in orphan_ids:
        if appid == "0":
            name = "Carpeta «0» (resto de un error de Steam)"
        elif appid.isdigit() and int(appid) >= STEAM_SHORTCUT_MIN:
            name = names.get(appid) or f"Juego no de Steam ya borrado · {appid}"
        else:
            name = names.get(appid) or f"Juego desinstalado · {appid}"
        for p, s in compat.get(appid, []):
            junk.append({"group": "compat", "kind": "compat", "path": p, "size": s, "name": name, "appid": appid,
                         "note": "Antes de borrar se copia la carpeta del usuario del prefijo (donde suelen estar "
                                 f"las partidas) a {BACKUP_ROOT}."})
        for p, s in shader.get(appid, []):
            junk.append({"group": "shader", "kind": "dir", "path": p, "size": s, "name": name, "appid": appid,
                         "note": "Shaders precompilados de un juego que ya no está. Se pueden borrar sin riesgo."})

    for lib in libs:
        dl = os.path.join(lib, "downloading")
        for appid in os.listdir(dl) if os.path.isdir(dl) else []:
            p, g = os.path.join(dl, appid), games.get(appid)
            if os.path.isdir(p) and (g is None or g["state"] == 4):
                s = du(p)
                if s > 0:
                    junk.append({"group": "temp", "kind": "dir", "path": p, "size": s, "appid": appid,
                                 "name": f"Descarga de {g['name'] if g else 'App ' + appid}",
                                 "note": "Descarga que quedó a medias de un juego que ya está completo o desinstalado."})
        tmp = os.path.join(lib, "temp")
        if os.path.isdir(tmp):
            s = du(tmp)
            if s > 1024 * 1024:
                junk.append({"group": "temp", "kind": "contents", "path": tmp, "size": s, "appid": "",
                             "name": "Temporales de Steam",
                             "note": "Archivos temporales de Steam. Mejor limpiar cuando no esté descargando."})
        for f in os.listdir(lib):
            if re.match(r"appmanifest_\d+\.acf\.\d+\.tmp$", f):
                p = os.path.join(lib, f)
                junk.append({"group": "temp", "kind": "file", "path": p, "size": os.lstat(p).st_size,
                             "appid": "", "name": f, "note": "Manifiesto temporal que Steam no llegó a borrar."})

    mapping = compat_mapping()
    in_use = {n for a, n in mapping.items() if a in games or a in sc or a == "0"}
    for path, name in tools:
        if name in in_use:
            continue
        external = os.path.basename(path).upper().startswith(EXTERNAL_TOOLS)
        junk.append({"group": "proton_custom", "kind": "dir", "path": path, "size": sizes[path], "appid": "",
                     "name": os.path.basename(path),
                     "note": ("No la usa ningún juego de Steam, pero la usan lanzadores externos (umu, Heroic, "
                              "Lutris). Bórrala solo si no los usas." if external else
                              "No la usa ningún juego instalado. Si algún día hiciera falta, se reinstala con "
                              "ProtonUp-Qt.")})
    official = {int(m.group(1)): g for g in games.values() if (m := OFFICIAL_PROTON_RE.match(g["name"]))}
    if official:
        newest = max(official)
        for n, g in official.items():
            if n < newest and f"proton_{n}" not in in_use:
                junk.append({"group": "proton_official", "kind": "steam_uninstall", "path": g["library"],
                             "size": g["size"], "appid": g["appid"], "name": g["name"],
                             "note": "Ningún juego la tiene asignada. La desinstala Steam (pedirá confirmación) y "
                                     "la vuelve a descargar sola si algún juego la necesita."})

    junk.sort(key=lambda j: (list(GROUPS).index(j["group"]), -j["size"]))

    progress("Calculando el reparto de cada unidad…")
    drives = []
    for m in mounts:
        u = shutil.disk_usage(m)
        drives.append({"label": "microSD" if m.startswith("/run/media") else "SSD interno", "mount": m,
                       "total": u.total, "used": u.used, "free": u.free, "parts": {}})
    by_mount = {d["mount"]: d for d in drives}

    def add(path, cat, size):
        d = by_mount.get(mount_of(path))
        if d:
            d["parts"][cat] = d["parts"].get(cat, 0) + size

    for g in games.values():
        add(g["library"], "Proton y runtimes" if g["tool"] else "Juegos de Steam", g["size"])
    for store, cat in ((compat, "Datos de Proton"), (shader, "Caché de shaders")):
        for entries in store.values():
            for p, s in entries:
                add(p, cat, s)
    for j in junk:
        if j["group"] == "temp":
            add(j["path"], "Descargas/temporales", j["size"])
    for p, cat in extra:
        add(p, cat, sizes[p])
    for d in drives:
        d["parts"]["Otros"] = max(d["used"] - sum(d["parts"].values()), 0)

    return {"games": list(games.values()), "junk": junk, "drives": drives,
            "orphan_total": sum(j["size"] for j in junk), "when": time.time()}


# ---------------------------------------------------------------- limpieza

def clean(items, log):
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    freed = 0
    for it in items:
        p = it["path"]
        try:
            if it["kind"] == "steam_uninstall":
                subprocess.Popen(["steam", f"steam://uninstall/{it['appid']}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log(f"→ {it['name']}: Steam pedirá confirmación para desinstalarla")
                continue
            if not os.path.lexists(p):
                continue
            if it["kind"] == "compat":
                user = os.path.join(p, "pfx/drive_c/users/steamuser")
                if os.path.isdir(user):
                    dest = os.path.join(BACKUP_ROOT, stamp, f"{it['appid']}-{re.sub(r'[^\w.-]+', '_', it['name'])}")
                    os.makedirs(dest, exist_ok=True)
                    r = subprocess.run(["rsync", "-a", user + "/", os.path.join(dest, "steamuser") + "/"],
                                       capture_output=True, text=True)
                    if r.returncode not in (0, 23, 24):
                        raise RuntimeError(f"no se pudo copiar las partidas: {r.stderr.strip()[-200:]}")
                    log(f"Partidas de {it['name']} copiadas en {dest}")
                shutil.rmtree(p)
            elif it["kind"] == "dir":
                shutil.rmtree(p)
            elif it["kind"] == "contents":
                for name in os.listdir(p):
                    q = os.path.join(p, name)
                    shutil.rmtree(q) if os.path.isdir(q) and not os.path.islink(q) else os.remove(q)
            elif it["kind"] == "file":
                os.remove(p)
            freed += it["size"]
            log(f"✔ {it['name']} ({human(it['size'])})")
        except Exception as e:  # noqa: BLE001
            log(f"✖ {it['name']}: {e}")
    log(f"Liberado: {human(freed)}")
    return freed


# ---------------------------------------------------------------- interfaz: estilo

def setup_style(root):
    root.configure(background=BG)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(name).configure(size=11)
    tkfont.nametofont("TkHeadingFont").configure(size=10, weight="bold")
    base = tkfont.nametofont("TkDefaultFont").actual("family")

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=FG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                troughcolor=SURFACE2, focuscolor=ACCENT, fieldbackground=SURFACE2, insertcolor=FG)
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=SURFACE)
    s.configure("TLabel", background=BG, foreground=FG)
    s.configure("Muted.TLabel", foreground=MUTED)
    s.configure("Card.TLabel", background=SURFACE)
    s.configure("CardMuted.TLabel", background=SURFACE, foreground=MUTED)
    s.configure("Title.TLabel", font=(base, 20, "bold"))
    s.configure("H2.TLabel", background=SURFACE, font=(base, 13, "bold"))

    s.configure("TButton", background=SURFACE2, foreground=FG, borderwidth=0, padding=(14, 8), relief="flat")
    s.map("TButton", background=[("disabled", SURFACE), ("pressed", BORDER), ("active", BORDER)],
          foreground=[("disabled", MUTED)])
    s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
    s.map("Accent.TButton", background=[("disabled", ACCENT_DARK), ("pressed", "#1580d0"), ("active", "#3aaeff")])
    s.configure("Danger.TButton", background="#c94848", foreground="#ffffff")
    s.map("Danger.TButton", background=[("disabled", "#5a2c2c"), ("pressed", "#a83a3a"), ("active", "#de5a5a")],
          foreground=[("disabled", "#c9a3a3")])

    s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(22, 10), borderwidth=0,
                font=(base, 11, "bold"))
    s.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", SURFACE2)],
          foreground=[("selected", FG), ("active", FG)])

    s.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=FG, rowheight=36,
                borderwidth=0, relief="flat")
    s.map("Treeview", background=[("selected", ACCENT_DARK)], foreground=[("selected", "#ffffff")])
    s.configure("Treeview.Heading", background=SURFACE2, foreground=MUTED, relief="flat", padding=(8, 8),
                borderwidth=0)
    s.map("Treeview.Heading", background=[("active", BORDER)])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    s.configure("TEntry", fieldbackground=SURFACE2, foreground=FG, bordercolor=BORDER, padding=6)
    s.configure("TCombobox", fieldbackground=SURFACE2, background=SURFACE2, foreground=FG, arrowcolor=FG,
                padding=5)
    s.map("TCombobox", fieldbackground=[("readonly", SURFACE2)], foreground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", SURFACE2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DARK)
    s.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=SURFACE2,
                indicatorforeground=FG)
    s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", ACCENT)])
    s.configure("Vertical.TScrollbar", background=SURFACE2, troughcolor=SURFACE, arrowcolor=MUTED,
                borderwidth=0, relief="flat")
    s.configure("Horizontal.TProgressbar", troughcolor=SURFACE2, background=ACCENT, borderwidth=0,
                lightcolor=ACCENT, darkcolor=ACCENT)
    return base


def bottom(canvas, item):
    return canvas.bbox(item)[3]


def fit(font, text, width):
    """Recorta `text` con «…» para que quepa en `width` píxeles."""
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


def rounded(canvas, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def bar_text(frac, width=12):
    return "█" * max(1 if frac > 0 else 0, round(frac * width))


# ---------------------------------------------------------------- interfaz: app

class App:
    def __init__(self, root):
        self.root = root
        self.data = None
        self.busy = False
        self.events = queue.Queue()
        self.checked = set()
        self.font = setup_style(root)
        root.title("DeckSpace")
        root.geometry("1180x760")
        root.minsize(820, 540)
        try:
            root.attributes("-zoomed", True)
        except tk.TclError:
            pass

        outer = ttk.Frame(root, padding=(18, 14, 18, 12))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="DeckSpace", style="Title.TLabel").pack(side="left")
        self.subtitle = ttk.Label(head, text="  Espacio en disco de tu Steam Deck", style="Muted.TLabel")
        self.subtitle.pack(side="left", pady=(8, 0))
        self.refresh_btn = ttk.Button(head, text="⟳  Analizar", style="Accent.TButton", command=self.analyze)
        self.refresh_btn.pack(side="right")
        self.progress = ttk.Progressbar(head, mode="indeterminate", length=140)
        self.status = ttk.Label(head, text="", style="Muted.TLabel")
        self.status.pack(side="right", padx=12)

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True, pady=(12, 0))
        self.tab_summary = ttk.Frame(self.tabs)
        self.tab_games = ttk.Frame(self.tabs, padding=14, style="Card.TFrame")
        self.tab_clean = ttk.Frame(self.tabs, padding=14, style="Card.TFrame")
        self.tab_dirs = ttk.Frame(self.tabs, padding=14, style="Card.TFrame")
        self.tabs.add(self.tab_summary, text="  Resumen  ")
        self.tabs.add(self.tab_games, text="  Juegos  ")
        self.tabs.add(self.tab_clean, text="  Limpieza  ")
        self.tabs.add(self.tab_dirs, text="  Carpetas  ")
        self.tabs.bind("<<NotebookTabChanged>>", self.on_tab)

        self.build_summary()
        self.build_games()
        self.build_clean()
        self.build_dirs()
        root.after(100, self.poll)
        root.after(200, self.analyze)

    # ------------------------------------------------------------ resumen
    def build_summary(self):
        self.canvas = tk.Canvas(self.tab_summary, background=BG, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(self.tab_summary, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.schedule_draw())
        self.canvas.bind_all("<Button-4>", lambda e: self.scroll(-1), add="+")
        self.canvas.bind_all("<Button-5>", lambda e: self.scroll(1), add="+")
        self._draw_job = None

    def scroll(self, direction):
        if self.tabs.index("current") == 0:
            self.canvas.yview_scroll(direction * 3, "units")

    def schedule_draw(self):
        if self._draw_job:
            self.root.after_cancel(self._draw_job)
        self._draw_job = self.root.after(60, self.draw_summary)

    def draw_summary(self):
        c, f = self.canvas, self.font
        c.delete("all")
        w = max(c.winfo_width(), 600)
        pad, y = 4, 14
        if not self.data:
            c.create_text(w / 2, 160, text="Analizando tu Deck…", fill=MUTED, font=(f, 16))
            return

        for d in self.data["drives"]:
            y = self.draw_drive(c, d, pad, y, w - pad * 2) + 16

        # Tarjetas de acción
        cw = (w - pad * 2 - 16) / 2
        junk = self.data["orphan_total"]
        stale = [g for g in self.data["games"] if not g["tool"] and self.is_stale(g)]
        stale_size = sum(g["total"] for g in stale)
        cards = [
            ("Espacio recuperable", human(junk) if junk else "Todo limpio",
             f"{len(self.data['junk'])} restos detectados" if junk else "No hay restos que limpiar",
             DANGER if junk > 1024 ** 3 else OK, "Ver limpieza  →", 2),
            ("Juegos sin jugar en 6 meses", human(stale_size) if stale else "Ninguno",
             f"{len(stale)} juegos que podrías desinstalar o mover" if stale else "Juegas a todo lo que tienes",
             WARN if stale else OK, "Ver juegos  →", 1),
        ]
        card_h = 0
        for i, (title, big, sub, color, btn, tab) in enumerate(cards):
            x = pad + i * (cw + 16)
            bg = rounded(c, x, y, x + cw, y + 10, 16, fill=SURFACE, outline="")
            t1 = c.create_text(x + 24, y + 18, anchor="nw", text=title, fill=MUTED, font=(f, 11, "bold"))
            t2 = c.create_text(x + 24, bottom(c, t1) + 2, anchor="nw", text=big, fill=color, font=(f, 26, "bold"))
            t3 = c.create_text(x + 24, bottom(c, t2) + 2, anchor="nw", text=sub, fill=FG, font=(f, 11),
                               width=cw - 48)
            b = ttk.Button(c, text=btn, command=lambda t=tab: self.tabs.select(t))
            bw_ = c.create_window(x + 24, bottom(c, t3) + 12, anchor="nw", window=b)
            self.root.update_idletasks()
            h = bottom(c, bw_) + 18 - y
            card_h = max(card_h, h)
            c.delete(bg)
            self._cards = getattr(self, "_cards", [])
        for i, (_, _, _, color, _, _) in enumerate(cards):
            x = pad + i * (cw + 16)
            c.tag_lower(rounded(c, x, y, x + cw, y + card_h, 16, fill=SURFACE, outline=""))
            c.create_rectangle(x, y + 20, x + 4, y + card_h - 20, fill=color, outline="")
        y += card_h + 16

        # Juegos que más ocupan
        top = sorted((g for g in self.data["games"] if not g["tool"]), key=lambda g: -g["total"])[:8]
        if top:
            row_h = 52
            h = 76 + len(top) * row_h
            rounded(c, pad, y, w - pad, y + h, 16, fill=SURFACE, outline="")
            c.create_text(pad + 24, y + 20, anchor="nw", text="Los juegos que más ocupan", fill=FG, font=(f, 13, "bold"))
            lx = pad + 24
            small = tkfont.Font(family=f, size=10)
            tx = w - pad - 24
            for label, color in reversed((("Juego", COLORS["Juegos de Steam"]),
                                          ("Datos de Proton", COLORS["Datos de Proton"]),
                                          ("Shaders", COLORS["Caché de shaders"]))):
                tx -= small.measure(label)
                c.create_text(tx, y + 30, anchor="w", text=label, fill=MUTED, font=small)
                c.create_oval(tx - 18, y + 25, tx - 8, y + 35, fill=color, outline="")
                tx -= 36
            name_font = tkfont.Font(family=f, size=11)
            name_w, size_w = min(360, w * 0.3), 100
            bx, bw = lx + name_w + 16, w - pad - 24 - lx - name_w - 16 - size_w
            mx = top[0]["total"] or 1
            yy = y + 66
            for g in top:
                c.create_text(lx, yy + 4, anchor="nw", text=fit(name_font, g["name"], name_w), fill=FG, font=name_font)
                c.create_text(lx, yy + 26, anchor="nw", text=f"{g['drive']}  ·  {ago(g['last_played'])}",
                              fill=MUTED, font=(f, 9))
                rounded(c, bx, yy + 12, bx + bw, yy + 32, 8, fill=SURFACE2, outline="")
                x0 = bx
                for part, color in ((g["size"], COLORS["Juegos de Steam"]), (g["compat"], COLORS["Datos de Proton"]),
                                    (g["shader"], COLORS["Caché de shaders"])):
                    seg = bw * part / mx
                    if seg >= 2:
                        c.create_rectangle(x0, yy + 12, x0 + seg, yy + 32, fill=color, outline="")
                    x0 += seg
                c.create_text(w - pad - 24, yy + 22, anchor="e", text=human(g["total"]), fill=FG, font=(f, 11, "bold"))
                yy += row_h
            y += h + 16
        c.configure(scrollregion=(0, 0, w, y))

    def draw_drive(self, c, d, x, y, w):
        f = self.font
        legend = [(k, v) for k, v in sorted(d["parts"].items(), key=lambda kv: (kv[0] == "Otros", -kv[1]))
                  if v >= 50 * 1024 ** 2]
        cols = 4 if w > 900 else 3
        rows = (len(legend) + cols - 1) // cols
        card = rounded(c, x, y, x + w, y + 10, 16, fill=SURFACE, outline="")
        t1 = c.create_text(x + 24, y + 18, anchor="nw", text=d["label"], fill=FG, font=(f, 15, "bold"))
        t2 = c.create_text(x + 24, bottom(c, t1) + 2, anchor="nw", text=d["mount"], fill=MUTED, font=(f, 9))
        pct = d["used"] / d["total"] * 100 if d["total"] else 0
        color = DANGER if pct > 90 else WARN if pct > 80 else OK
        t3 = c.create_text(x + w - 24, y + 12, anchor="ne", text=f"{human(d['free'])} libres", fill=color,
                           font=(f, 20, "bold"))
        t4 = c.create_text(x + w - 24, bottom(c, t3), anchor="ne",
                           text=f"{human(d['used'])} usados de {human(d['total'])}  ·  {pct:.0f} % ocupado",
                           fill=MUTED, font=(f, 10))
        bx, by, bw, bh = x + 24, max(bottom(c, t2), bottom(c, t4)) + 14, w - 48, 26
        rounded(c, bx, by, bx + bw, by + bh, 10, fill=FREE_COLOR, outline="")
        x0 = bx
        parts = sorted(d["parts"].items(), key=lambda kv: (kv[0] == "Otros", -kv[1]))
        for i, (cat, size) in enumerate(parts):
            seg = bw * size / d["total"]
            if seg < 1:
                continue
            if i == 0:
                rounded(c, x0, by, x0 + max(seg, 20), by + bh, 10, fill=COLORS.get(cat, MUTED), outline="")
                c.create_rectangle(x0 + 10, by, x0 + seg, by + bh, fill=COLORS.get(cat, MUTED), outline="")
            else:
                c.create_rectangle(x0, by, x0 + seg, by + bh, fill=COLORS.get(cat, MUTED), outline="")
            x0 += seg
        ly = by + bh + 18
        colw = (w - 48) / cols
        for i, (cat, size) in enumerate(legend):
            lx = bx + (i % cols) * colw
            yy = ly + (i // cols) * 30
            c.create_oval(lx, yy + 5, lx + 12, yy + 17, fill=COLORS.get(cat, MUTED), outline="")
            c.create_text(lx + 20, yy + 11, anchor="w", text=cat, fill=MUTED, font=(f, 10))
            c.create_text(lx + colw - 28, yy + 11, anchor="e", text=human(size), fill=FG, font=(f, 10, "bold"))
        h = ly + rows * 30 + 8 - y
        c.delete(card)
        c.tag_lower(rounded(c, x, y, x + w, y + h, 16, fill=SURFACE, outline=""))
        return y + h

    @staticmethod
    def is_stale(g):
        return not g["last_played"] or time.time() - g["last_played"] > STALE_DAYS * 86400

    # ------------------------------------------------------------ juegos
    def build_games(self):
        t = self.tab_games
        bar = ttk.Frame(t, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text="Buscar", style="CardMuted.TLabel").pack(side="left")
        self.gfilter = tk.StringVar()
        self.gfilter.trace_add("write", lambda *_: self.render_games())
        ttk.Entry(bar, textvariable=self.gfilter, width=24).pack(side="left", padx=(8, 16))
        ttk.Label(bar, text="Unidad", style="CardMuted.TLabel").pack(side="left")
        self.gdrive = tk.StringVar(value="Todas")
        cb = ttk.Combobox(bar, textvariable=self.gdrive, values=["Todas", "SSD", "microSD"], width=9, state="readonly")
        cb.pack(side="left", padx=(8, 16))
        cb.bind("<<ComboboxSelected>>", lambda e: self.render_games())
        self.gstale = self.toggle_button(bar, "Solo sin jugar en 6 meses")
        self.gstale.pack(side="left")
        self.gtools = self.toggle_button(bar, "Mostrar Proton y runtimes")
        self.gtools.pack(side="left", padx=8)

        self.games = self.make_tree(t, [("drive", "Unidad", 100, "w"), ("total", "Total", 100, "e"),
                                        ("share", "", 130, "w"), ("size", "Instalación", 115, "e"),
                                        ("compat", "Proton", 105, "e"), ("shader", "Shaders", 105, "e"),
                                        ("last", "Última vez", 160, "w")], "Juego", 240, self.sort_games)
        self.games.tag_configure("tool", foreground=MUTED)
        self.games.bind("<Double-1>", lambda e: self.open_game_folder())
        self.gsort, self.grev = "total", True

        foot = ttk.Frame(t, style="Card.TFrame")
        foot.pack(fill="x", pady=(10, 0))
        info = ttk.Frame(foot, style="Card.TFrame")
        info.pack(side="left")
        self.games_info = ttk.Label(info, style="Card.TLabel", font=(self.font, 11, "bold"))
        self.games_info.pack(anchor="w")
        ttk.Label(info, text="Para mover un juego de unidad: Steam → Ajustes → Almacenamiento",
                  style="CardMuted.TLabel").pack(anchor="w")
        ttk.Button(foot, text="Desinstalar en Steam…", style="Danger.TButton", command=self.uninstall).pack(side="right")
        ttk.Button(foot, text="Abrir carpeta", command=self.open_game_folder).pack(side="right", padx=8)

    def toggle_button(self, parent, text):
        """Botón que se marca/desmarca (más fácil de tocar que una casilla)."""
        btn = ttk.Button(parent, text=f"☐  {text}")
        btn.on = False

        def flip():
            btn.on = not btn.on
            btn.configure(text=f"{'☑' if btn.on else '☐'}  {text}", style="Accent.TButton" if btn.on else "TButton")
            self.render_games()
        btn.configure(command=flip)
        return btn

    def make_tree(self, parent, cols, first, width, on_sort=None):
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        tree = ttk.Treeview(box, columns=[c[0] for c in cols], selectmode="extended")
        tree.heading("#0", text=first, anchor="w", command=(lambda: on_sort("name")) if on_sort else "")
        tree.column("#0", width=width, minwidth=200, stretch=True)
        for c, title, w, anchor in cols:
            tree.heading(c, text=title, anchor=anchor, command=(lambda c=c: on_sort(c)) if on_sort else "")
            tree.column(c, width=w, minwidth=w, stretch=False, anchor=anchor)
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree.tag_configure("odd", background="#19212b")
        fixed = sum(c[2] for c in cols)

        def keep_widths(event):
            # Tk encoge las columnas al crear la tabla; se restauran y la primera ocupa el resto
            for c, _, w, _ in cols:
                tree.column(c, width=w)
            tree.column("#0", width=max(200, event.width - fixed - 4))
        tree.bind("<Configure>", keep_widths)
        return tree

    def sort_games(self, key):
        self.grev = not self.grev if self.gsort == key else key not in ("name", "drive")
        self.gsort = key
        self.render_games()

    def render_games(self):
        if not self.data:
            return
        t = self.games
        t.delete(*t.get_children())
        q = self.gfilter.get().lower().strip()
        key = {"name": lambda g: g["name"].lower(), "drive": lambda g: (g["drive"], -g["total"]),
               "size": lambda g: g["size"], "compat": lambda g: g["compat"], "shader": lambda g: g["shader"],
               "total": lambda g: g["total"], "share": lambda g: g["total"],
               "last": lambda g: g["last_played"]}[self.gsort]
        rows = [g for g in self.data["games"]
                if (self.gtools.on or not g["tool"])
                and (not q or q in g["name"].lower())
                and self.gdrive.get() in ("Todas", g["drive"])
                and (not self.gstale.on or self.is_stale(g))]
        mx = max((g["total"] for g in rows), default=1) or 1
        for i, g in enumerate(sorted(rows, key=key, reverse=self.grev)):
            tags = ["odd"] if i % 2 else []
            if g["tool"]:
                tags.append("tool")
            t.insert("", "end", iid=g["appid"], text=g["name"], tags=tags, values=(
                g["drive"], human(g["total"]), bar_text(g["total"] / mx), human(g["size"]), human(g["compat"]),
                human(g["shader"]), ago(g["last_played"])))
        self.games_info.configure(text=f"{len(rows)} juegos  ·  {human(sum(g['total'] for g in rows))}")

    def selected_games(self):
        by = {g["appid"]: g for g in self.data["games"]} if self.data else {}
        return [by[i] for i in self.games.selection() if i in by]

    def uninstall(self):
        games = self.selected_games()
        if not games:
            return self.dialog("Desinstalar", "Selecciona primero uno o más juegos de la lista.", ok="Entendido",
                               cancel=None)
        names = "\n".join(f"•  {g['name']}  ({human(g['total'])})" for g in games[:12])
        if self.dialog("Desinstalar en Steam", f"Steam abrirá su ventana de desinstalación para:\n\n{names}\n\n"
                       "Las partidas en la nube de Steam se conservan. Cuando termine, pulsa «Analizar».",
                       ok="Abrir en Steam", danger=True):
            for g in games:
                subprocess.Popen(["steam", f"steam://uninstall/{g['appid']}"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

    def open_game_folder(self):
        for g in self.selected_games()[:1]:
            p = os.path.join(g["library"], "common", g["installdir"])
            if os.path.isdir(p):
                subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ------------------------------------------------------------ limpieza
    def build_clean(self):
        t = self.tab_clean
        intro = ttk.Label(t, style="CardMuted.TLabel", wraplength=1200, justify="left",
                          text="Toca una fila para marcarla (☑). Al tocar un grupo se marcan todos sus elementos. "
                               "Nada se borra hasta que pulses «Limpiar» y lo confirmes.")
        intro.pack(fill="x", pady=(0, 10))
        body = ttk.Frame(t, style="Card.TFrame")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        self.junk = self.make_tree(left, [("size", "Tamaño", 110, "e"), ("risk", "Riesgo", 230, "w")],
                                   "Elemento", 460)
        self.junk.configure(selectmode="browse")
        self.junk.tag_configure("group", font=(self.font, 11, "bold"), background=SURFACE2)
        for k, (_, color) in RISK.items():
            self.junk.tag_configure(k, foreground=color)
        self.junk.tag_configure("checked", foreground=FG)
        self.junk.bind("<ButtonRelease-1>", self.on_junk_click)
        self.junk.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
        self.junk.bind("<space>", lambda e: self.toggle_focused())

        side = ttk.Frame(body, style="Card.TFrame", width=330)
        side.pack(side="right", fill="y", padx=(14, 0))
        side.pack_propagate(False)
        ttk.Label(side, text="Detalles", style="H2.TLabel").pack(anchor="w")
        self.detail = tk.Text(side, wrap="word", background=SURFACE, foreground=FG, relief="flat",
                              highlightthickness=0, font=(self.font, 10), height=12, padx=0, pady=8,
                              state="disabled", cursor="arrow")
        self.detail.tag_configure("muted", foreground=MUTED)
        self.detail.tag_configure("big", font=(self.font, 14, "bold"))
        self.detail.pack(fill="both", expand=True)
        ttk.Label(side, text="Registro", style="H2.TLabel").pack(anchor="w", pady=(8, 0))
        self.log = tk.Text(side, wrap="word", background=SURFACE2, foreground=MUTED, relief="flat",
                           highlightthickness=0, font=("monospace", 9), height=9, padx=8, pady=6, state="disabled")
        self.log.pack(fill="x")

        foot = ttk.Frame(t, style="Card.TFrame")
        foot.pack(fill="x", pady=(12, 0))
        self.sel_info = ttk.Label(foot, style="Card.TLabel", font=(self.font, 12, "bold"))
        self.sel_info.pack(side="left")
        self.clean_btn = ttk.Button(foot, text="🧹  Limpiar", style="Danger.TButton", command=self.clean_selected)
        self.clean_btn.pack(side="right")
        ttk.Button(foot, text="Desmarcar todo", command=lambda: self.set_checked(set())).pack(side="right", padx=8)
        ttk.Button(foot, text="Marcar todo", command=lambda: self.set_checked(set(self.junk_items()))).pack(side="right")
        ttk.Button(foot, text="Marcar lo que no tiene riesgo",
                   command=lambda: self.set_checked({i for i, j in self.junk_items().items()
                                                     if GROUPS[j["group"]][1] == "safe"})).pack(side="right", padx=8)

    def junk_items(self):
        return {str(i): j for i, j in enumerate(self.data["junk"])} if self.data else {}

    def render_junk(self):
        t = self.junk
        t.delete(*t.get_children())
        items = self.junk_items()
        if not items:
            t.insert("", "end", iid="empty", text="✔  No hay restos que limpiar. ¡Tu Deck está limpia!")
        for g, (title, risk) in GROUPS.items():
            members = [(i, j) for i, j in items.items() if j["group"] == g]
            if not members:
                continue
            gid = f"group:{g}"
            t.insert("", "end", iid=gid, open=True, tags=("group", risk))
            for n, (i, j) in enumerate(members):
                t.insert(gid, "end", iid=i, values=(human(j["size"]), ""), tags=(["odd"] if n % 2 else []))
        self.refresh_checks()

    def refresh_checks(self):
        t, items = self.junk, self.junk_items()
        for g, (title, risk) in GROUPS.items():
            gid = f"group:{g}"
            if not t.exists(gid):
                continue
            kids = t.get_children(gid)
            n = sum(1 for k in kids if k in self.checked)
            mark = "☑" if n == len(kids) else "◩" if n else "☐"
            size = sum(items[k]["size"] for k in kids)
            t.item(gid, text=f"{mark}  {title}  ({len(kids)})",
                   values=(human(size), RISK[risk][0]))
            for k in kids:
                t.item(k, text=f"{'☑' if k in self.checked else '☐'}  {items[k]['name']}")
        sel = [items[k] for k in self.checked if k in items]
        self.sel_info.configure(text=(f"Marcado: {len(sel)} elementos · {human(sum(j['size'] for j in sel))}"
                                      if sel else "Nada marcado"))
        self.clean_btn.configure(state="normal" if sel and not self.busy else "disabled")

    def set_checked(self, ids):
        self.checked = set(ids)
        self.refresh_checks()

    def on_junk_click(self, event):
        if self.junk.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        row = self.junk.identify_row(event.y)
        if row:
            self.toggle(row)

    def toggle_focused(self):
        row = self.junk.focus()
        if row:
            self.toggle(row)

    def toggle(self, row):
        if row.startswith("group:"):
            kids = set(self.junk.get_children(row))
            self.checked = self.checked - kids if kids <= self.checked else self.checked | kids
        elif row in self.junk_items():
            self.checked ^= {row}
        self.refresh_checks()

    def show_detail(self):
        sel = self.junk.selection()
        items = self.junk_items()
        txt = self.detail
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if sel and sel[0] in items:
            j = items[sel[0]]
            label, _ = RISK[GROUPS[j["group"]][1]]
            txt.insert("end", j["name"] + "\n", "big")
            txt.insert("end", f"{human(j['size'])}   ·   {label}\n\n")
            txt.insert("end", j["note"] + "\n\n")
            txt.insert("end", "Ubicación\n", "muted")
            txt.insert("end", j["path"])
        elif sel and sel[0].startswith("group:"):
            title, risk = GROUPS[sel[0][6:]]
            txt.insert("end", title + "\n", "big")
            txt.insert("end", RISK[risk][0] + "\n\nToca el grupo para marcar o desmarcar todos sus elementos.")
        else:
            txt.insert("end", "Selecciona un elemento para ver qué es y dónde está.", "muted")
        txt.configure(state="disabled")

    def write_log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", f"{dt.datetime.now():%H:%M:%S}  {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clean_selected(self):
        items = self.junk_items()
        sel = [items[k] for k in self.checked if k in items]
        if not sel:
            return
        if any(i["kind"] == "contents" for i in sel) and steam_running():
            if not self.dialog("Steam está abierto", "Borrar los temporales de Steam mientras descarga puede fallar. "
                               "¿Continuar igualmente?", ok="Continuar"):
                return
        total = sum(i["size"] for i in sel)
        by_group = {}
        for i in sel:
            by_group.setdefault(GROUPS[i["group"]][0], []).append(i)
        lines = "\n".join(f"•  {g}: {len(v)} ({human(sum(x['size'] for x in v))})" for g, v in by_group.items())
        msg = f"Se van a liberar unos {human(total)}:\n\n{lines}"
        if any(i["kind"] == "compat" for i in sel):
            msg += f"\n\nLas partidas de los prefijos de Proton se copiarán antes en:\n{BACKUP_ROOT}"
        if any(i["kind"] == "steam_uninstall" for i in sel):
            msg += "\n\nLas versiones oficiales de Proton las desinstala Steam (te pedirá confirmación)."
        if self.dialog("Confirmar limpieza", msg, ok=f"Limpiar {human(total)}", danger=True):
            self.run("Limpiando…", lambda log: clean(sel, log), lambda _: self.after_clean())

    def after_clean(self):
        self.checked.clear()
        self.analyze()

    # ------------------------------------------------------------ carpetas
    def build_dirs(self):
        t = self.tab_dirs
        ttk.Label(t, style="CardMuted.TLabel", text="Explora qué ocupa más espacio. Despliega una carpeta para "
                  "calcular su contenido (puede tardar unos segundos en las grandes).").pack(fill="x", pady=(0, 10))
        self.dirs = self.make_tree(t, [("size", "Tamaño", 110, "e"), ("share", "% de la carpeta", 200, "w")],
                                   "Carpeta", 560)
        self.dirs.configure(selectmode="browse")
        self.dirs.tag_configure("file", foreground=MUTED)
        self.dirs.bind("<<TreeviewOpen>>", self.on_dir_open)
        self.dirs.bind("<Double-1>", lambda e: self.open_dir())
        foot = ttk.Frame(t, style="Card.TFrame")
        foot.pack(fill="x", pady=(10, 0))
        ttk.Button(foot, text="Abrir en el gestor de archivos", command=self.open_dir).pack(side="right")
        self.dirs_ready = False
        self.dir_paths = {}

    def on_tab(self, _e=None):
        if self.tabs.index("current") == 3 and not self.dirs_ready:
            self.dirs_ready = True
            roots = [("Carpeta personal  (~)", HOME)]
            for d in self.data["drives"] if self.data else []:
                if d["mount"].startswith("/run/media"):
                    roots.append(("microSD", d["mount"]))
            for label, p in roots:
                iid = self.dirs.insert("", "end", text=label, values=("…", ""))
                self.dir_paths[iid] = p
                self.dirs.insert(iid, "end", text="Calculando…")

    def on_dir_open(self, _e):
        iid = self.dirs.focus()
        kids = self.dirs.get_children(iid)
        if len(kids) == 1 and self.dirs.item(kids[0], "text") == "Calculando…":
            path = self.dir_paths[iid]

            def work():
                rows = du_children(path)
                self.events.put(("call", lambda: self.fill_dir(iid, rows)))
            threading.Thread(target=work, daemon=True).start()

    def fill_dir(self, iid, rows):
        t = self.dirs
        t.delete(*t.get_children(iid))
        total = sum(r[2] for r in rows) or 1
        t.item(iid, values=(human(total), t.item(iid, "values")[1] if t.parent(iid) else ""))
        shown = [r for r in rows if r[2] >= 1024 ** 2][:60]
        for i, (name, path, size, is_dir) in enumerate(shown):
            tags = (["odd"] if i % 2 else []) + ([] if is_dir else ["file"])
            child = t.insert(iid, "end", text=name + ("/" if is_dir else ""), tags=tags,
                             values=(human(size), f"{bar_text(size / total, 10)}  {size / total * 100:4.1f} %"))
            self.dir_paths[child] = path
            if is_dir:
                t.insert(child, "end", text="Calculando…")
        rest = len(rows) - len(shown)
        if rest > 0:
            t.insert(iid, "end", text=f"…  y {rest} elementos pequeños más", values=("", ""))

    def open_dir(self):
        iid = self.dirs.focus()
        p = self.dir_paths.get(iid)
        if p:
            target = p if os.path.isdir(p) else os.path.dirname(p)
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ------------------------------------------------------------ diálogo propio (tema oscuro)
    def dialog(self, title, text, ok="Aceptar", cancel="Cancelar", danger=False):
        win = tk.Toplevel(self.root, background=SURFACE)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=24, style="Card.TFrame")
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=title, style="H2.TLabel").pack(anchor="w")
        ttk.Label(frm, text=text, style="Card.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=(12, 20))
        result = {"ok": False}
        btns = ttk.Frame(frm, style="Card.TFrame")
        btns.pack(fill="x")

        def close(value):
            result["ok"] = value
            win.destroy()
        okb = ttk.Button(btns, text=ok, style="Danger.TButton" if danger else "Accent.TButton",
                         command=lambda: close(True))
        okb.pack(side="right")
        if cancel:
            ttk.Button(btns, text=cancel, command=lambda: close(False)).pack(side="right", padx=8)
        win.bind("<Escape>", lambda e: close(False))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        okb.focus_set()
        self.root.wait_window(win)
        return result["ok"]

    # ------------------------------------------------------------ trabajo en segundo plano
    def run(self, text, fn, then):
        if self.busy:
            return
        self.busy = True
        self.refresh_btn.configure(state="disabled")
        self.clean_btn.configure(state="disabled")
        self.progress.pack(side="right", padx=(0, 4))
        self.progress.start(12)
        self.status.configure(text=text)

        def worker():
            try:
                self.events.put(("done", then, fn(lambda m: self.events.put(("msg", m)))))
            except Exception as e:  # noqa: BLE001
                self.events.put(("error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "msg":
                    self.status.configure(text=ev[1])
                    self.write_log(ev[1])
                elif ev[0] == "call":
                    ev[1]()
                else:
                    self.busy = False
                    self.refresh_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.pack_forget()
                    self.status.configure(text="")
                    if ev[0] == "done":
                        ev[1](ev[2])
                    else:
                        self.write_log(f"✖ {ev[1]}")
                        self.dialog("Error", ev[1], ok="Cerrar", cancel=None)
                    self.refresh_checks()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def analyze(self):
        def done(data):
            self.data = data
            valid = set(self.junk_items())
            self.checked &= valid
            self.subtitle.configure(text=f"  Último análisis: {dt.datetime.fromtimestamp(data['when']):%H:%M}")
            self.draw_summary()
            self.render_games()
            self.render_junk()
            self.show_detail()
        self.run("Analizando…", analyze, done)


def main():
    import sys
    root = tk.Tk(className="DeckSpace")
    app = App(root)
    if "--tab" in sys.argv:  # abrir directamente en una pestaña (0-3)
        tab = int(sys.argv[sys.argv.index("--tab") + 1])
        root.after(3500, lambda: (app.tabs.select(tab), app.on_tab()))
    root.mainloop()


if __name__ == "__main__":
    main()
