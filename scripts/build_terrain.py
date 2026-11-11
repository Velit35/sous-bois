"""Prépare les couches de terrain de Sous-Bois (calcul lancé sur GitHub).

Toutes les couches partagent la même grille (≈ 55 m) sur l'Ille-et-Vilaine et ses abords.

  terrain.bin.gz  peuplement (0 = rien) — IGN BD Forêt V2 + BD TOPO (haies, petits bois),
                  zones bâties et parcs retirés (OpenStreetMap)
  relief.bin.gz   humidité due au relief, 1 (sec) → 255 (fond humide), 0 = inconnu
  loss.bin.gz     année de la dernière coupe ou trouée (année − 2000), 0 = aucune (Hansen / UMD)
  pub.bin.gz      1 = forêt publique (ONF), 0 = privé ou inconnu
  ph.bin.gz       pH × 10 en surface (ISRIC SoilGrids), grille plus grossière
  meta.json       description des fichiers
  build-log.txt   journal
"""
import io, json, math, os, re, sys, tempfile, time, traceback, unicodedata
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, REGION, DLAT, DLON, W, H, S, Log, get, now_iso, write_gz, read_gz, transform, cell

log = Log("build-log.txt")
PH_DLAT, PH_DLON = 0.0025, 0.00375
GEOPF_WFS = "https://data.geopf.fr/wfs/ows"

CLASSES = [
    "Hors forêt", "Chênes", "Hêtre", "Châtaignier", "Autres feuillus", "Mélange de feuillus",
    "Pins", "Douglas", "Épicéa / sapin", "Autres conifères", "Mixte feuillus-conifères",
    "Peupleraie", "Forêt ouverte", "Lande", "Haie / talus boisé", "Petit bois",
]

def norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s)

def classify(tfv, essence=""):
    t = norm(tfv) + " | " + norm(essence)
    if "peupleraie" in t: return 11
    if "lande" in t or "herbace" in t: return 13
    ouverte = "ouverte" in t
    if "sans couvert" in t: return 12
    if "autre que pin" in t: return 9
    if "mixte" in t or ("conifere" in t and "feuillu" in t): c = 10
    elif "chene" in t: c = 1
    elif "hetre" in t: c = 2
    elif "chataignier" in t: c = 3
    elif "douglas" in t: c = 7
    elif "epicea" in t or "sapin" in t: c = 8
    elif re.search(r"\bpins?\b", t): c = 6
    elif "conifere" in t or "meleze" in t or "cedre" in t: c = 9
    elif "melange" in t and "feuillu" in t: c = 5
    elif "feuillu" in t or "robinier" in t or "frene" in t or "bouleau" in t or "charme" in t: c = 4
    elif ouverte: return 12
    else: return 4
    return 12 if ouverte and c in (4, 5, 9, 10) else c


# ------------------------------------------------------------------ outils WFS
_caps = {}
def wfs_names():
    if "wfs" not in _caps:
        r = get(GEOPF_WFS, {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetCapabilities"}, timeout=240, log=log)
        _caps["wfs"] = re.findall(r"<(?:wfs:)?Name>([^<]+)</(?:wfs:)?Name>", r.text) if r else []
        log(f"WFS : {len(_caps['wfs'])} couches")
    return _caps["wfs"]

def wfs_tiles(typename, step=0.25, cql=None, count=5000):
    """Parcourt la région par tuiles et renvoie les objets GeoJSON tuile par tuile."""
    lon = REGION["west"]
    while lon < REGION["east"] - 1e-9:
        lat = REGION["south"]
        while lat < REGION["north"] - 1e-9:
            w, s, e, n = lon, lat, min(lon + step, REGION["east"]), min(lat + step, REGION["north"])
            start, got_all = 0, []
            while True:
                p = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": typename,
                     "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:4326", "COUNT": count, "STARTINDEX": start}
                if cql:
                    p["CQL_FILTER"] = cql.format(w=w, s=s, e=e, n=n)
                else:
                    p["BBOX"] = f"{s},{w},{n},{e},urn:ogc:def:crs:EPSG::4326"
                fc = None
                for attempt in range(5):
                    r = get(GEOPF_WFS, p, timeout=300, log=log)
                    if r is None:
                        break
                    try:
                        fc = r.json(); break
                    except Exception:
                        log(f"  réponse illisible (essai {attempt + 1}) : {r.text[:80]!r}")
                        time.sleep(5 * (attempt + 1))
                if fc is None:
                    log(f"  tuile {w:.2f},{s:.2f} abandonnée")
                    break
                got = fc.get("features", [])
                got_all += got
                matched = fc.get("numberMatched") or fc.get("totalFeatures")
                start += len(got)
                if not got or (isinstance(matched, int) and start >= matched) or (not isinstance(matched, int) and len(got) < count):
                    break
            yield (w, s, e, n), got_all
            lat += step
        lon += step

def cql_works(tn, cql):
    for _ in range(3):
        t = get(GEOPF_WFS, {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": tn, "OUTPUTFORMAT": "application/json",
                            "SRSNAME": "EPSG:4326", "COUNT": 5, "CQL_FILTER": cql.format(w=-1.62, s=48.15, e=-1.55, n=48.20)}, log=log, tries=2)
        try:
            return t is not None and len(t.json().get("features", [])) > 0
        except Exception:
            time.sleep(5)
    return False

def pick(props, *pats, avoid=("code", "g11")):
    for p in pats:
        for k in props:
            kl = k.lower()
            if re.fullmatch(p, kl) and not any(a in kl for a in avoid):
                return props[k]
    return None

def swapped_xy(features):
    from shapely.geometry import shape
    for f in features[:30]:
        try:
            c = shape(f["geometry"]).centroid
        except Exception:
            continue
        inside = REGION["west"] - 1 < c.x < REGION["east"] + 1 and REGION["south"] - 1 < c.y < REGION["north"] + 1
        return not inside
    return False

def burn(shapes, out, all_touched=False):
    from rasterio import features as rf
    if shapes:
        rf.rasterize(shapes, out=out, transform=transform(), all_touched=all_touched)


# ------------------------------------------------------------------ 0. sondage des services
def probe():
    """Liste les couches utiles et enregistre des tuiles témoins (pour choisir les fonds de carte)."""
    pdir = OUT / "probe"; pdir.mkdir(exist_ok=True)
    r = get("https://data.geopf.fr/wmts", {"SERVICE": "WMTS", "REQUEST": "GetCapabilities"}, timeout=240, log=log)
    wmts = re.findall(r"<ows:Identifier>([^<]+)</ows:Identifier>", r.text) if r else []
    keys = ("ROAD", "ROUTE", "TRANSPORT", "CHEMIN", "SENTIER", "FORET", "PUBLI", "CADAST", "PARCEL", "ORTHO", "HAIE", "TOPO")
    found = sorted({n for n in wmts if any(k in n.upper() for k in keys)})
    log("WMTS utiles :", found[:120])
    log("WFS utiles :", sorted({n for n in wfs_names() if any(k in n.upper() for k in ("FORET", "PUBLI", "HAIE", "VEGET", "PARCEL", "TRONCON"))})[:80])
    # Tuiles témoins sur la forêt de Rennes (z16)
    z, lat, lon = 16, 48.183, -1.575
    x = int((lon + 180) / 360 * 2 ** z); y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2 ** z)
    for layer, fmt, style in [("TRANSPORTNETWORKS.ROADS", "image/png", "normal"), ("FORETS.PUBLIQUES", "image/png", "FORETS PUBLIQUES"),
                              ("FORETS.PUBLIQUES", "image/png", "normal"), ("CADASTRALPARCELS.PARCELLAIRE_EXPRESS", "image/png", "normal"),
                              ("ORTHOIMAGERY.ORTHOPHOTOS", "image/jpeg", "normal")]:
        if layer not in wmts:
            continue
        rr = get("https://data.geopf.fr/wmts", {"SERVICE": "WMTS", "REQUEST": "GetTile", "VERSION": "1.0.0", "LAYER": layer, "STYLE": style,
                                                "TILEMATRIXSET": "PM", "TILEMATRIX": z, "TILEROW": y, "TILECOL": x, "FORMAT": fmt}, log=log, tries=2)
        if rr is not None and rr.headers.get("content-type", "").startswith("image"):
            (pdir / f"{layer}--{style.replace(' ', '_')}.{fmt.split('/')[1]}").write_bytes(rr.content)
            log(f"  tuile témoin {layer} ({style}) : {len(rr.content)} octets")
    # Styles disponibles pour les couches clés
    if r:
        for layer in ("TRANSPORTNETWORKS.ROADS", "FORETS.PUBLIQUES"):
            m = re.search(r"<ows:Identifier>" + re.escape(layer) + r"</ows:Identifier>(.*?)</Layer>", r.text, re.S)
            if m:
                log(f"  {layer} styles :", re.findall(r"<Style[^>]*>\s*<ows:Title>[^<]*</ows:Title>\s*<ows:Abstract>[^<]*</ows:Abstract>\s*<ows:Identifier>([^<]+)", m.group(1))
                    or re.findall(r"<ows:Identifier>([^<]+)</ows:Identifier>", m.group(1))[:6],
                    "zoom", re.findall(r"<TileMatrix>(\d+)</TileMatrix>", m.group(1))[:1], "→", re.findall(r"<TileMatrix>(\d+)</TileMatrix>", m.group(1))[-1:])


# ------------------------------------------------------------------ 1. BD Forêt
def forests():
    names = wfs_names()
    tn = next((n for n in names if "forestinventory.v2:formation_vegetale" in n.lower()), None) or \
         next((n for n in names if "FORESTINVENTORY.V2" in n.upper()), None)
    log("BD Forêt :", tn)
    if not tn:
        return None
    arr = np.zeros((H, W), np.uint8)
    order = {11: 0, 13: 0, 12: 1, 4: 2, 9: 2, 5: 3, 7: 3, 8: 3, 6: 4, 10: 4, 3: 5, 2: 5, 1: 5}
    tally, labels, n = {}, {}, 0
    from shapely.geometry import shape, mapping
    from shapely.affinity import affine_transform
    swap = None
    for _, feats in wfs_tiles(tn, step=0.25, count=2000):
        if swap is None and feats:
            swap = swapped_xy(feats); log("  inversion lat/lon :", swap)
        shapes = []
        for f in feats:
            p = f.get("properties") or {}
            tfv = pick(p, r"tfv", r"lib_tfv", r"libelle.*", r".*tfv.*") or ""
            ess = pick(p, r"essence", r".*essence.*") or ""
            c = classify(tfv, ess)
            tally[c] = tally.get(c, 0) + 1
            labels.setdefault(f"{tfv} | {ess}", c)
            try:
                g = shape(f["geometry"])
                if swap: g = affine_transform(g, [0, 1, 1, 0, 0, 0])
                shapes.append((mapping(g), c))
            except Exception:
                pass
        shapes.sort(key=lambda s: order.get(s[1], 0))
        burn(shapes, arr)
        n += len(feats)
    log("  polygones :", n, "→", {CLASSES[k]: v for k, v in sorted(tally.items())})
    for k, v in sorted(labels.items(), key=lambda kv: kv[1]):
        log(f"     {CLASSES[v]:<26} ← {k}")
    return arr if n else None


# ------------------------------------------------------------------ 2. BD TOPO : haies et petits bois
def hedges(arr):
    names = wfs_names()
    tn = "BDTOPO_V3:zone_de_vegetation" if "BDTOPO_V3:zone_de_vegetation" in names else None
    log("BD TOPO végétation :", tn)
    if not tn:
        return
    from shapely.geometry import shape, mapping
    from shapely.affinity import affine_transform
    hedge = np.zeros((H, W), np.uint8); wood = np.zeros((H, W), np.uint8)
    natures, n, swap = {}, 0, None
    # Filtre côté serveur ; si refusé, téléchargement complet de la tuile et tri ici
    cql = "nature IN ('Haie','Bois','Forêt fermée de feuillus','Forêt fermée mixte','Forêt fermée de conifères') AND BBOX(geometrie,{w},{s},{e},{n},'EPSG:4326')"
    use_cql = cql_works(tn, cql)
    log("  filtre serveur :", use_cql)
    for _, feats in wfs_tiles(tn, step=0.1, cql=cql if use_cql else None):
        if swap is None and feats:
            swap = swapped_xy(feats); log("  inversion lat/lon :", swap)
        hs, ws = [], []
        for f in feats:
            nat = str((f.get("properties") or {}).get("nature") or "")
            natures[nat] = natures.get(nat, 0) + 1
            target = hs if nat == "Haie" else ws if nat in ("Bois", "Forêt fermée de feuillus", "Forêt fermée mixte", "Forêt fermée de conifères") else None
            if target is None:
                continue
            try:
                g = shape(f["geometry"])
                if swap: g = affine_transform(g, [0, 1, 1, 0, 0, 0])
                target.append((mapping(g), 1))
            except Exception:
                pass
        burn(hs, hedge, all_touched=True); burn(ws, wood)
        n += len(feats)
    log("  objets :", n, "natures :", dict(sorted(natures.items(), key=lambda kv: -kv[1])[:15]))
    free = arr == 0
    arr[free & (wood > 0)] = 15
    arr[(arr == 0) & (hedge > 0)] = 14
    log(f"  ajoutés : petits bois {int((free & (wood > 0)).sum())} px, haies {int(((arr == 14)).sum())} px")


# ------------------------------------------------------------------ 3. zones bâties et parcs (OpenStreetMap)
def overpass_tile(q):
    for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                "https://maps.mail.ru/osm/tools/overpass/api/interpreter"):
        for i in range(2):
            try:
                r = S.post(url, data={"data": q}, timeout=300)
                if r.status_code == 200:
                    return r.json()
                log(f"    Overpass {url.split('/')[2]} : HTTP {r.status_code}")
            except Exception as ex:
                log(f"    Overpass {url.split('/')[2]} : {ex.__class__.__name__}")
            time.sleep(20 * (i + 1))
    return None

def osm_urban(m):
    from shapely.geometry import Polygon, LineString, mapping
    from shapely.ops import polygonize, linemerge, unary_union
    lu = "residential|commercial|industrial|retail|construction|garages|railway|cemetery|education|religious|brownfield|military"
    le = "park|garden|golf_course|pitch|sports_centre|stadium|playground|dog_park"
    am = "school|university|college|hospital|parking"
    step, ok, tot, kinds = 0.25, 0, 0, {}
    lon = REGION["west"]
    while lon < REGION["east"] - 1e-9:
        lat = REGION["south"]
        while lat < REGION["north"] - 1e-9:
            s, w, n, e = lat, lon, min(lat + step, REGION["north"]), min(lon + step, REGION["east"])
            bb = f"({s},{w},{n},{e})"
            q = f"""[out:json][timeout:240];(way["landuse"~"^({lu})$"]{bb};relation["landuse"~"^({lu})$"]{bb};
way["leisure"~"^({le})$"]{bb};relation["leisure"~"^({le})$"]{bb};way["amenity"~"^({am})$"]{bb};);out geom;"""
            tot += 1
            data = overpass_tile(q)
            if data:
                ok += 1
                shapes = []
                for el in data.get("elements", []):
                    tags = el.get("tags", {})
                    kind = tags.get("landuse") or tags.get("leisure") or tags.get("amenity")
                    try:
                        if el["type"] == "way" and len(el.get("geometry", [])) >= 4:
                            pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
                            if pts[0] == pts[-1]:
                                shapes.append((mapping(Polygon(pts)), 1)); kinds[kind] = kinds.get(kind, 0) + 1
                        elif el["type"] == "relation":
                            outers = [LineString([(p["lon"], p["lat"]) for p in mb["geometry"]]) for mb in el.get("members", [])
                                      if mb.get("role") == "outer" and len(mb.get("geometry", [])) >= 2]
                            polys = list(polygonize(linemerge(outers))) if outers else []
                            if polys:
                                shapes.append((mapping(unary_union(polys)), 1)); kinds[kind] = kinds.get(kind, 0) + 1
                    except Exception:
                        pass
                burn(shapes, m)
            time.sleep(3)
            lat += step
        lon += step
    log(f"  OSM : {ok}/{tot} tuiles, polygones", dict(sorted(kinds.items(), key=lambda kv: -kv[1])[:12]))
    return ok, tot

def bdtopo_urban(m):
    from shapely.geometry import shape, mapping
    names = wfs_names(); n_tot, natures = 0, {}
    keep_act = re.compile(r"parc|jardin|square|camping|golf|loisir|zoo|stade|sport|terrain|piscine|ecole|enseignement|college|lycee|universit|hopital|sante|"
                          r"clinique|industri|commerc|activite|cimeti|caserne|militaire|aerodrome|gare|station|dechet|carriere|parking", re.I)
    for tn, test in (("BDTOPO_V3:zone_d_habitation", lambda nat: not re.search(r"ruine", nat, re.I)),
                     ("BDTOPO_V3:zone_d_activite_ou_d_interet", lambda nat: bool(keep_act.search(norm(nat))))):
        if tn not in names:
            log("  absente :", tn); continue
        swap = None
        for _, feats in wfs_tiles(tn, step=0.25):
            if swap is None and feats:
                swap = swapped_xy(feats)
            sh = []
            for f in feats:
                p = f.get("properties") or {}
                nat = f"{p.get('categorie') or ''} {p.get('nature') or ''}".strip()
                natures[nat] = natures.get(nat, 0) + 1
                if not test(nat):
                    continue
                try:
                    g = shape(f["geometry"])
                    if swap:
                        from shapely.affinity import affine_transform
                        g = affine_transform(g, [0, 1, 1, 0, 0, 0])
                    sh.append((mapping(g), 1))
                except Exception:
                    pass
            burn(sh, m); n_tot += len(sh)
    log(f"  BD TOPO : {n_tot} zones retenues ; natures :", dict(sorted(natures.items(), key=lambda kv: -kv[1])[:25]))

def urban_mask():
    m = np.zeros((H, W), np.uint8)
    bdtopo_urban(m)
    ok, tot = osm_urban(m)
    log(f"  masque : {int(m.sum())} px")
    return m if m.sum() else None

def apply_urban(arr, m):
    from scipy.ndimage import uniform_filter
    frac = uniform_filter(m.astype("float32"), size=11)          # part bâtie dans ≈ 600 m
    kill = (arr > 0) & ((m > 0) | (frac >= 0.45))
    log(f"  forêt retirée en zone bâtie ou parc : {int(kill.sum())} px ({kill.sum() / max(1, (arr > 0).sum()) * 100:.1f} %)")
    arr[kill] = 0


# ------------------------------------------------------------------ 4. forêts publiques (ONF)
def public_forests():
    names = wfs_names()
    cands = [n for n in names if "FORETS.PUBLIQUES" in n.upper() or "FORET_PUBLIQUE" in n.upper() or "FORETS_PUBLIQUES" in n.upper()]
    log("Forêts publiques WFS :", cands)
    pub = np.zeros((H, W), np.uint8)
    if cands:
        from shapely.geometry import shape, mapping
        from shapely.affinity import affine_transform
        swap, n = None, 0
        for _, feats in wfs_tiles(cands[0], step=0.5):
            if swap is None and feats:
                swap = swapped_xy(feats)
                log("  exemple :", json.dumps((feats[0].get("properties") or {}), ensure_ascii=False)[:400])
            sh = []
            for f in feats:
                try:
                    g = shape(f["geometry"])
                    if swap: g = affine_transform(g, [0, 1, 1, 0, 0, 0])
                    sh.append((mapping(g), 1))
                except Exception:
                    pass
            burn(sh, pub); n += len(feats)
        log("  forêts publiques :", n, "polygones,", int(pub.sum()), "px")
        if n:
            return pub
    # Repli : image WMS de la couche, découpée sur notre grille
    from PIL import Image
    for url in ("https://data.geopf.fr/wms-v/ows", "https://data.geopf.fr/wms-r/wms"):
        r = get(url, {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities"}, timeout=240, log=log)
        if not r:
            continue
        layers = [l for l in re.findall(r"<Name>([^<]+)</Name>", r.text) if "FORETS.PUBLIQUES" in l.upper()]
        log(f"  WMS {url} :", layers)
        if not layers:
            continue
        step_c, step_r = 1000, 1000
        for r0 in range(0, H, step_r):
            for c0 in range(0, W, step_c):
                hh, ww = min(step_r, H - r0), min(step_c, W - c0)
                n_ = REGION["north"] - r0 * DLAT; s_ = n_ - hh * DLAT
                w_ = REGION["west"] + c0 * DLON; e_ = w_ + ww * DLON
                rr = get(url, {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": layers[0], "STYLES": "",
                               "CRS": "EPSG:4326", "BBOX": f"{s_},{w_},{n_},{e_}", "WIDTH": ww, "HEIGHT": hh,
                               "FORMAT": "image/png", "TRANSPARENT": "TRUE"}, timeout=240, log=log)
                if rr is None or not rr.headers.get("content-type", "").startswith("image"):
                    continue
                im = np.array(Image.open(io.BytesIO(rr.content)).convert("RGBA"))
                pub[r0:r0 + hh, c0:c0 + ww] = (im[..., 3] > 40).astype(np.uint8)
        log("  forêts publiques (image) :", int(pub.sum()), "px")
        if pub.sum():
            return pub
    return None


# ------------------------------------------------------------------ 4 bis. chemins et sentiers (BD TOPO)
PATH_CODES = {"Route empierrée": 1, "Chemin": 2, "Sentier": 3, "Piste cyclable": 4}
def paths():
    from shapely.geometry import shape
    names = wfs_names()
    tn = "BDTOPO_V3:troncon_de_route" if "BDTOPO_V3:troncon_de_route" in names else None
    log("Tronçons :", tn)
    if not tn:
        return None
    pdir = OUT / "paths"; pdir.mkdir(exist_ok=True)
    for old in pdir.glob("*.json"):
        old.unlink()
    cql = "nature IN ('Route empierrée','Chemin','Sentier','Piste cyclable') AND BBOX(geometrie,{w},{s},{e},{n},'EPSG:4326')"
    use_cql = cql_works(tn, cql)
    log("  filtre serveur :", use_cql)
    chunks, natures, swap, n = {}, {}, None, 0
    for _, feats in wfs_tiles(tn, step=0.1, cql=cql if use_cql else None):
        if swap is None and feats:
            swap = swapped_xy(feats)
        for f in feats:
            nat = str((f.get("properties") or {}).get("nature") or "")
            natures[nat] = natures.get(nat, 0) + 1
            code = PATH_CODES.get(nat)
            if not code:
                continue
            try:
                g = shape(f["geometry"]).simplify(0.00003)
            except Exception:
                continue
            lines = list(g.geoms) if g.geom_type.startswith("Multi") else [g]
            for ln in lines:
                cs = list(ln.coords)
                if swap:
                    cs = [(y, x) for x, y, *_ in cs]
                if len(cs) < 2:
                    continue
                mx, my = cs[len(cs) // 2][:2]
                key = f"{math.floor(mx / 0.1)}_{math.floor(my / 0.1)}"
                q = [(round(c[0] * 1e5), round(c[1] * 1e5)) for c in cs]
                enc = [code, q[0][0], q[0][1]]
                for (x0, y0), (x1, y1) in zip(q, q[1:]):
                    enc += [x1 - x0, y1 - y0]
                chunks.setdefault(key, []).append(enc); n += 1
    for key, lines in chunks.items():
        (pdir / f"{key}.json").write_text(json.dumps({"v": 1, "l": lines}, separators=(",", ":")))
    (pdir / "index.json").write_text(json.dumps({"step": 0.1, "codes": PATH_CODES, "tiles": sorted(chunks)}))
    size = sum(p.stat().st_size for p in pdir.glob("*.json"))
    log(f"  {n} tronçons dans {len(chunks)} tuiles, {size / 1e6:.1f} Mo ; natures vues :", dict(sorted(natures.items(), key=lambda kv: -kv[1])[:12]))
    return n or None


# ------------------------------------------------------------------ 5. relief
def dem():
    """Altitude sur notre grille : IGN RGE ALTI (WMS, format BIL) sinon Copernicus GLO-30."""
    out = np.full((H, W), np.nan, np.float32)
    step = 600
    ok = 0
    for r0 in range(0, H, step):
        for c0 in range(0, W, step):
            hh, ww = min(step, H - r0), min(step, W - c0)
            n_ = REGION["north"] - r0 * DLAT; s_ = n_ - hh * DLAT
            w_ = REGION["west"] + c0 * DLON; e_ = w_ + ww * DLON
            r = get("https://data.geopf.fr/wms-r/wms", {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
                    "LAYERS": "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES", "STYLES": "", "CRS": "EPSG:4326",
                    "BBOX": f"{s_},{w_},{n_},{e_}", "WIDTH": ww, "HEIGHT": hh, "FORMAT": "image/x-bil;bits=32"}, timeout=240, log=log, tries=3)
            if r is None or len(r.content) != ww * hh * 4:
                if r is not None:
                    log("  BIL inattendu :", r.headers.get("content-type"), len(r.content), r.content[:120])
                continue
            for dt in ("<f4", ">f4"):
                a = np.frombuffer(r.content, dt).reshape(hh, ww).astype(np.float32)
                good = (a > -5) & (a < 450)
                v = a[good]
                if v.size > a.size * 0.3 and -5 < np.median(v) < 400:
                    a[~good] = np.nan
                    out[r0:r0 + hh, c0:c0 + ww] = a; ok += 1; break
    log(f"  MNT IGN : {ok} tuiles, {np.isfinite(out).mean() * 100:.1f} % couvert")
    if np.isfinite(out).mean() > 0.95:
        return out, "IGN RGE ALTI"
    # Repli Copernicus (modèle de surface 30 m, arbres compris)
    import rasterio
    from rasterio.warp import reproject, Resampling
    filled = 0
    for la in range(int(math.floor(REGION["south"])), int(math.floor(REGION["north"])) + 1):
        for lo in range(int(math.floor(REGION["west"])), int(math.floor(REGION["east"])) + 1):
            name = f"Copernicus_DSM_COG_10_N{la:02d}_00_{'W' if lo < 0 else 'E'}{abs(lo):03d}_00_DEM"
            url = f"/vsicurl/https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
            try:
                with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(url) as src:
                    tmp = np.full((H, W), np.nan, np.float32)
                    reproject(rasterio.band(src, 1), tmp, dst_transform=transform(), dst_crs="EPSG:4326",
                              dst_nodata=np.nan, resampling=Resampling.average)
                    m = np.isnan(out) & np.isfinite(tmp)
                    out[m] = tmp[m]; filled += int(m.sum())
            except Exception as ex:
                log(f"  Copernicus {name} : {str(ex)[:150]}")
    log(f"  Copernicus : {filled} px complétés, couverture {np.isfinite(out).mean() * 100:.1f} %")
    return (out, "Copernicus GLO-30") if np.isfinite(out).mean() > 0.9 else (None, None)

def relief():
    from scipy import ndimage as nd
    z, src = dem()
    if z is None:
        return None, None
    lat_c = (REGION["north"] + REGION["south"]) / 2
    dx, dy = DLON * 111320 * math.cos(math.radians(lat_c)), DLAT * 110540
    mean = np.nanmean(z)
    zf = np.where(np.isfinite(z), z, mean).astype(np.float32)
    zf = nd.gaussian_filter(zf, 0.7)
    gy, gx = np.gradient(zf, dy, dx)          # gy : vers le sud (lignes descendantes)
    slope = np.arctan(np.hypot(gx, gy))
    northness = -gy / (np.hypot(gx, gy) + 1e-6) * np.clip(np.degrees(slope) / 8, 0, 1)   # versant nord = +1
    tpi_s = zf - nd.uniform_filter(zf, 9)     # ≈ 500 m
    tpi_l = zf - nd.uniform_filter(zf, 31)    # ≈ 1,7 km
    z_ = lambda a: (a - np.mean(a)) / (np.std(a) + 1e-6)
    wet = -0.45 * z_(tpi_s) - 0.35 * z_(tpi_l) - 0.25 * z_(np.tan(slope)) + 0.2 * northness
    # Indice topographique d'humidité (accumulation des écoulements), si pysheds est disponible
    try:
        import rasterio
        from pysheds.grid import Grid
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "dem.tif")
            with rasterio.open(p, "w", driver="GTiff", width=W, height=H, count=1, dtype="float32",
                               crs="EPSG:4326", transform=transform(), nodata=-9999) as dst:
                dst.write(zf, 1)
            g = Grid.from_raster(p); d = g.read_raster(p)
            d = g.resolve_flats(g.fill_depressions(g.fill_pits(d)))
            acc = np.asarray(g.accumulation(g.flowdir(d)), dtype=np.float64)
        a = (acc + 1) * dx
        twi = np.log(a / (np.tan(slope) + 0.01))
        wet = 0.55 * z_(twi) + 0.45 * wet
        log("  indice d'humidité : TWI (pysheds) + position dans le relief")
    except Exception as ex:
        log("  pysheds indisponible, position dans le relief seule :", str(ex)[:200])
    rank = wet.ravel().argsort().argsort().reshape(wet.shape)
    out = (1 + rank.astype(np.float64) / rank.size * 254).astype(np.uint8)
    out[~np.isfinite(z)] = 0
    log(f"  relief : altitude {np.nanmin(z):.0f}–{np.nanmax(z):.0f} m ({src})")
    return out, src


# ------------------------------------------------------------------ 6. coupes récentes (Hansen / UMD, Landsat 30 m)
def forest_loss():
    import rasterio
    from rasterio.windows import from_bounds
    for yr, ver in (("2025", "1.13"), ("2024", "1.12"), ("2023", "1.11"), ("2022", "1.10")):
        url = f"/vsicurl/https://storage.googleapis.com/earthenginepartners-hansen/GFC-{yr}-v{ver}/Hansen_GFC-{yr}-v{ver}_lossyear_50N_010W.tif"
        try:
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="4"), rasterio.open(url) as src:
                win = from_bounds(REGION["west"], REGION["north"] - H * DLAT, REGION["west"] + W * DLON, REGION["north"], src.transform)
                a = src.read(1, window=win.round_offsets().round_lengths(), out_shape=(H * 2, W * 3))
            a = a[:H * 2, :W * 3].reshape(H, 2, W, 3)
            cnt = (a > 0).sum(axis=(1, 3))
            last = a.max(axis=(1, 3))
            out = np.where(cnt >= 2, last, 0).astype(np.uint8)
            log(f"  Hansen GFC {yr} : {int((out > 0).sum())} px touchés, dernière année 20{int(out.max()):02d}")
            return out, int(yr)
        except Exception as ex:
            log(f"  Hansen {yr} : {str(ex)[:160]}")
    return None, None


# ------------------------------------------------------------------ 7. pH (SoilGrids)
def soil_ph():
    import rasterio
    pad = 0.05
    params = [("map", "/map/phh2o.map"), ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
              ("COVERAGEID", "phh2o_0-5cm_mean"), ("FORMAT", "image/tiff"),
              ("SUBSET", f"long({REGION['west'] - pad},{REGION['east'] + pad})"),
              ("SUBSET", f"lat({REGION['south'] - pad},{REGION['north'] + pad})"),
              ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
              ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/4326")]
    r = None
    for i in range(4):
        try:
            r = S.get("https://maps.isric.org/mapserv", params=params, timeout=300)
            if r.status_code == 200 and r.content[:2] in (b"II", b"MM"):
                break
            log(f"  SoilGrids HTTP {r.status_code} : {r.text[:200]!r}")
        except Exception as e:
            log(f"  SoilGrids : {e}")
        r = None; time.sleep(10 * (i + 1))
    if r is None:
        return None
    with rasterio.open(io.BytesIO(r.content)) as src:
        band = src.read(1).astype("float32"); nod = src.nodata; tr = src.transform
    Wp = int(round((REGION["east"] - REGION["west"]) / PH_DLON)); Hp = int(round((REGION["north"] - REGION["south"]) / PH_DLAT))
    lons = REGION["west"] + (np.arange(Wp) + 0.5) * PH_DLON; lats = REGION["north"] - (np.arange(Hp) + 0.5) * PH_DLAT
    inv = ~tr
    cols = np.clip(np.floor(np.array([inv * (x, lats[0]) for x in lons])[:, 0]).astype(int), 0, band.shape[1] - 1)
    rows = np.clip(np.floor(np.array([inv * (lons[0], y) for y in lats])[:, 1]).astype(int), 0, band.shape[0] - 1)
    v = band[np.ix_(rows, cols)]
    bad = ~np.isfinite(v) | (v <= 0) | ((v == nod) if nod is not None else False)
    out = np.where(bad, 0, np.clip(np.round(v), 1, 140)).astype("uint8")
    return out, Wp, Hp


# ------------------------------------------------------------------ principal
def stage(name, fn):
    log(f"\n== {name}")
    t = time.time()
    try:
        res = fn()
    except Exception:
        log("  ÉCHEC :", traceback.format_exc()[-1500:]); res = None
    log(f"  ({time.time() - t:.0f} s)")
    return res

def main():
    log("Calcul du", now_iso(), "zone", REGION, f"grille {W}×{H}")
    old = json.loads((OUT / "meta.json").read_text()) if (OUT / "meta.json").exists() else {}
    meta = {"version": 2, "generated": now_iso(), "region": REGION, "classes": CLASSES,
            "grid": {"w": W, "h": H, "dlat": DLAT, "dlon": DLON, "west": REGION["west"], "north": REGION["north"]}, "files": {}}
    stage("Sondage des services", probe)

    arr = stage("Forêts (IGN BD Forêt V2)", forests)
    if arr is None:
        arr = read_gz("terrain-base.bin.gz", (H, W))
        log("  BD Forêt indisponible : reprise du calcul précédent" if arr is not None else "  BD Forêt indisponible")
    else:
        write_gz("terrain-base.bin.gz", arr, log)
    if arr is None:
        log("Rien à faire sans forêts."); log.save(); return 1
    arr = arr.copy()
    stage("Haies et petits bois (IGN BD TOPO)", lambda: hedges(arr))
    m = stage("Zones bâties et parcs (OpenStreetMap)", urban_mask)
    if m is not None:
        write_gz("urban.bin.gz", m, log)
    else:
        m = read_gz("urban.bin.gz", (H, W)); log("  reprise du masque précédent" if m is not None else "  pas de masque urbain")
    if m is not None:
        apply_urban(arr, m)
    write_gz("terrain.bin.gz", arr, log)
    meta["files"]["terrain"] = "terrain.bin.gz"
    meta["forest_share"] = round(float((arr > 0).mean()), 4)
    meta["class_px"] = {CLASSES[k]: int((arr == k).sum()) for k in range(len(CLASSES))}
    log("Pixels par classe :", meta["class_px"])

    def keep(key, fname, res):
        if res is not None:
            write_gz(fname, res, log); meta["files"][key] = fname
        elif old.get("files", {}).get(key) and (OUT / fname).exists():
            meta["files"][key] = fname; log(f"  {key} : version précédente conservée")

    keep("pub", "pub.bin.gz", stage("Forêts publiques (ONF)", public_forests))
    if stage("Chemins et sentiers (IGN BD TOPO)", paths):
        meta["paths"] = "paths/index.json"
    elif old.get("paths") and (OUT / "paths" / "index.json").exists():
        meta["paths"] = old["paths"]
    rel = stage("Relief", relief)
    keep("relief", "relief.bin.gz", rel[0] if rel else None)
    if rel and rel[1]: meta["relief_source"] = rel[1]
    elif old.get("relief_source"): meta["relief_source"] = old["relief_source"]
    loss = stage("Coupes récentes (Hansen / UMD)", forest_loss)
    keep("loss", "loss.bin.gz", loss[0] if loss else None)
    if loss and loss[1]: meta["loss_until"] = loss[1]
    elif old.get("loss_until"): meta["loss_until"] = old["loss_until"]
    ph = stage("pH du sol (SoilGrids)", soil_ph)
    if ph:
        write_gz("ph.bin.gz", ph[0], log)
        meta["ph"] = {"file": "ph.bin.gz", "w": ph[1], "h": ph[2], "dlat": PH_DLAT, "dlon": PH_DLON, "west": REGION["west"], "north": REGION["north"]}
    elif old.get("ph"):
        meta["ph"] = old["ph"]
    # Compatibilité avec l'ancienne version de l'app
    meta["terrain"] = {"file": "terrain.bin.gz", **meta["grid"]}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log("\nTerminé :", now_iso(), "fichiers", meta["files"])
    log.save()
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("ÉCHEC :", traceback.format_exc()); log.save(); sys.exit(1)
