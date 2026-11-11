#!/usr/bin/env python3
"""Genera data/scuole.json (letto da index.html) a partire da data/scuole.csv.

Il CSV non viene mai modificato: lo script lo legge e basta.

Collegamento CSV -> mappa: colonna `provincia` del CSV (sigla) <-> proprietà
`prov_acr` del TopoJSON. Le sigle vengono normalizzate e passate per una
tabella di alias esplicita; ogni rimappatura viene stampata a video.

Uso:
    python3 build_data.py                       # ora di aggiornamento = adesso
    python3 build_data.py --updated "2026-11-11 18:30"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "scuole.csv"
TOPO_PATH = ROOT / "data" / "province.topojson"
OUT_PATH = ROOT / "data" / "scuole.json"
TZ = ZoneInfo("Europe/Rome")

# Sigle ufficiali in uso (ISTAT, 107 unità prima della riforma sarda del 2025).
# Servono solo per segnalare le province della mappa con sigle "nuove".
SIGLE_ISTAT = set("""
AG AL AN AO AP AQ AR AT AV BA BG BI BL BN BO BR BS BT BZ CA CB CE CH CL CN CO CR
CS CT CZ EN FC FE FG FI FM FR GE GO GR IM IS KR LC LE LI LO LT LU MB MC ME MI MN
MO MS MT NA NO NU OR PA PC PD PE PG PI PN PO PR PT PU PV PZ RA RC RE RG RI RM RN
RO SA SI SO SP SR SS SU SV TA TE TN TO TP TR TS TV UD VA VB VC VE VI VR VT VV
""".split())

# Sigle storiche o alternative -> sigla della mappa. Ogni uso viene stampato.
SIGLA_ALIAS = {
    "FO": "FC",   # Forlì (prima di Forlì-Cesena)
    "PS": "PU",   # Pesaro (prima di Pesaro e Urbino)
    "BAT": "BT",  # Barletta-Andria-Trani
    "MZ": "MB",   # abbreviazione non ufficiale di Monza
}

# Nomi (o varianti) di provincia scritti per esteso nella colonna `provincia`.
NOME_ALIAS = {
    "monza e brianza": "MB", "monza e della brianza": "MB", "monza": "MB",
    "barletta andria trani": "BT", "barletta": "BT", "bat": "BT",
    "forli cesena": "FC", "forli": "FC",
    "pesaro e urbino": "PU", "pesaro urbino": "PU", "pesaro": "PU",
    "carbonia iglesias": "CI", "sulcis iglesiente": "CI",
    "medio campidano": "VS", "olbia tempio": "OT", "gallura": "OT",
    "reggio emilia": "RE", "reggio nell emilia": "RE",
    "reggio calabria": "RC", "reggio di calabria": "RC",
    "massa carrara": "MS", "verbano cusio ossola": "VB",
    "bolzano": "BZ", "bozen": "BZ", "aosta": "AO", "valle d aosta": "AO",
}

# Sud Sardegna (SU) non esiste nella mappa: dal 2025 il suo territorio è diviso
# tra Medio Campidano (VS), Sulcis Iglesiente (CI) e la Città metropolitana di
# Cagliari (CA). Si risolve con le coordinate (punto nel poligono); se mancano,
# con questa tabella di comuni. Se nessuna delle due funziona, errore.
SU_COMUNI = {
    "iglesias": "CI", "carbonia": "CI", "sant antioco": "CI", "carloforte": "CI",
    "san giovanni suergiu": "CI", "giba": "CI", "domusnovas": "CI",
    "villacidro": "VS", "sanluri": "VS", "san gavino monreale": "VS",
    "guspini": "VS", "serramanna": "VS", "samassi": "VS", "arbus": "VS",
}
PROVINCE_SOPPRESSE = {"SU": ["VS", "CI", "CA"]}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def parse_num(s: str) -> float | None:
    s = (s or "").strip().replace(",", ".")
    return float(s) if s else None


# ---------------------------------------------------------------- TopoJSON --
def load_topology(path: Path):
    topo = json.loads(path.read_text(encoding="utf-8"))
    obj_name = next(iter(topo["objects"]))
    tr = topo.get("transform")
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for p in arc:
            if tr:
                x += p[0]
                y += p[1]
                pts.append((x * tr["scale"][0] + tr["translate"][0],
                            y * tr["scale"][1] + tr["translate"][1]))
            else:
                pts.append((p[0], p[1]))
        arcs.append(pts)

    def ring(idxs):
        out = []
        for i in idxs:
            pts = arcs[i] if i >= 0 else arcs[~i][::-1]
            out.extend(pts if not out else pts[1:])
        return out

    provinces = {}
    for g in topo["objects"][obj_name]["geometries"]:
        polys = []
        if g["type"] == "Polygon":
            polys = [[ring(r) for r in g["arcs"]]]
        elif g["type"] == "MultiPolygon":
            polys = [[ring(r) for r in p] for p in g["arcs"]]
        provinces[g["properties"]["prov_acr"]] = {"props": g["properties"], "polys": polys}
    return obj_name, provinces


def in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def province_at(lon, lat, provinces, candidates=None):
    for sigla, p in provinces.items():
        if candidates and sigla not in candidates:
            continue
        for poly in p["polys"]:
            if poly and in_ring(lon, lat, poly[0]) and not any(in_ring(lon, lat, h) for h in poly[1:]):
                return sigla
    return None


# -------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--updated", help='ora di ultimo aggiornamento, es. "2026-11-11 18:30" (default: adesso)')
    args = ap.parse_args()

    obj_name, provinces = load_topology(TOPO_PATH)
    name_to_sigla = {norm(p["props"]["prov_name"]): s for s, p in provinces.items()}
    for part_name, s in [(norm(x), s) for s, p in provinces.items() for x in p["props"]["prov_name"].split("/")]:
        name_to_sigla.setdefault(part_name, s)

    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))

    remaps, unmatched, geo_mismatch = [], [], []
    schools = []
    for n, r in enumerate(rows, start=2):  # n = riga nel file (1 = intestazione)
        raw = r["provincia"]
        sig = re.sub(r"[^A-Za-z]", "", raw).upper()
        lat, lon = parse_num(r["latitudine"]), parse_num(r["longitudine"])
        target, why = None, ""
        if sig in provinces:
            target = sig
        elif sig in SIGLA_ALIAS:
            target, why = SIGLA_ALIAS[sig], f"sigla storica {sig}"
        elif sig in PROVINCE_SOPPRESSE:
            cands = PROVINCE_SOPPRESSE[sig]
            if lat is not None and lon is not None:
                target = province_at(lon, lat, provinces, cands)
                why = f"{sig} soppressa, assegnata dalle coordinate"
            if not target and norm(r["comune"]) in SU_COMUNI:
                target = SU_COMUNI[norm(r["comune"])]
                why = f"{sig} soppressa, assegnata dal comune"
        elif norm(raw) in NOME_ALIAS:
            target, why = NOME_ALIAS[norm(raw)], f"nome per esteso '{raw}'"
        elif norm(raw) in name_to_sigla:
            target, why = name_to_sigla[norm(raw)], f"nome per esteso '{raw}'"

        if not target:
            unmatched.append((n, raw, r["scuola"], r["comune"]))
            continue
        if why:
            remaps.append((n, raw, target, why, r["scuola"], r["comune"]))

        if lat is not None and lon is not None:
            found = province_at(lon, lat, provinces)
            if found is None:  # punto in mare per la costa semplificata: cerca intorno
                for d in (0.01, 0.02, 0.04):
                    near = {province_at(lon + dx, lat + dy, provinces)
                            for dx in (-d, 0, d) for dy in (-d, 0, d)} - {None}
                    if near:
                        found = target if target in near else sorted(near)[0]
                        break
            if found != target:
                geo_mismatch.append((n, r["scuola"], r["comune"], target, found or "fuori mappa", r["controllo_geo"]))

        schools.append({
            "scuola": r["scuola"].strip(),
            "comune": r["comune"].strip(),
            "provincia": raw.strip(),
            "prov": target,
            "evacuata": (r["evacuata"].strip().lower() or "nd"),
            "fonte": r["fonte"].strip(),
            "url": r["url"].strip(),
            "controllo_geo": r["controllo_geo"].strip(),
            "lat": lat,
            "lon": lon,
        })

    counts = Counter(s["prov"] for s in schools)
    csv_sigle = {re.sub(r"[^A-Za-z]", "", r["provincia"]).upper() for r in rows}

    # ---------------------------------------------------------- report --
    print(f"TopoJSON: oggetto '{obj_name}', {len(provinces)} province; campo di collegamento: prov_acr")
    print(f"CSV: {len(rows)} righe, {len(csv_sigle)} sigle distinte")
    print("\n1) Sigle del CSV senza una provincia nella mappa (con la stessa sigla):")
    missing = sorted(s for s in csv_sigle if s not in provinces)
    print("   " + (", ".join(missing) if missing else "nessuna"))
    print("\n2) Rimappature applicate (righe del CSV non modificate, solo il collegamento):")
    for n, raw, t, why, sc, co in remaps:
        print(f"   riga {n}: {raw} -> {t} ({provinces[t]['props']['prov_name']}) | {why} | {sc}, {co}")
    if not remaps:
        print("   nessuna")
    print("\n3) Province della mappa senza corrispondenza nell'elenco ISTAT delle sigle"
          " (nuove province sarde 2025, non ricevono sigle dal CSV se non per rimappatura):")
    extra = sorted(s for s in provinces if s not in SIGLE_ISTAT)
    print("   " + (", ".join(f"{s} {provinces[s]['props']['prov_name']}" for s in extra) or "nessuna"))
    print(f"   Sigle ISTAT assenti dalla mappa: {', '.join(sorted(SIGLE_ISTAT - set(provinces))) or 'nessuna'}")
    print(f"\n4) Province della mappa con almeno una scuola: {len(counts)}; senza scuole: {len(provinces) - len(counts)}")
    print("\n5) Coordinate che cadono fuori dalla provincia indicata (solo avviso, nessuna correzione):")
    for n, sc, co, t, found, cg in geo_mismatch:
        print(f"   riga {n}: {sc}, {co} -> provincia {t}, punto in {found}{' [' + cg + ']' if cg else ''}")
    if not geo_mismatch:
        print("   nessuna")

    if unmatched:
        print("\nERRORE: righe senza provincia collegabile. Nessun file scritto.", file=sys.stderr)
        for n, raw, sc, co in unmatched:
            print(f"   riga {n}: provincia '{raw}' | {sc}, {co}", file=sys.stderr)
        sys.exit(1)

    # ---------------------------------------------- classi per quantili --
    vals = sorted(counts.values())

    def q(p):  # quantile "nearest rank" sui conteggi > 0
        return vals[min(len(vals) - 1, max(0, round(p * (len(vals) - 1))))]

    # Il quintile p è il limite SUPERIORE di una classe; la classe successiva
    # parte dal valore dopo. Restituisce i limiti inferiori delle classi 2..5
    # (la classe 1 parte da 1). Quintili uguali riducono il numero di classi.
    breaks = sorted({q(p) + 1 for p in (0.2, 0.4, 0.6, 0.8) if q(p) < vals[-1]})

    total = len(schools)
    assert sum(counts.values()) == total == len(rows), "totali per provincia != righe del CSV"

    updated = (datetime.strptime(args.updated, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
               if args.updated else datetime.now(TZ))
    out = {
        "meta": {
            "updated": updated.isoformat(timespec="minutes"),
            "total": total,
            "province_coinvolte": len(counts),
            "senza_coordinate": sum(1 for s in schools if s["lat"] is None),
            "breaks": breaks,
            "metodo_classi": "quintili dei conteggi delle province con almeno una scuola",
            "join": "CSV.provincia -> TopoJSON.prov_acr",
        },
        "province": sorted(
            ({"sigla": s, "nome": p["props"]["prov_name"], "regione": p["props"]["reg_name"],
              "n": counts.get(s, 0)} for s, p in provinces.items()),
            key=lambda x: x["nome"]),
        "scuole": sorted(schools, key=lambda s: (s["prov"], norm(s["comune"]), norm(s["scuola"]))),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\nOK: {total} scuole in {len(counts)} province (somma per provincia = righe CSV = {len(rows)})")
    lows = [1] + breaks
    print("Classi legenda: " + ", ".join(
        (str(a) if a == b - 1 else f"{a}-{b - 1}") for a, b in zip(lows, breaks)) + f", {breaks[-1]}+")
    print(f"Scritto {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
