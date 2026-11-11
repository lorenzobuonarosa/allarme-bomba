# Scuole con allarme bomba dell'11 novembre 2026

Dashboard statica (mappa per provincia + tabella + nota metodologica) delle scuole
che hanno ricevuto la mail minatoria dell'11 novembre 2026.

```
index.html                    la pagina (D3 e topojson-client in vendor/, nessun backend)
build_data.py                 data/scuole.csv -> data/scuole.json
update_check.py               ricerca oraria di nuove scuole -> data/candidate.csv
.github/workflows/update.yml  esecuzione oraria su GitHub Actions
data/scuole.csv               dataset definitivo (si modifica solo a mano)
data/province.topojson        confini provinciali (110 unità, campo prov_acr)
```

## Rigenerare i dati

```bash
python3 build_data.py
```

Per fissare l'ora mostrata nel sottotitolo:

```bash
python3 build_data.py --updated "2026-11-11 18:30"
```

Lo script legge `data/scuole.csv` (`;`, virgola decimale, UTF-8 con BOM), lo collega
alla mappa (`provincia` del CSV ↔ `prov_acr` del TopoJSON) e scrive `data/scuole.json`.
A video stampa: sigle del CSV senza provincia nella mappa, rimappature applicate
(es. `SU` Sud Sardegna → provincia del 2025 ricavata dalle coordinate), province della
mappa senza corrispondenza nelle sigle ISTAT, coordinate che cadono in un'altra provincia.
Se una riga non si collega a nessuna provincia si ferma con errore senza scrivere nulla.
Controlla che la somma per provincia sia uguale al numero di righe.

Classi della legenda: quintili dei conteggi delle province con almeno una scuola
(oggi 1, 2–3, 4–5, 6–9, 10 e più); province senza scuole in grigio.

## Aggiornamento orario (in revisione, mai automatico)

`update_check.py` usa l'API Anthropic con la ricerca web, scrive solo le scuole nuove in
`data/candidate.csv` (con data e ora) e una riga in `data/update_log.txt`.
Non tocca `data/scuole.csv`.

1. Nel repository: *Settings → Secrets and variables → Actions → New repository secret*,
   nome `ANTHROPIC_API_KEY`.
2. Il workflow parte ogni ora (`0 * * * *`, UTC) solo dal branch predefinito; si può
   avviare a mano da *Actions → Controllo orario nuove scuole → Run workflow*.
3. Quando ci sono candidate nuove apre una issue con etichetta `candidate`, o aggiunge un
   commento a quella già aperta. Chiudi la issue dopo la revisione.
4. Revisione: copia a mano le righe verificate in `data/scuole.csv`, poi
   `python3 build_data.py`, commit e push.

Limiti di costo: al massimo `UPDATE_MAX_SEARCHES` ricerche per esecuzione (6) e
`UPDATE_MAX_CALLS_PER_HOUR` chiamate API nell'ultima ora (4, contate in
`data/update_state.json`). Ogni riga del log riporta token e costo stimato.
Ordine di grandezza con `claude-opus-5-5`: 0,30–0,50 $ a esecuzione, cioè 7–12 $ al giorno;
con `UPDATE_MODEL=claude-sonnet-5-5` circa la metà. Per fermarlo: disattiva il workflow
dalla scheda Actions.

Per provarlo in locale:

```bash
pip install "anthropic>=2.8,<3"
ANTHROPIC_API_KEY=... python3 update_check.py
```

## Pubblicare su GitHub Pages

*Settings → Pages → Build and deployment → Deploy from a branch*, branch `main`, cartella
`/ (root)`. La pagina sarà su `https://<utente>.github.io/<repository>/`.
Va servita via HTTP: aprendo `index.html` dal disco il browser blocca la lettura dei JSON
(in locale: `python3 -m http.server` e poi `http://localhost:8000`).

## Incorporare in un iframe

```html
<iframe src="https://<utente>.github.io/<repository>/" title="Scuole con allarme bomba dell'11 novembre"
        style="width:100%;border:0;height:1500px" loading="lazy" id="scuole-allarme"></iframe>
<script>
  window.addEventListener("message", function (e) {
    if (e.data && e.data.type === "scuole-allarme-bomba:height") {
      document.getElementById("scuole-allarme").style.height = e.data.height + "px";
    }
  });
</script>
```

Lo script è facoltativo: la pagina comunica la propria altezza e l'iframe si adatta.
Per aprire la tabella già filtrata aggiungi la sigla in fondo all'URL, es. `…/#RM`.
