"""Météo fine de Sous-Bois, recalculée chaque nuit sur GitHub.

Quadrillage d'environ 4 km sur la zone ; pour chaque nœud et pour aujourd'hui → J+3,
les indicateurs utilisés par l'app (pluie 7/15/30 j, température, humidité et température
du sol, choc thermique, gel). Source : Open-Meteo (meilleurs modèles disponibles, AROME
pour la France). Résultat : data/meteo.json
"""
import json, sys, time, traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, REGION, Log, get, now_iso, metrics, parse_openmeteo

log = Log("meteo-log.txt")
DLON, DLAT = 0.05, 0.033          # ≈ 3,7 km
PAST, FC = 30, 4
FIELDS = ["er", "r7", "r15", "r30", "t7", "fmin", "sm", "ts", "drop"]

def nodes():
    """Nœuds du quadrillage situés dans les départements (data/depts.json, préparé par le calcul du terrain)."""
    from shapely.geometry import shape, Point
    p = OUT / "depts.json"
    geom = shape(json.loads(p.read_text())["geometry"]).buffer(0.03) if p.exists() else None
    pad = 0.06
    out = []
    i0, i1 = int((REGION["west"] - pad) // DLON), int((REGION["east"] + pad) // DLON) + 1
    j0, j1 = int((REGION["south"] - pad) // DLAT), int((REGION["north"] + pad) // DLAT) + 1
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            la, lo = round(j * DLAT, 5), round(i * DLON, 5)
            if geom is None or geom.contains(Point(lo, la)):
                out.append((i, j, la, lo))
    return out

def fetch(batch):
    p = {"latitude": ",".join(f"{b[2]:.4f}" for b in batch), "longitude": ",".join(f"{b[3]:.4f}" for b in batch),
         "daily": "precipitation_sum,temperature_2m_min,temperature_2m_max,temperature_2m_mean",
         "hourly": "soil_moisture_3_to_9cm,soil_temperature_6cm",
         "past_days": PAST, "forecast_days": FC, "timezone": "Europe/Paris"}
    r = get("https://api.open-meteo.com/v1/forecast", p, timeout=180, log=log, tries=5, pause=20)
    if r is None:
        return None
    j = r.json()
    return j if isinstance(j, list) else [j]

def rnd(v, k):
    return None if v is None else (round(v, 3) if k == "sm" else round(v, 1))

def main():
    today = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    if not (OUT / "depts.json").exists():
        log("Contour des départements pas encore prêt : météo précédente conservée."); log.save(); return 1
    ns = nodes()
    log("Météo du", now_iso(), "—", len(ns), "nœuds")
    rows, dates, fails = [], None, 0
    for k in range(0, len(ns), 100):
        batch = ns[k:k + 100]
        res = fetch(batch)
        if res is None:
            fails += 1; continue
        for (i, j, la, lo), jj in zip(batch, res):
            try:
                days = parse_openmeteo(jj, "soil_moisture_3_to_9cm", "soil_temperature_6cm")
            except Exception:
                continue
            if dates is None:
                dates = [d["d"] for d in days[PAST:PAST + FC]]
            vals = []
            for d in range(FC):
                m = metrics(days, PAST + d)
                vals += [rnd(m[f], f) for f in FIELDS]
            rows.append([i, j, round(jj.get("elevation") or 0)] + vals)
        time.sleep(2)
    log(f"Nœuds obtenus : {len(rows)}/{len(ns)}, lots en échec : {fails}")
    if len(rows) < len(ns) * 0.8:
        log("Trop de manques : fichier précédent conservé."); log.save(); return 1
    if dates and dates[0] != today:
        log("Attention : premier jour", dates[0], "≠", today)
    out = {"version": 1, "generated": now_iso(), "date": dates[0] if dates else today, "days": dates,
           "grid": {"dlon": DLON, "dlat": DLAT}, "fields": FIELDS, "nodes": rows}
    (OUT / "meteo.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    log(f"meteo.json : {(OUT / 'meteo.json').stat().st_size / 1e3:.0f} ko")
    s = [r for r in rows if r[3] is not None]
    if s:
        log("Exemple (premier nœud, aujourd'hui) :", dict(zip(FIELDS, rows[len(rows) // 2][3:3 + len(FIELDS)])))
    log.save()
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("ÉCHEC :", traceback.format_exc()); log.save(); sys.exit(1)
