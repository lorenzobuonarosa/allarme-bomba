#!/usr/bin/env python3
"""Cerca online nuove scuole che hanno ricevuto la mail minatoria dell'11 novembre 2026.

Non modifica mai data/scuole.csv. Le scuole non ancora presenti finiscono in
data/candidate.csv per la revisione manuale; ogni esecuzione aggiunge una riga
a data/update_log.txt.

Richiede: pip install "anthropic>=2.8,<3" e la variabile ANTHROPIC_API_KEY.

Variabili d'ambiente opzionali:
    UPDATE_MODEL                modello (default claude-opus-5-5)
    UPDATE_EFFORT               low | medium | high (default medium)
    UPDATE_MAX_SEARCHES         ricerche web per esecuzione (default 6)
    UPDATE_MAX_CALLS_PER_HOUR   tetto di chiamate API nell'ultima ora (default 4)
    ISSUE_BODY_PATH             se impostata, scrive lì il testo della issue
    GITHUB_OUTPUT               (GitHub Actions) riceve new_candidates=N
"""

from __future__ import annotations

import csv
import difflib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SCUOLE_CSV = DATA / "scuole.csv"
CANDIDATE_CSV = DATA / "candidate.csv"
LOG_PATH = DATA / "update_log.txt"
STATE_PATH = DATA / "update_state.json"
TOPO_PATH = DATA / "province.topojson"
TZ = ZoneInfo("Europe/Rome")

MODEL = os.environ.get("UPDATE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("UPDATE_EFFORT", "medium")
MAX_SEARCHES = int(os.environ.get("UPDATE_MAX_SEARCHES", "6"))
MAX_CALLS_PER_HOUR = int(os.environ.get("UPDATE_MAX_CALLS_PER_HOUR", "4"))
MAX_CONTINUATIONS = 2  # riprese dopo pause_turn, contano nel tetto di chiamate

# $ per milione di token (input, output) e $ per ricerca web; servono solo per la stima nel log
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
PRICE_PER_SEARCH = 0.01

# Ogni ora la ricerca si concentra su un gruppo di regioni, a rotazione.
REGION_GROUPS = [
    ["Lombardia", "Piemonte", "Valle d'Aosta", "Liguria"],
    ["Veneto", "Friuli-Venezia Giulia", "Trentino-Alto Adige"],
    ["Emilia-Romagna", "Toscana", "Marche", "Umbria"],
    ["Lazio", "Abruzzo", "Molise", "Campania"],
    ["Puglia", "Basilicata", "Calabria", "Sicilia", "Sardegna"],
]

CANDIDATE_FIELDS = [
    "rilevato_il", "scuola", "comune", "provincia", "evacuata", "fonte", "url",
    "frase_di_conferma", "url_nei_risultati_di_ricerca", "possibile_duplicato_di", "regioni_cercate",
]

SYSTEM_PROMPT = """Sei un assistente di ricerca per una redazione giornalistica italiana.
Cerchi sul web articoli di giornali locali e nazionali italiani che nominano scuole
italiane che hanno ricevuto la mail minatoria (falso allarme bomba) dell'11 novembre 2026,
firmata "Mikhail Andropov, Vilayat di Misoginistan".

Regole:
- Considera solo articoli che parlano della mail dell'11 novembre 2026. Escludi le ondate
  del 19 ottobre 2026 e del 2 novembre 2026, e qualsiasi altra data.
- Considera solo scuole citate per nome (o in modo univoco) nell'articolo. Non inventare
  nomi, comuni o URL: ogni URL deve essere quello di un articolo che hai trovato.
- "evacuata": "si" se l'articolo dice che la scuola è stata evacuata, "no" se dice che non
  lo è stata, "nd" se non lo dice. Ricevere la mail non significa essere evacuati.
- "provincia": sigla automobilistica di 2 lettere (es. RM, MI, BT, FC, PU, MB).
- "frase_di_conferma": breve citazione o parafrasi dall'articolo, meno di 15 parole.
- Rispondi alla fine con SOLO un array JSON, senza testo prima o dopo, senza markdown.
  Ogni elemento: {"scuola", "comune", "provincia", "evacuata", "fonte", "url", "frase_di_conferma"}.
  Se non trovi nulla di nuovo, rispondi [].
"""


# ------------------------------------------------------------------ utility --
def now() -> datetime:
    return datetime.now(TZ)


GENERIC = {
    "istituto", "ist", "comprensivo", "ic", "i", "c", "scuola", "scuole", "statale", "liceo",
    "plesso", "sede", "succursale", "primaria", "secondaria", "di", "del", "della", "dei", "degli",
    "primo", "secondo", "grado", "media", "elementare", "infanzia", "materna", "superiore",
    "iis", "iiss", "isis", "itis", "itc", "itcg", "ipsia", "ite", "iti", "cpia", "omnicomprensivo",
    "tecnico", "professionale", "classico", "scientifico", "artistico", "linguistico", "e", "d", "la", "il",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def core_name(s: str) -> str:
    """Nome normalizzato senza parentesi e senza parole generiche (istituto, liceo, IC…)."""
    s = re.sub(r"\([^)]*\)", " ", s or "")
    toks = [t for t in norm(s).split() if t not in GENERIC]
    return " ".join(toks)


def same_school(a_name, a_comune, b_name, b_comune) -> float:
    """0 = diverse, 1 = stessa scuola; valori intermedi = somiglianza."""
    if norm(a_comune) != norm(b_comune):
        return 0.0
    ca, cb = core_name(a_name), core_name(b_name)
    if not ca or not cb:
        return 1.0 if norm(a_name) == norm(b_name) else 0.0
    if ca == cb:
        return 1.0
    sa, sb = set(ca.split()), set(cb.split())
    if sa <= sb or sb <= sa:
        return 1.0
    return difflib.SequenceMatcher(None, ca, cb).ratio()


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def append_candidates(rows: list[dict]) -> None:
    new_file = not CANDIDATE_CSV.exists()
    with CANDIDATE_CSV.open("a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDS, delimiter=";", extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def log(line: str) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"calls": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def calls_last_hour(state: dict) -> int:
    limit = now() - timedelta(hours=1)
    state["calls"] = [c for c in state.get("calls", []) if datetime.fromisoformat(c) > limit]
    return len(state["calls"])


def region_of_sigla() -> dict:
    try:
        topo = json.loads(TOPO_PATH.read_text(encoding="utf-8"))
        geoms = next(iter(topo["objects"].values()))["geometries"]
        return {g["properties"]["prov_acr"]: g["properties"]["reg_name"] for g in geoms}
    except Exception:  # noqa: BLE001 - la mappa serve solo a restringere il prompt
        return {}


def extract_json_array(text: str):
    """Estrae l'array JSON dalla risposta; solleva ValueError se non c'è."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("["), t.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("nessun array JSON nella risposta")
        data = json.loads(t[start:end + 1])
    if isinstance(data, dict):
        data = data.get("scuole") or data.get("results") or []
    if not isinstance(data, list):
        raise ValueError("il JSON non è un array")
    return data


def clean_item(it: dict) -> dict | None:
    if not isinstance(it, dict):
        return None
    g = lambda k: str(it.get(k) or "").strip()  # noqa: E731
    item = {
        "scuola": g("scuola"), "comune": g("comune"),
        "provincia": re.sub(r"[^A-Za-z]", "", g("provincia")).upper()[:3],
        "evacuata": g("evacuata").lower().replace("sì", "si"),
        "fonte": g("fonte"), "url": g("url"), "frase_di_conferma": g("frase_di_conferma"),
    }
    if item["evacuata"] not in ("si", "no", "nd"):
        item["evacuata"] = "nd"
    if not item["scuola"] or not item["comune"] or not item["url"].startswith("http"):
        return None
    words = item["frase_di_conferma"].split()
    if len(words) > 15:
        item["frase_di_conferma"] = " ".join(words[:15]) + "…"
    return item


# ------------------------------------------------------------- chiamata API --
def run_search(client, user_prompt: str, state: dict, stats: dict) -> tuple[str, set]:
    """Esegue la ricerca, gestendo pause_turn. Restituisce (testo finale, URL visti)."""
    import anthropic

    messages = [{"role": "user", "content": user_prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES,
              "user_location": {"type": "approximate", "country": "IT", "timezone": "Europe/Rome"}}]
    seen_urls: set[str] = set()
    text = ""
    for attempt in range(1 + MAX_CONTINUATIONS):
        if calls_last_hour(state) >= MAX_CALLS_PER_HOUR:
            stats["errors"].append(f"tetto di {MAX_CALLS_PER_HOUR} chiamate/ora raggiunto")
            break
        state["calls"].append(now().isoformat())
        save_state(state)
        stats["api_calls"] += 1
        try:
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools,
                output_config={"effort": EFFORT},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # se il modello rifiuta, l'API riprova su un modello di riserva
            )
        except anthropic.RateLimitError as e:
            stats["errors"].append(f"rate limit (429): {e.message[:120]}")
            break
        except anthropic.AuthenticationError as e:
            stats["errors"].append(f"chiave API non valida: {e.message[:120]}")
            break
        except anthropic.BadRequestError as e:
            stats["errors"].append(f"richiesta rifiutata (400): {e.message[:200]}")
            break
        except anthropic.APITimeoutError:
            stats["errors"].append("timeout della richiesta")
            break
        except anthropic.APIConnectionError as e:
            stats["errors"].append(f"errore di rete: {str(e)[:120]}")
            break
        except anthropic.APIStatusError as e:
            stats["errors"].append(f"errore API {e.status_code}: {e.message[:120]}")
            break

        u = resp.usage
        stats["input_tokens"] += (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        stats["output_tokens"] += u.output_tokens or 0
        stu = getattr(u, "server_tool_use", None)
        stats["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

        for block in resp.content:
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                seen_urls.update(r.url for r in block.content if getattr(r, "url", None))
            elif block.type == "web_search_tool_result":
                code = getattr(block.content, "error_code", "sconosciuto")
                stats["errors"].append(f"errore ricerca web: {code}")

        if resp.stop_reason == "refusal":
            stats["errors"].append("il modello ha rifiutato la richiesta")
            break
        if resp.stop_reason == "pause_turn":
            # il server ha interrotto il ciclo di ricerca: si riprende reinviando la risposta
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "max_tokens":
            stats["errors"].append("risposta troncata (max_tokens)")
        break
    return text, seen_urls


# -------------------------------------------------------------------- main --
def main() -> int:
    started = now()
    stats = {"api_calls": 0, "searches": 0, "input_tokens": 0, "output_tokens": 0, "errors": []}
    group = REGION_GROUPS[started.hour % len(REGION_GROUPS)]
    new_rows: list[dict] = []
    n_results = n_known = 0

    known = read_csv(SCUOLE_CSV)
    if not known:
        print("ERRORE: data/scuole.csv mancante o vuoto", file=sys.stderr)
        return 1
    previous = read_csv(CANDIDATE_CSV)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        stats["errors"].append("ANTHROPIC_API_KEY non impostata")
    else:
        import anthropic

        client = anthropic.Anthropic(max_retries=3, timeout=300.0)
        state = load_state()

        # nel prompt vanno solo le scuole note delle regioni del turno, per contenere i token
        reg = region_of_sigla()
        local_known = [r for r in known if reg.get(r["provincia"].strip().upper()) in group]
        local_known += [r for r in previous if reg.get(r.get("provincia", "").upper()) in group]
        known_list = "\n".join(f"- {r['scuola']} ({r['comune']}, {r['provincia']})" for r in local_known)
        user_prompt = (
            f"Oggi è {started:%d/%m/%Y}. Cerca articoli sulle scuole che hanno ricevuto la mail "
            f"minatoria dell'11 novembre 2026, concentrandoti su: {', '.join(group)}. "
            "Includi anche scuole di altre regioni se le trovi in articoli nazionali.\n\n"
            "Queste scuole sono GIÀ nel nostro elenco: non riportarle.\n"
            f"{known_list or '(nessuna)'}\n\n"
            "Restituisci solo l'array JSON delle scuole nuove."
        )

        text, seen_urls = run_search(client, user_prompt, state, stats)
        save_state(state)

        items = []
        if text:
            try:
                items = extract_json_array(text)
            except (ValueError, json.JSONDecodeError) as e:
                stats["errors"].append(f"risposta non JSON ({e}): {text[:150]!r}")
        elif stats["api_calls"] and not stats["errors"]:
            stats["errors"].append("risposta vuota")

        stamp = started.isoformat(timespec="minutes")
        pool = [(r["scuola"], r["comune"], "scuole.csv") for r in known] + \
               [(r["scuola"], r["comune"], "candidate.csv") for r in previous]
        for raw in items:
            it = clean_item(raw)
            if not it:
                continue
            n_results += 1
            best, best_ref = 0.0, ""
            for name, comune, where in pool:
                sc = same_school(it["scuola"], it["comune"], name, comune)
                if sc > best:
                    best, best_ref = sc, f"{name} ({comune}) in {where}"
            if best >= 0.85:
                n_known += 1
                continue
            it.update({
                "rilevato_il": stamp,
                "url_nei_risultati_di_ricerca": "si" if it["url"] in seen_urls else "no",
                "possibile_duplicato_di": best_ref if best >= 0.6 else "",
                "regioni_cercate": ", ".join(group),
            })
            new_rows.append(it)
            pool.append((it["scuola"], it["comune"], "candidate.csv"))

        if new_rows:
            append_candidates(new_rows)

    pin, pout = PRICES.get(MODEL, PRICES["claude-opus-5-5"])
    cost = stats["input_tokens"] / 1e6 * pin + stats["output_tokens"] / 1e6 * pout + stats["searches"] * PRICE_PER_SEARCH
    log(" | ".join([
        started.isoformat(timespec="minutes"),
        f"regioni={'/'.join(group)}",
        f"chiamate_api={stats['api_calls']}",
        f"ricerche={stats['searches']}",
        f"risultati={n_results}",
        f"gia_note={n_known}",
        f"candidate_nuove={len(new_rows)}",
        f"token_in={stats['input_tokens']}",
        f"token_out={stats['output_tokens']}",
        f"costo_stimato=${cost:.3f}",
        f"durata={(now() - started).seconds}s",
        "errori=" + ("; ".join(stats["errors"]) if stats["errors"] else "nessuno"),
    ]))

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"new_candidates={len(new_rows)}\n")
    if os.environ.get("ISSUE_BODY_PATH") and new_rows:
        lines = [f"**{len(new_rows)} nuove scuole candidate** rilevate il {started:%d/%m/%Y alle %H:%M} "
                 f"(regioni cercate: {', '.join(group)}).", "",
                 "Da verificare prima di copiarle in `data/scuole.csv`.", "",
                 "| Scuola | Comune | Prov. | Evacuata | Fonte | URL nei risultati | Possibile duplicato |",
                 "|---|---|---|---|---|---|---|"]
        for r in new_rows:
            cell = lambda s: str(s).replace("|", "/").replace("\n", " ")  # noqa: E731
            lines.append(f"| {cell(r['scuola'])} | {cell(r['comune'])} | {r['provincia']} | {r['evacuata']} | "
                         f"[{cell(r['fonte']) or 'link'}]({r['url']}) | {r['url_nei_risultati_di_ricerca']} | "
                         f"{cell(r['possibile_duplicato_di'])} |")
            if r["frase_di_conferma"]:
                lines.append(f"| ↳ _{cell(r['frase_di_conferma'])}_ | | | | | | |")
        Path(os.environ["ISSUE_BODY_PATH"]).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # gli errori di rete/API non fanno fallire il job: restano nel log
    return 0


if __name__ == "__main__":
    sys.exit(main())
