"""Apprend, pour chaque espèce, la météo et les habitats des poussées, à partir des
observations publiques (GBIF : sociétés mycologiques, iNaturalist, Observation.org…).

Météo : pour chaque observation, on compare la météo du jour de l'observation à celle
de jours voisins de la même saison au même endroit (régression logistique).
Habitat : pour les observations précises situées dans la zone, on compare les
peuplements où elles tombent à la surface de chaque peuplement.

Résultat : data/model.json (lu par l'app), data/train-log.txt
"""
import json, os, random, sys, time, traceback
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, REGION, W, H, S, Log, get, now_iso, metrics, parse_openmeteo, read_gz, cell

log = Log("train-log.txt")
random.seed(42)

SPECIES = {"cepe": "Boletus edulis", "girolle": "Cantharellus cibarius", "craterelle": "Craterellus tubaeformis",
           "trompette": "Craterellus cornucopioides", "pdm": "Hydnum repandum"}
BOX = {"lat": (46.6, 49.3), "lon": (-5.3, -0.2)}       # Bretagne, Normandie ouest, Pays de la Loire
PER_SPECIES = 170
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
    while off < 6000:
        r = get("https://api.gbif.org/v1/occurrence/search", {
            "taxonKey": key, "decimalLatitude": f"{BOX['lat'][0]},{BOX['lat'][1]}",
            "decimalLongitude": f"{BOX['lon'][0]},{BOX['lon'][1]}", "hasCoordinate": "true",
            "hasGeospatialIssue": "false", "occurrenceStatus": "PRESENT", "year": "2012,2026",
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
            out.append({"lat": o["decimalLatitude"], "lon": o["decimalLongitude"], "date": d, "unc": unc,
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

def weather_samples(o, last):
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
        return []
    days = parse_openmeteo(r.json(), "soil_moisture_0_to_7cm", "soil_temperature_0_to_7cm")
    idx = {d["d"]: i for i, d in enumerate(days)}
    t0 = idx.get(o["date"].isoformat())
    if t0 is None or t0 < 30:
        return []
    out = [(1, metrics(days, t0))]
    cands = [t for t in range(30, len(days)) if abs(t - t0) >= 8]
    for t in random.sample(cands, min(3, len(cands))):
        out.append((0, metrics(days, t)))
    return out

def vec(m):
    if any(m.get(k) is None for k in ("er", "r7", "t7", "ts", "drop", "fmin")):
        return None
    return [m["er"], m["r7"], m["t7"], m["t7"] ** 2, m["ts"], m["drop"], m["fmin"]]

def fit(X, y, groups):
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold, cross_val_score
    X = np.asarray(X, float); y = np.asarray(y)
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = (X - mu) / sd
    clf = LogisticRegression(C=0.5, class_weight="balanced", max_iter=2000)
    auc = None
    if y.sum() >= 15:
        k = min(5, len(set(groups)))
        auc = float(np.mean(cross_val_score(clf, Z, y, cv=GroupKFold(k), groups=groups, scoring="roc_auc")))
    clf.fit(Z, y)
    return {"feats": FEATS, "mean": mu.round(4).tolist(), "std": sd.round(4).tolist(),
            "coef": clf.coef_[0].round(4).tolist(), "intercept": round(float(clf.intercept_[0]), 4),
            "auc": round(auc, 3) if auc is not None else None}

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
        # tolérance de 2 pixels autour du point (≈ 110 m)
        r, c = rc
        win = cls[max(0, r - 2):r + 3, max(0, c - 2):c + 3]
        vals = win[win > 0]
        if vals.size == 0:
            out_forest += 1; continue
        k = int(np.bincount(vals).argmax())
        n[k] += 1; N += 1
        if rel is not None and rel[r, c]:
            rel_v.append(int(rel[r, c]))
    if N == 0:
        return {"N": 0, "outside": out_forest}
    share_n = (n + 0.5) / (N + 0.5 * NCLS)
    share_a = np.where(area > 0, area / A, np.nan)
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
    # Un même relevé exporté deux fois ne compte qu'une fois
    seen, out = set(), []
    for r in recs:
        k = json.dumps(r, sort_keys=True)
        if k not in seen:
            seen.add(k); out.append(r)
    return out

def load_samples(key):
    p = OUT / "samples" / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else None

def save_samples(key, X, y, g):
    (OUT / "samples").mkdir(exist_ok=True)
    (OUT / "samples" / f"{key}.json").write_text(json.dumps({"generated": now_iso(), "X": [[round(v, 2) for v in x] for x in X], "y": y, "g": g}))

def main():
    log("Entraînement du", now_iso())
    last = date.today() - timedelta(days=7)
    meta = json.loads((OUT / "meta.json").read_text()) if (OUT / "meta.json").exists() else None
    cls = read_gz("terrain.bin.gz", (H, W)) if meta else None
    rel = read_gz("relief.bin.gz", (H, W)) if meta else None
    model = {"version": 2, "generated": now_iso(), "box": BOX, "species": {}}
    # La météo des observations publiques coûte des appels à l'API : on la garde en cache
    # et on ne la retélécharge qu'au calcul mensuel (FULL=1) ou si le cache manque.
    full = os.environ.get("FULL") == "1"
    perso = perso_records()
    log("Calcul", "complet" if full else "rapide (météo publique en cache)", f"— {len(perso)} relevés perso anonymes")
    for key, name in SPECIES.items():
        log(f"\n== {name}")
        tk = taxon_key(name)
        if not tk:
            log("  taxon introuvable"); continue
        occ = dedupe(occurrences(tk))
        months = np.bincount([o["date"].month for o in occ], minlength=13)[1:]
        log(f"  observations datées : {len(occ)} — par mois : {months.tolist()}")
        entry = {"name": name, "taxonKey": tk, "n_obs": len(occ), "months": months.tolist()}
        mine = [r for r in perso if r.get("sp") == key]
        if cls is not None:
            entry["habitat"] = habitat(occ, cls, rel)
            # Tes trouvailles ajoutent leur type de bois (sans position)
            add = [r["cls"] for r in mine if r.get("type") == "trouvaille" and r.get("cls")]
            if add and entry["habitat"].get("N", 0) + len(add) > 0:
                h = entry["habitat"]; n = np.array(h.get("n", [0] * NCLS), float)
                for c in add:
                    if 0 < c < NCLS: n[c] += 1
                area = np.bincount(cls[cls > 0].ravel(), minlength=NCLS).astype(float)
                N = n.sum(); share_n = (n + 0.5) / (N + 0.5 * NCLS); share_a = np.where(area > 0, area / area.sum(), np.nan)
                ratio = share_n / share_a; big = share_a > 0.004
                aff = np.where(np.isfinite(ratio), ratio / np.nanmax(np.where(big, ratio, np.nan)), 0)
                h.update({"N": int(N), "n": n.astype(int).tolist(), "aff": np.clip(aff, 0, 1.2).round(3).tolist(), "perso": len(add)})
            log("  habitat :", {k: v for k, v in entry["habitat"].items() if k != "aff"})
        cache = None if full else load_samples(key)
        if cache:
            X, y, g = cache["X"], cache["y"], cache["g"]
            log(f"  météo publique en cache ({cache['generated'][:10]})")
        else:
            sample = random.sample(occ, min(PER_SPECIES, len(occ)))
            X, y, g = [], [], []
            for gi, o in enumerate(sample):
                for lab, m in weather_samples(o, last):
                    v = vec(m)
                    if v is not None:
                        X.append(v); y.append(lab); g.append(gi)
                time.sleep(0.5)
            save_samples(key, X, y, g)
        n_pub = sum(y)
        # Tes relevés : de vrais « trouvé » et « rien trouvé », comptés triple
        np_, na_ = 0, 0
        for k, r in enumerate(mine):
            v = vec(r.get("m") or {})
            if v is None:
                continue
            lab = 1 if r.get("type") == "trouvaille" or (r.get("bilan") or 0) >= 1 else 0
            for _ in range(3):
                X.append(v); y.append(lab); g.append(100000 + k)
            np_ += lab; na_ += 1 - lab
        log(f"  échantillons météo : {n_pub} poussées publiques ; perso : {np_} réussites, {na_} « rien »")
        if sum(y) >= 25:
            entry["weather"] = fit(X, y, g)
            entry["weather"]["n"] = int(n_pub + np_)
            entry["weather"]["perso"] = {"ok": np_, "rien": na_}
            P = np.array([x for x, l in zip(X, y) if l == 1])
            entry["weather"]["at_finds"] = {f: [round(float(np.percentile(P[:, i], q)), 1) for q in (10, 50, 90)]
                                            for i, f in enumerate(FEATS) if f != "t7sq"}
            log("  modèle météo :", {k: entry["weather"][k] for k in ("auc", "coef", "n")})
            log("  conditions aux poussées (10e / médiane / 90e centile) :", entry["weather"]["at_finds"])
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
