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
            if unc is not None and unc > 5000:
                continue
            out.append({"id": str(o.get("key")), "lat": o["decimalLatitude"], "lon": o["decimalLongitude"], "date": d, "unc": unc,
                        "src": o.get("datasetName") or o.get("publisher") or o.get("institutionCode") or "?"})
        off += len(res)
        if j.get("endOfRecords") or not res:
            break
    return out

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

    # 3. Modèles
    area = np.bincount(cls[cls > 0].ravel(), minlength=NCLS).astype(float) if cls is not None else None
    for key, d in data.items():
        name = SPECIES[key]
        log(f"\n== {name}")
        occ = d["occ"]
        entry = {"name": name, "taxonKey": d["tk"], "n_obs": len(occ), "months": np.bincount([o["date"].month for o in occ], minlength=13)[1:].tolist()}
        mine = [r for r in perso if r.get("sp") == key]
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
            lab = 1 if r.get("type") == "trouvaille" or (r.get("bilan") or 0) >= 1 else 0
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
