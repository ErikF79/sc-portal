# SC Portal

PWA (Progressive Web App) per il **Sport Center**: calendari delle squadre, agenda, planner settimanale e analisi delle classifiche.

- **Versione:** 0.7
- **Dati:** tutti derivati da `data_engine/output/sc_portal_data.json` (nessun dato simulato: dove mancano i risultati si mostra `—` / "nessun risultato")
- **Squadre:** FIP (basket, calendari + risultati), PlayBasket (validazione incrociata), Lega Serie A (calcio), gare personali

## Avvio locale

```
START_SC_PORTAL.bat
```

oppure manualmente:

```
py -m http.server 8000
```

poi apri `http://localhost:8000`.

## Aggiornamento dati

```
RUN_DATA_ENGINE.bat
```

rigenera `data_engine/output/sc_portal_data.json` scaricando i calendari/risultati dalle fonti attive (FIP, PlayBasket, Lega). La PWA rileva il file ad ogni avvio (network-first).

## Struttura

| Percorso | Contenuto |
|---|---|
| `index.html` | applicazione (tutta la UI/JS) |
| `sw.js` | service worker (cache versionale) |
| `manifest.json`, `icon.svg` | installazione PWA |
| `data_engine/` | script di raccolta dati + output JSON |
| `data_engine/output/sc_portal_data.json` | unica fonte dati consumata dalla PWA |

## Note

- Nessun login né credenziali: le fonti usate sono pubbliche.
- Le classifiche hanno almeno 3 gare di gioco, altrimenti restano vuote.
- Installabile da Chrome/Safari mobile (Aggiungi a schermata Home).
