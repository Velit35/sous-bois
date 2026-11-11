"""Apprend, pour chaque espèce, la météo et les habitats des poussées, à partir des
observations publiques (GBIF : sociétés mycologiques, iNaturalist, Observation.org…).

Météo : pour chaque observation, on compare la météo du jour de l'observation à celle
de jours voisins de la même saison au même endroit (régression logistique).
Habitat : pour les observations précises situées dans la zone, on compare les
peuplements où elles tombent à la surface de chaque peuplement.

Résultat : data/model.json (lu par l'app), data/train-log.txt
"""
import json, random, sys, time, traceback
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

def main():
    log("Entraînement du", now_iso())
    last = date.today() - timedelta(days=7)
    meta = json.loads((OUT / "meta.json").read_text()) if (OUT / "meta.json").exists() else None
    cls = read_gz("terrain.bin.gz", (H, W)) if meta else None
    rel = read_gz("relief.bin.gz", (H, W)) if meta else None
    model = {"version": 1, "generated": now_iso(), "box": BOX, "species": {}}
    for key, name in SPECIES.items():
        log(f"\n== {name}")
        tk = taxon_key(name)
        if not tk:
            log("  taxon introuvable"); continue
        occ = dedupe(occurrences(tk))
        months = np.bincount([o["date"].month for o in occ], minlength=13)[1:]
        log(f"  observations datées : {len(occ)} — par mois : {months.tolist()}")
        srcs = {}
        for o in occ:
            srcs[o["src"]] = srcs.get(o["src"], 0) + 1
        log("  sources :", dict(sorted(srcs.items(), key=lambda kv: -kv[1])[:6]))
        entry = {"name": name, "taxonKey": tk, "n_obs": len(occ), "months": months.tolist()}
        if cls is not None:
            entry["habitat"] = habitat(occ, cls, rel)
            log("  habitat :", entry["habitat"])
        sample = random.sample(occ, min(PER_SPECIES, len(occ)))
        X, y, g = [], [], []
        for gi, o in enumerate(sample):
            for lab, m in weather_samples(o, last):
                v = vec(m)
                if v is not None:
                    X.append(v); y.append(lab); g.append(gi)
            time.sleep(0.5)
        log(f"  échantillons météo : {sum(y)} poussées, {len(y) - sum(y)} jours témoins")
        if sum(y) >= 25:
            entry["weather"] = fit(X, y, g)
            entry["weather"]["n"] = int(sum(y))
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
