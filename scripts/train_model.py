"""Apprend, pour chaque espèce, la météo et les habitats des poussées.

Données : observations publiques de toute la France métropolitaine (GBIF : sociétés
mycologiques, iNaturalist, Observation.org…) + les fichiers anonymes envoyés depuis l'app
(data/perso/*.json, sans aucune position).

Météo : pour chaque observation, on compare la météo du jour de l'observation à celle de
jours voisins de la même saison au même endroit (régression logistique). Cette météo vient
de l'archive Open-Meteo : elle est gardée en cache (data/samples/) et complétée par paliers,
dans la limite d'un nombre d'appels par jour, pour rester dans le quota gratuit.
Habitat : pour les observations précises situées dans les départements couverts, on compare
les peuplements où elles tombent à la surface de chaque peuplement.

Résultat : data/model.json (lu par l'app), data/train-log.txt
"""
import json, os, random, sys, time, traceback
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, REGION, W, H, S, Log, get, now_iso, metrics, parse_openmeteo, read_gz, cell

log = Log("train-log.txt")

SPECIES = {"cepe": "Boletus edulis", "girolle": "Cantharellus cibarius", "craterelle": "Craterellus tubaeformis",
           "trompette": "Craterellus cornucopioides", "pdm": "Hydnum repandum"}
TARGET = 500                                   # observations avec météo visées par espèce
BUDGET = int(os.environ.get("BUDGET", "330"))  # appels à l'archive météo par calcul (≈ 2 300 « appels » Open-Meteo)
NW = {"lat": (46.6, 49.3), "lon": (-5.3, -0.2)}  # Bretagne et alentours, pour mesurer la fiabilité localement
FEATS = ["er", "r7", "t7", "t7sq", "ts", "drop", "fmin"]
NCLS = 16
HAB_TARGET = 400                                   # observations précises par espèce pour les habitats
HAB_BUDGET = int(os.environ.get("HAB_BUDGET", "1800"))   # lectures de la BD Forêt par calcul
GEOPF_WFS = "https://data.geopf.fr/wfs/ows"

def taxon_key(name):
    r = get("https://api.gbif.org/v1/species/match", {"name": name, "strict": "true"}, log=log)
    if not r:
        return None
    j = r.json()
    return j.get("acceptedUsageKey") or j.get("usageKey")

def occurrences(key):
    out, off = [], 0
    while off < 30000:
        r = get("https://api.gbif.org/v1/occurrence/search", {
            "taxonKey": key, "country": "FR", "decimalLatitude": "41,51.2", "hasCoordinate": "true",
            "hasGeospatialIssue": "false", "occurrenceStatus": "PRESENT", "year": "2010,2026",
            "limit": 300, "offset": off}, log=log, timeout=120)
        if not r:
            break
        j = r.json()
        res = j.get("results", [])
        for o in res:
            ev = str(o.get("eventDate") or "")
            if len(ev) < 10 or "/" in ev[:10]:
                continue
            try:
                d = date.fromisoformat(ev[:10])
            except ValueError:
                continue
            unc = o.get("coordinateUncertaintyInMeters")
            # 10 km suffisent pour la météo (les grilles météo ont cette finesse) : on garde les relevés d'inventaire par maille
            if unc is not None and unc > 10000:
                continue
            out.append({"id": str(o.get("key")), "lat": o["decimalLatitude"], "lon": o["decimalLongitude"], "date": d, "unc": unc,
                        "src": o.get("datasetKey") or "?"})
        off += len(res)
        if j.get("endOfRecords") or not res:
            break
    return out

_titles = {}
def dataset_title(k):
    if k not in _titles:
        r = get(f"https://api.gbif.org/v1/dataset/{k}", log=log, tries=2) if k and k != "?" else None
        _titles[k] = (r.json().get("title") if r is not None else None) or k
    return _titles[k]

def dedupe(occ):
    seen, out = set(), []
    for o in occ:
        k = (round(o["lat"] / 0.02), round(o["lon"] / 0.02), o["date"].isoformat())
        if k not in seen:
            seen.add(k); out.append(o)
    return out

def in_nw(lat, lon):
    return NW["lat"][0] <= lat <= NW["lat"][1] and NW["lon"][0] <= lon <= NW["lon"][1]

def weather_samples(o, last, rng):
    """Indicateurs le jour de l'observation et 3 jours « témoins » de la même saison."""
    start, end = o["date"] - timedelta(days=60), min(o["date"] + timedelta(days=30), last)
    if o["date"] > last:
        return []
    r = get("https://archive-api.open-meteo.com/v1/archive", {
        "latitude": f"{o['lat']:.3f}", "longitude": f"{o['lon']:.3f}", "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "precipitation_sum,temperature_2m_min,temperature_2m_max,temperature_2m_mean",
        "hourly": "soil_moisture_0_to_7cm,soil_temperature_0_to_7cm", "timezone": "Europe/Paris"},
        log=log, timeout=120, tries=4, pause=15)
    if r is None:
        return None                      # échec réseau : on réessaiera au prochain calcul
    days = parse_openmeteo(r.json(), "soil_moisture_0_to_7cm", "soil_temperature_0_to_7cm")
    idx = {d["d"]: i for i, d in enumerate(days)}
    t0 = idx.get(o["date"].isoformat())
    if t0 is None or t0 < 30:
        return []
    out = [(1, metrics(days, t0))]
    cands = [t for t in range(30, len(days)) if abs(t - t0) >= 8]
    for t in rng.sample(cands, min(3, len(cands))):
        out.append((0, metrics(days, t)))
    return out

def vec(m):
    if any(m.get(k) is None for k in ("er", "r7", "t7", "ts", "drop", "fmin")):
        return None
    return [m["er"], m["r7"], m["t7"], m["t7"] ** 2, m["ts"], m["drop"], m["fmin"]]

def fit(X, y, groups, nw_groups):
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold, cross_val_predict
    from sklearn.metrics import roc_auc_score
    X = np.asarray(X, float); y = np.asarray(y); groups = np.asarray(groups)
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = (X - mu) / sd
    clf = LogisticRegression(C=0.5, class_weight="balanced", max_iter=2000)
    auc = auc_nw = None
    if y.sum() >= 15:
        k = min(5, len(set(groups.tolist())))
        pred = cross_val_predict(clf, Z, y, cv=GroupKFold(k), groups=groups, method="predict_proba")[:, 1]
        auc = float(roc_auc_score(y, pred))
        sel = np.isin(groups, list(nw_groups))
        if y[sel].sum() >= 15 and (1 - y[sel]).sum() >= 15:
            auc_nw = float(roc_auc_score(y[sel], pred[sel]))
    clf.fit(Z, y)
    return {"feats": FEATS, "mean": mu.round(4).tolist(), "std": sd.round(4).tolist(),
            "coef": clf.coef_[0].round(4).tolist(), "intercept": round(float(clf.intercept_[0]), 4),
            "auc": round(auc, 3) if auc is not None else None, "auc_nw": round(auc_nw, 3) if auc_nw is not None else None}

def habitat(occ, cls, rel):
    area = np.bincount(cls[cls > 0].ravel(), minlength=NCLS).astype(float)
    A = area.sum()
    n = np.zeros(NCLS); N = 0; out_forest = 0; rel_v = []
    for o in occ:
        if o["unc"] is None or o["unc"] > 200:
            continue
        rc = cell(o["lat"], o["lon"])
        if not rc:
            continue
        r, c = rc
        win = cls[max(0, r - 2):r + 3, max(0, c - 2):c + 3]          # tolérance ≈ 110 m
        vals = win[win > 0]
        if vals.size == 0:
            out_forest += 1; continue
        k = int(np.bincount(vals).argmax())
        n[k] += 1; N += 1
        if rel is not None and rel[r, c]:
            rel_v.append(int(rel[r, c]))
    return finish_habitat(n, N, area, out_forest, rel_v)

def finish_habitat(n, N, area, out_forest=0, rel_v=()):
    if N == 0:
        return {"N": 0, "outside": out_forest}
    share_n = (n + 0.5) / (N + 0.5 * NCLS)
    share_a = np.where(area > 0, area / area.sum(), np.nan)
    ratio = share_n / share_a
    big = share_a > 0.004
    aff = np.where(np.isfinite(ratio), ratio / np.nanmax(np.where(big, ratio, np.nan)), 0)
    res = {"N": int(N), "outside": out_forest, "n": n.astype(int).tolist(), "aff": np.clip(aff, 0, 1.2).round(3).tolist()}
    if rel_v:
        res["relief_mean"] = round(float(np.mean(rel_v)), 1)
    return res

# ---------------------------------------------------------------- habitats appris sur toute la France
def forest_class_at(lat, lon):
    """Peuplement BD Forêt V2 au point (0 = hors forêt cartographiée), None si l'IGN ne répond pas."""
    from build_terrain import classify, pick
    d = 0.00015
    for attempt in range(3):
        r = get(GEOPF_WFS, {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
                            "TYPENAMES": "LANDCOVER.FORESTINVENTORY.V2:formation_vegetale", "OUTPUTFORMAT": "application/json",
                            "COUNT": 3, "SRSNAME": "EPSG:4326", "BBOX": f"{lat - d},{lon - d},{lat + d},{lon + d},urn:ogc:def:crs:EPSG::4326"},
                log=log, timeout=60, tries=2, pause=5)
        if r is None:
            return None
        try:
            feats = r.json().get("features", [])
        except Exception:
            time.sleep(3 * (attempt + 1)); continue
        if not feats:
            return 0
        p = feats[0].get("properties") or {}
        return classify(pick(p, r"tfv", r"lib_tfv", r"libelle.*", r".*tfv.*") or "", pick(p, r"essence", r".*essence.*") or "")
    return None

def random_near(lat, lon, rng, radius_m=2000):
    import math
    a, dist = rng.uniform(0, 2 * math.pi), radius_m * math.sqrt(rng.uniform(0.04, 1))   # entre 400 m et 2 km
    return lat + dist * math.cos(a) / 110540, lon + dist * math.sin(a) / (111320 * math.cos(math.radians(lat)))

def load_hab(key):
    p = OUT / "samples" / f"habitat-{key}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {"items": {}}
    except Exception:
        return {"items": {}}

def habitat_fr(items):
    """Préférence de chaque peuplement : part des observations / part des points témoins voisins (cas-témoins)."""
    K = 14                                    # classes de la BD Forêt (haies et petits bois n'y figurent pas)
    n = np.zeros(K); b = np.zeros(K)
    for it in items.values():
        if it.get("p") is None or not it.get("bg"):
            continue
        n[min(it["p"], K - 1)] += 1
        for c in it["bg"]:
            if c is not None:
                b[min(c, K - 1)] += 0.5           # 2 témoins par observation
    N, B = n[1:].sum(), b[1:].sum()
    if N < 20:
        return {"N": int(N), "B": int(B)}
    ratio = ((n + 1) / (N + K)) / ((b + 1) / (B + K))
    ok = b >= 7.5                         # au moins 15 points témoins dans ce peuplement
    ok[0] = False
    aff = np.where(ok, ratio / ratio[ok].max(), np.nan)
    return {"N": int(N), "B": int(B), "outside": int(n[0]), "n": n.astype(int).tolist(), "b": b.round(1).tolist(),
            "aff": [None if not np.isfinite(v) else round(float(v), 3) for v in aff]}

def perso_records():
    """Fichiers anonymes envoyés depuis l'app (data/perso/*.json) : aucune position, juste météo, bois et bilan."""
    recs = []
    for p in sorted((OUT / "perso").glob("*.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            recs += [r for r in j.get("records", []) if isinstance(r, dict)]
        except Exception as ex:
            log("  fichier perso illisible :", p.name, ex)
    seen, out = set(), []
    for r in recs:
        k = json.dumps(r, sort_keys=True)
        if k not in seen:
            seen.add(k); out.append(r)
    return out

def load_cache(key):
    p = OUT / "samples" / f"{key}.json"
    if p.exists():
        try:
            j = json.loads(p.read_text())
            if j.get("version") == 2:
                return j
        except Exception:
            pass
    return {"version": 2, "items": {}}

def save_cache(key, cache):
    (OUT / "samples").mkdir(exist_ok=True)
    cache["updated"] = now_iso()
    (OUT / "samples" / f"{key}.json").write_text(json.dumps(cache, separators=(",", ":")))

def load_terrain():
    meta = json.loads((OUT / "meta.json").read_text()) if (OUT / "meta.json").exists() else None
    if not meta or meta.get("version", 0) < 3:
        return None, None
    a = read_gz(meta["files"]["a"], (H, W))
    if a is None:
        return None, None
    rel = (a & 15).astype(np.uint8) * 17
    return (a >> 4).astype(np.uint8), (rel if meta.get("has", {}).get("relief") else None)

def main():
    log("Entraînement du", now_iso(), f"— budget de {BUDGET} appels météo")
    last = date.today() - timedelta(days=7)
    cls, rel = load_terrain()
    log("Terrain :", "chargé" if cls is not None else "indisponible (habitats non appris)")
    perso = perso_records()
    log(f"Relevés perso anonymes : {len(perso)}")
    model = {"version": 3, "generated": now_iso(), "scope": "France métropolitaine", "species": {}}

    # 1. Observations et file d'attente de la météo à télécharger
    data = {}
    for key, name in SPECIES.items():
        tk = taxon_key(name)
        if not tk:
            log(f"{name} : taxon introuvable"); continue
        occ = dedupe(occurrences(tk))
        cache = load_cache(key)
        rng = random.Random(key)
        order = sorted(occ, key=lambda o: o["id"]); rng.shuffle(order)
        # Les observations bretonnes passent en premier : ce sont les plus utiles ici
        order.sort(key=lambda o: 0 if in_nw(o["lat"], o["lon"]) else 1)
        target = order[:TARGET]
        todo = [o for o in target if o["id"] not in cache["items"]]
        data[key] = {"tk": tk, "occ": occ, "cache": cache, "todo": todo, "rng": rng}
        srcs = {}
        for o in occ:
            srcs[o["src"]] = srcs.get(o["src"], 0) + 1
        named = {dataset_title(k): v for k, v in srcs.items()}
        fongi = sum(v for k, v in named.items() if any(w in str(k).lower() for w in ("fongi", "mycolog", "inpn", "champignon")))
        log(f"  sources principales : {dict(sorted(named.items(), key=lambda kv: -kv[1])[:8])} — FongiFrance, sociétés mycologiques, INPN : {fongi}")
        months = np.bincount([o["date"].month for o in occ], minlength=13)[1:]
        log(f"{name} : {len(occ)} observations datées en France (dont {sum(in_nw(o['lat'], o['lon']) for o in occ)} dans l'Ouest), "
            f"météo en cache {len(cache['items'])}, à compléter {len(todo)} — par mois {months.tolist()}")

    # 2. Météo téléchargée par paliers, à tour de rôle entre espèces
    calls = 0
    queues = {k: list(v["todo"]) for k, v in data.items()}
    while calls < BUDGET and any(queues.values()):
        for key in list(queues):
            if not queues[key] or calls >= BUDGET:
                continue
            o = queues[key].pop(0)
            res = weather_samples(o, last, data[key]["rng"]); calls += 1
            if res is None:
                continue
            rows = [[lab] + vec(m) for lab, m in res if vec(m) is not None]
            data[key]["cache"]["items"][o["id"]] = {"lat": round(o["lat"], 2), "lon": round(o["lon"], 2), "rows": [[round(v, 2) for v in r] for r in rows]}
            time.sleep(0.4)
    for key, d in data.items():
        save_cache(key, d["cache"])
    log(f"\nAppels à l'archive météo : {calls} ; reste à compléter : {sum(len(q) for q in queues.values())}")

    # 2 bis. Habitats : peuplement à chaque observation précise et à 2 points témoins voisins, par paliers
    hab, hq = {}, {}
    for key, d in data.items():
        hab[key] = load_hab(key)
        precise = [o for o in d["occ"] if o["unc"] is not None and o["unc"] <= 100]
        r2 = random.Random("hab" + key)
        order = sorted(precise, key=lambda o: o["id"]); r2.shuffle(order)
        hq[key] = [o for o in order[:HAB_TARGET] if o["id"] not in hab[key]["items"]]
        log(f"  habitats {SPECIES[key]} : {len(precise)} observations précises (≤ 100 m), en cache {len(hab[key]['items'])}, à lire {len(hq[key])}")
    reads = 0
    while reads < HAB_BUDGET and any(hq.values()):
        for key in list(hq):
            if not hq[key] or reads >= HAB_BUDGET:
                continue
            o = hq[key].pop(0)
            r2 = random.Random(o["id"])
            p = forest_class_at(o["lat"], o["lon"]); reads += 1
            if p is None:
                continue
            bg = [forest_class_at(*random_near(o["lat"], o["lon"], r2)) for _ in range(2)]; reads += 2
            hab[key]["items"][o["id"]] = {"p": p, "bg": bg}
            time.sleep(0.15)
    for key in hab:
        (OUT / "samples").mkdir(exist_ok=True)
        (OUT / "samples" / f"habitat-{key}.json").write_text(json.dumps(hab[key], separators=(",", ":")))
    log(f"Lectures BD Forêt : {reads} ; reste à lire : {sum(len(q) for q in hq.values())} observations")

    # 3. Modèles
    area = np.bincount(cls[cls > 0].ravel(), minlength=NCLS).astype(float) if cls is not None else None
    for key, d in data.items():
        name = SPECIES[key]
        log(f"\n== {name}")
        occ = d["occ"]
        entry = {"name": name, "taxonKey": d["tk"], "n_obs": len(occ), "months": np.bincount([o["date"].month for o in occ], minlength=13)[1:].tolist()}
        mine = [r for r in perso if r.get("sp") == key]
        entry["habitat_fr"] = habitat_fr(hab[key]["items"])
        log("  habitats appris en France :", entry["habitat_fr"])
        if cls is not None:
            h = habitat(occ, cls, rel)
            add = [r["cls"] for r in mine if r.get("type") == "trouvaille" and r.get("cls")]
            if add:
                n = np.array(h.get("n", [0] * NCLS), float)
                for c in add:
                    if 0 < c < NCLS: n[c] += 1
                h = finish_habitat(n, int(n.sum()), area, h.get("outside", 0)); h["perso"] = len(add)
            entry["habitat"] = h
            log("  habitat :", {k: v for k, v in h.items() if k != "aff"})
        X, y, g, nw = [], [], [], set()
        for gi, (oid, it) in enumerate(d["cache"]["items"].items()):
            for row in it["rows"]:
                X.append(row[1:]); y.append(int(row[0])); g.append(gi)
            if in_nw(it["lat"], it["lon"]):
                nw.add(gi)
        n_pub = sum(y)
        np_, na_ = 0, 0
        for k, r in enumerate(mine):
            v = vec(r.get("m") or {})
            if v is None:
                continue
            # « déjà ramassé » : les champignons étaient là, la météo était bonne
            lab = 1 if r.get("type") == "trouvaille" or (r.get("bilan") or 0) >= 1 or r.get("ramasse") else 0
            gid = 1_000_000 + k
            for _ in range(3):                      # tes relevés comptent triple
                X.append(v); y.append(lab); g.append(gid)
            nw.add(gid); np_ += lab; na_ += 1 - lab
        log(f"  échantillons : {n_pub} poussées publiques ; perso : {np_} réussites, {na_} « rien »")
        if sum(y) >= 25:
            w = fit(X, y, g, nw)
            w.update({"n": int(n_pub + np_), "perso": {"ok": np_, "rien": na_}})
            P = np.array([x for x, l in zip(X, y) if l == 1])
            w["at_finds"] = {f: [round(float(np.percentile(P[:, i], q)), 1) for q in (10, 50, 90)] for i, f in enumerate(FEATS) if f != "t7sq"}
            entry["weather"] = w
            log("  modèle météo :", {k: w[k] for k in ("auc", "auc_nw", "n")})
            log("  conditions aux poussées (10e / médiane / 90e centile) :", w["at_finds"])
        model["species"][key] = entry
    (OUT / "model.json").write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")
    log("\nTerminé", now_iso())
    log.save()
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("ÉCHEC :", traceback.format_exc()); log.save(); sys.exit(1)
