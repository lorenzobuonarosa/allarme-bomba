#!/usr/bin/env python3
"""Aggiunge a data/scuole.csv le scuole trovate dalla ricerca oraria.

Usato dal task orario di Claude Code. Legge un file JSON con un array di oggetti
{scuola, comune, provincia, evacuata, fonte, url, frase_di_conferma} e:
  - scarta le scuole già presenti (stesso confronto di update_check.py);
  - scarta le righe incomplete o con una sigla di provincia che non è nella mappa;
  - accoda le nuove righe a data/scuole.csv nello stesso formato (; , CRLF);
  - scrive una riga in data/update_log.txt.
Non modifica né cancella le righe esistenti.

Uso:
    python3 add_schools.py nuove.json [--max 40] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from update_check import same_school  # noqa: E402  stessa normalizzazione nome + comune

CSV_PATH = ROOT / "data" / "scuole.csv"
TOPO_PATH = ROOT / "data" / "province.topojson"
LOG_PATH = ROOT / "data" / "update_log.txt"
TZ = ZoneInfo("Europe/Rome")
FIELDS = ["scuola", "comune", "provincia", "evacuata", "latitudine", "longitudine", "fonte", "url",
          "note", "controllo_geo", "nome_trovato_google", "indirizzo_google"]
ALIAS = {"FO": "FC", "PS": "PU", "BAT": "BT", "MZ": "MB"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file")
    ap.add_argument("--max", type=int, default=40, help="massimo di righe aggiunte per esecuzione")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    started = datetime.now(TZ)
    try:
        items = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        if isinstance(items, dict):
            items = items.get("scuole", [])
        if not isinstance(items, list):
            raise ValueError("serve un array JSON")
    except (OSError, ValueError) as e:
        print(f"ERRORE: {e}")
        return 1

    topo = json.loads(TOPO_PATH.read_text(encoding="utf-8"))
    sigle = {g["properties"]["prov_acr"] for g in next(iter(topo["objects"].values()))["geometries"]}

    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        existing = list(csv.DictReader(f, delimiter=";"))
    pool = [(r["scuola"], r["comune"]) for r in existing]

    added, skipped = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        g = lambda k: re.sub(r"\s+", " ", str(it.get(k) or "")).strip()  # noqa: E731
        scuola, comune, url = g("scuola"), g("comune"), g("url")
        prov = re.sub(r"[^A-Za-z]", "", g("provincia")).upper()
        prov = ALIAS.get(prov, prov)
        ev = g("evacuata").lower().replace("sì", "si")
        ev = ev if ev in ("si", "no", "nd") else "nd"
        label = f"{scuola or '?'} ({comune or '?'})"
        if not scuola or not comune or not url.startswith("http"):
            skipped.append(f"{label}: incompleta"); continue
        if prov not in sigle:
            skipped.append(f"{label}: provincia '{prov}' non nella mappa"); continue
        dup = next(((n, c) for n, c in pool if same_school(scuola, comune, n, c) >= 0.85), None)
        if dup:
            skipped.append(f"{label}: già presente come {dup[0]}"); continue
        if len(added) >= args.max:
            skipped.append(f"{label}: oltre il limite di {args.max} per ora"); continue
        frase = " ".join(g("frase_di_conferma").split()[:15])
        added.append({
            "scuola": scuola, "comune": comune, "provincia": prov, "evacuata": ev,
            "latitudine": "", "longitudine": "", "fonte": g("fonte"), "url": url,
            "note": f"aggiunta automatica {started:%d/%m/%Y %H:%M}" + (f"; fonte: \"{frase}\"" if frase else ""),
            "controllo_geo": "non trovato", "nome_trovato_google": "", "indirizzo_google": "",
        })
        pool.append((scuola, comune))

    if added and not args.dry_run:
        buf = io.StringIO()
        csv.DictWriter(buf, fieldnames=FIELDS, delimiter=";", lineterminator="\r\n").writerows(added)
        raw = CSV_PATH.read_bytes()
        with CSV_PATH.open("ab") as f:
            if raw and not raw.endswith(b"\n"):
                f.write(b"\r\n")
            f.write(buf.getvalue().encode("utf-8"))

    line = (f"{started.isoformat(timespec='minutes')} | task orario | proposte={len(items)} | "
            f"aggiunte={len(added)} | scartate={len(skipped)}"
            + (" | aggiunte: " + "; ".join(f"{r['scuola']} ({r['comune']}, {r['provincia']})" for r in added) if added else "")
            + (" | scartate: " + "; ".join(skipped) if skipped else ""))
    if not args.dry_run:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
