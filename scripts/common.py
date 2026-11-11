"""Outils partagés par les scripts de calcul de Sous-Bois."""
import gzip, json, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"
OUT.mkdir(exist_ok=True)

# Ille-et-Vilaine et ses abords (Saint-Malo → Redon, Montfort → Vitré/Fougères)
REGION = {"name": "Ille-et-Vilaine", "west": -2.40, "east": -0.95, "south": 47.55, "north": 48.80}
DLAT = 0.0005      # ≈ 55 m
DLON = 0.00075     # ≈ 55 m à 48° N
W = int(round((REGION["east"] - REGION["west"]) / DLON))
H = int(round((REGION["north"] - REGION["south"]) / DLAT))

S = requests.Session()
S.headers["User-Agent"] = "sous-bois/1.0 (github.com/Velit35/sous-bois)"


class Log:
    def __init__(self, name):
        self.path = OUT / name
        self.lines = []

    def __call__(self, *a):
        line = " ".join(str(x) for x in a)
        print(line, flush=True)
        self.lines.append(line)

    def save(self):
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(url, params=None, timeout=120, tries=4, log=print, ok=(200,), pause=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code in ok:
                return r
            msg = r.text[:300].replace("\n", " ")
            log(f"  HTTP {r.status_code} sur {r.url[:180]} : {msg!r}")
            if r.status_code == 429:
                time.sleep(60)
            elif 400 <= r.status_code < 500 and r.status_code != 408:
                return None
        except Exception as e:
            log(f"  erreur réseau ({e.__class__.__name__}: {str(e)[:200]})")
        time.sleep(pause * (i + 1))
    return None


def write_gz(name, arr, log=print):
    with gzip.open(OUT / name, "wb", compresslevel=9) as f:
        f.write(np.ascontiguousarray(arr, dtype="uint8").tobytes())
    log(f"{name} : {(OUT / name).stat().st_size / 1e6:.2f} Mo")


def read_gz(name, shape):
    p = OUT / name
    if not p.exists():
        return None
    a = np.frombuffer(gzip.open(p).read(), np.uint8)
    return a.reshape(shape) if a.size == shape[0] * shape[1] else None


def transform():
    from rasterio.transform import from_origin
    return from_origin(REGION["west"], REGION["north"], DLON, DLAT)


def cell(lat, lon):
    r = int((REGION["north"] - lat) / DLAT)
    c = int((lon - REGION["west"]) / DLON)
    return (r, c) if 0 <= r < H and 0 <= c < W else None


def metrics(days, t):
    """Mêmes indicateurs que l'app (fonction metrics du fichier index.html)."""
    def val(i, k):
        return days[i].get(k) if 0 <= i < len(days) else None
    r30 = r15 = r7 = 0.0
    for i in range(t - 30, t):
        p = val(i, "p") or 0.0
        r30 += p
        if i >= t - 15:
            r15 += p
        if i >= t - 7:
            r7 += p
    tm = [v for v in (val(i, "tmean") for i in range(t - 6, t + 1)) if v is not None]
    fm = [v for v in (val(i, "tmin") for i in range(t - 5, t + 1)) if v is not None]
    ts_now = [v for v in (val(i, "ts") for i in range(t - 2, t + 1)) if v is not None]
    ts_a = [v for v in (val(i, "ts") for i in range(t - 9, t - 4)) if v is not None]
    ts_b = [v for v in (val(i, "ts") for i in range(t - 3, t + 1)) if v is not None]
    avg = lambda a: sum(a) / len(a) if a else None
    ta, tb = avg(ts_a), avg(ts_b)
    return {"r30": r30, "r15": r15, "r7": r7, "er": r15 + 0.5 * (r30 - r15), "t7": avg(tm),
            "fmin": min(fm) if fm else None, "sm": val(t, "sm"), "ts": avg(ts_now),
            "drop": (ta - tb) if ta is not None and tb is not None else None}


def parse_openmeteo(j, sm_key, ts_key):
    """Réponse Open-Meteo → liste de jours {d, p, tmin, tmax, tmean, sm, ts}."""
    d, h = j["daily"], j.get("hourly", {})
    sm, ts = h.get(sm_key) or [], h.get(ts_key) or []
    out = []
    for i, t in enumerate(d["time"]):
        def day_mean(arr):
            v = [x for x in arr[i * 24:(i + 1) * 24] if x is not None]
            return sum(v) / len(v) if v else None
        tmin, tmax = d["temperature_2m_min"][i], d["temperature_2m_max"][i]
        tmean = (d.get("temperature_2m_mean") or [None] * len(d["time"]))[i]
        if tmean is None and tmin is not None and tmax is not None:
            tmean = (tmin + tmax) / 2
        out.append({"d": t, "p": d["precipitation_sum"][i] or 0.0, "tmin": tmin, "tmax": tmax, "tmean": tmean,
                    "sm": day_mean(sm), "ts": day_mean(ts)})
    return out
