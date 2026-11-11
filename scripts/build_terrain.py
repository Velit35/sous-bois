"""Prépare les couches de terrain de Sous-Bois.

Télécharge les forêts de l'IGN (BD Forêt V2, service WFS de la Géoplateforme) et le pH
du sol (ISRIC SoilGrids, service WCS), puis les rasterise sur une grille régulière en
latitude/longitude. Les fichiers produits dans data/ sont lus directement par l'app.

  data/terrain.bin.gz   un octet par pixel : classe de peuplement (0 = pas de forêt)
  data/ph.bin.gz        un octet par pixel : pH × 10 (0 = inconnu), grille plus grossière
  data/meta.json        emprise, tailles, classes, statistiques
  data/build-log.txt    journal du dernier calcul
"""
import gzip, io, json, math, re, sys, time, unicodedata, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

OUT = Path(__file__).resolve().parent.parent / "data"
OUT.mkdir(exist_ok=True)
LOG = []

def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.append(line)

def save_log():
    (OUT / "build-log.txt").write_text("\n".join(LOG) + "\n", encoding="utf-8")

# Ille-et-Vilaine et ses abords (Saint-Malo → Redon, Montfort → Vitré/Fougères)
REGION = {"name": "Ille-et-Vilaine", "west": -2.40, "east": -0.95, "south": 47.55, "north": 48.80}
DLAT = 0.0005      # ≈ 55 m
DLON = 0.00075     # ≈ 55 m à 48° N
PH_DLAT, PH_DLON = 0.0025, 0.00375   # ≈ 275 m, proche de la résolution de SoilGrids

CLASSES = [
    "Hors forêt", "Chênes", "Hêtre", "Châtaignier", "Autres feuillus", "Mélange de feuillus",
    "Pins", "Douglas", "Épicéa / sapin", "Autres conifères", "Mixte feuillus-conifères",
    "Peupleraie", "Forêt ouverte", "Lande",
]

def norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s)

def classify(tfv, essence=""):
    t = norm(tfv) + " | " + norm(essence)
    if "peupleraie" in t: return 11
    if "lande" in t or "herbace" in t: return 13
    ouverte = "ouverte" in t
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
    # Une forêt ouverte garde son essence mais pèse moins : on la note à part
    return 12 if ouverte and c in (4, 5, 9, 10) else c

S = requests.Session()
S.headers["User-Agent"] = "sous-bois/1.0 (github.com/Velit35/sous-bois)"

def get(url, params=None, timeout=120, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            log(f"  HTTP {r.status_code} sur {r.url[:200]} : {r.text[:300]!r}")
        except Exception as e:
            log(f"  erreur réseau ({e.__class__.__name__}: {e})")
        time.sleep(3 * (i + 1))
    return None

# ---------------------------------------------------------------- BD Forêt
WFS = "https://data.geopf.fr/wfs/ows"

def find_typename():
    r = get(WFS, {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetCapabilities"}, timeout=180)
    if r is None:
        return None
    names = re.findall(r"<(?:wfs:)?Name>([^<]+)</(?:wfs:)?Name>", r.text)
    cands = [n for n in names if "FORESTINVENTORY" in n.upper() or "BDFORET" in n.upper().replace("_", "")]
    log("Couches forêt trouvées :", cands[:20])
    for pref in ("FORESTINVENTORY.V2:formation_vegetale", "FORESTINVENTORY.V2"):
        for n in cands:
            if pref.lower() in n.lower():
                return n
    return cands[0] if cands else None

def fetch_forest(typename):
    feats = {}
    step = 0.25
    lon = REGION["west"]
    while lon < REGION["east"] - 1e-9:
        lat = REGION["south"]
        while lat < REGION["north"] - 1e-9:
            w, s, e, n = lon, lat, min(lon + step, REGION["east"]), min(lat + step, REGION["north"])
            start = 0
            while True:
                params = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": typename,
                          "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:4326",
                          "BBOX": f"{s},{w},{n},{e},urn:ogc:def:crs:EPSG::4326",
                          "COUNT": 2000, "STARTINDEX": start}
                r = get(WFS, params, timeout=240)
                if r is None:
                    log(f"  cellule {w:.2f},{s:.2f} abandonnée")
                    break
                try:
                    fc = r.json()
                except Exception:
                    log("  réponse non JSON :", r.text[:300])
                    break
                got = fc.get("features", [])
                for f in got:
                    fid = f.get("id") or json.dumps(f.get("properties"), sort_keys=True)[:200] + str(len(feats))
                    feats[fid] = f
                if len(got) < 2000:
                    break
                start += 2000
            lat += step
        log(f"  bande lon {lon:.2f} : {len(feats)} polygones cumulés")
        lon += step
    return list(feats.values())

def pick(props, *pats, avoid=("code", "g11")):
    keys = list(props.keys())
    for p in pats:
        for k in keys:
            kl = k.lower()
            if re.fullmatch(p, kl) and not any(a in kl for a in avoid):
                return props[k]
    return None

def rasterize_forest(features):
    from rasterio import features as rfeatures
    from rasterio.transform import from_origin
    from shapely.geometry import shape, mapping
    from shapely.affinity import affine_transform

    W = int(round((REGION["east"] - REGION["west"]) / DLON))
    H = int(round((REGION["north"] - REGION["south"]) / DLAT))
    tr = from_origin(REGION["west"], REGION["north"], DLON, DLAT)

    if features:
        log("Exemple de propriétés :", json.dumps(features[0].get("properties", {}), ensure_ascii=False)[:600])
    # Ordre des axes : on vérifie sur le premier polygone que l'on est bien en lon/lat
    swap = False
    for f in features[:50]:
        try:
            c = shape(f["geometry"]).centroid
        except Exception:
            continue
        inside = REGION["west"] - 1 < c.x < REGION["east"] + 1 and REGION["south"] - 1 < c.y < REGION["north"] + 1
        swapped = REGION["west"] - 1 < c.y < REGION["east"] + 1 and REGION["south"] - 1 < c.x < REGION["north"] + 1
        if not inside and swapped:
            swap = True
        break
    log("Inversion lat/lon :", swap)

    shapes, tally, labels = [], {}, {}
    for f in features:
        p = f.get("properties") or {}
        tfv = pick(p, r"tfv", r"lib_tfv", r"libelle.*", r".*tfv.*") or ""
        ess = pick(p, r"essence", r".*essence.*") or ""
        c = classify(tfv, ess)
        tally[c] = tally.get(c, 0) + 1
        key = f"{tfv} | {ess}"
        if key not in labels:
            labels[key] = c
        try:
            g = shape(f["geometry"])
            if swap:
                g = affine_transform(g, [0, 1, 1, 0, 0, 0])
            shapes.append((mapping(g), c))
        except Exception:
            pass
    # Les peuplements les plus favorables gagnent quand deux polygones se touchent
    order = {11: 0, 13: 0, 12: 1, 4: 2, 9: 2, 5: 3, 7: 3, 8: 3, 6: 4, 10: 4, 3: 5, 2: 5, 1: 5}
    shapes.sort(key=lambda s: order.get(s[1], 0))
    arr = rfeatures.rasterize(shapes, out_shape=(H, W), transform=tr, fill=0, dtype="uint8", all_touched=False) if shapes else np.zeros((H, W), np.uint8)
    log("Classes (polygones) :", {CLASSES[k]: v for k, v in sorted(tally.items())})
    log("Types rencontrés → classe :")
    for k, v in sorted(labels.items(), key=lambda kv: kv[1]):
        log(f"   {CLASSES[v]:<26} ← {k}")
    return arr, W, H

# ---------------------------------------------------------------- pH (SoilGrids)
def fetch_ph():
    import rasterio
    pad = 0.05
    url = "https://maps.isric.org/mapserv"
    params = [("map", "/map/phh2o.map"), ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
              ("COVERAGEID", "phh2o_0-5cm_mean"), ("FORMAT", "image/tiff"),
              ("SUBSET", f"long({REGION['west'] - pad},{REGION['east'] + pad})"),
              ("SUBSET", f"lat({REGION['south'] - pad},{REGION['north'] + pad})"),
              ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
              ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/4326")]
    r = None
    for i in range(4):
        try:
            r = S.get(url, params=params, timeout=300)
            if r.status_code == 200 and r.content[:2] in (b"II", b"MM"):
                break
            log(f"  SoilGrids HTTP {r.status_code} : {r.text[:300]!r}")
        except Exception as e:
            log(f"  SoilGrids erreur : {e}")
        r = None
        time.sleep(10 * (i + 1))
    if r is None:
        return None
    with rasterio.open(io.BytesIO(r.content)) as src:
        band = src.read(1).astype("float32")
        nod = src.nodata
        tr = src.transform
        log("SoilGrids :", src.width, "×", src.height, "crs", src.crs, "nodata", nod)
    W = int(round((REGION["east"] - REGION["west"]) / PH_DLON))
    H = int(round((REGION["north"] - REGION["south"]) / PH_DLAT))
    lons = REGION["west"] + (np.arange(W) + 0.5) * PH_DLON
    lats = REGION["north"] - (np.arange(H) + 0.5) * PH_DLAT
    inv = ~tr
    cols = np.floor(np.array([inv * (x, lats[0]) for x in lons])[:, 0]).astype(int)
    rows = np.floor(np.array([inv * (lons[0], y) for y in lats])[:, 1]).astype(int)
    cols = np.clip(cols, 0, band.shape[1] - 1)
    rows = np.clip(rows, 0, band.shape[0] - 1)
    v = band[np.ix_(rows, cols)]
    bad = ~np.isfinite(v) | (v <= 0)
    if nod is not None:
        bad |= v == nod
    out = np.where(bad, 0, np.clip(np.round(v), 1, 140)).astype("uint8")   # SoilGrids donne pH × 10
    ok = out[out > 0]
    if ok.size:
        log(f"pH : min {ok.min() / 10:.1f}, médiane {np.median(ok) / 10:.1f}, max {ok.max() / 10:.1f}")
    return out, W, H

# ---------------------------------------------------------------- main
def write_gz(name, arr):
    with gzip.open(OUT / name, "wb", compresslevel=9) as f:
        f.write(np.ascontiguousarray(arr, dtype="uint8").tobytes())
    log(f"{name} : {(OUT / name).stat().st_size / 1e6:.2f} Mo")

def main():
    log("Calcul du", datetime.now(timezone.utc).isoformat(timespec="seconds"), "zone", REGION)
    meta = {"version": 1, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "region": REGION, "classes": CLASSES}
    ok = True
    tn = find_typename()
    log("Couche utilisée :", tn)
    if tn:
        feats = fetch_forest(tn)
        log("Polygones téléchargés :", len(feats))
        if feats:
            arr, W, H = rasterize_forest(feats)
            write_gz("terrain.bin.gz", arr)
            share = (arr > 0).mean()
            log(f"Part de forêt dans la zone : {share * 100:.1f} %")
            meta["terrain"] = {"file": "terrain.bin.gz", "w": W, "h": H, "dlat": DLAT, "dlon": DLON,
                               "west": REGION["west"], "north": REGION["north"], "forest_share": round(float(share), 4)}
        else:
            ok = False
    else:
        ok = False
    try:
        ph = fetch_ph()
    except Exception as e:
        log("pH impossible :", repr(e)); ph = None
    if ph:
        arr, W, H = ph
        write_gz("ph.bin.gz", arr)
        meta["ph"] = {"file": "ph.bin.gz", "w": W, "h": H, "dlat": PH_DLAT, "dlon": PH_DLON,
                      "west": REGION["west"], "north": REGION["north"]}
    if "terrain" in meta:
        (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log("Terminé :", "OK" if ok else "INCOMPLET")
    save_log()
    return 0 if ok else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        log("ÉCHEC :", traceback.format_exc())
        save_log()
        sys.exit(1)
