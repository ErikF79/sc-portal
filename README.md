# SC Portal

PWA (Progressive Web App) per il **Sport Center**: calendari delle squadre, agenda, planner settimanale, analisi delle classifiche, **statistiche giocatori** e **schede squadra** con sito ufficiale e grafici.

- **Versione:** 0.8.0
- **Dati:** tutti derivati da `data_engine/output/sc_portal_data.json` (nessun dato simulato: dove mancano i risultati si mostra `—` / "nessun risultato")
- **Squadre:** FIP (basket, calendari + risultati), PlayBasket (rose, medie e game log), Lega Basket (box completo Serie A), Lega Serie A (calcio), gare personali

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

In PWA il pulsante **↻ Aggiorna** (in alto) riscarica il JSON più recente dal server senza svuotare la cache, e in basso nella barra filtri mostra "ultimo aggiornamento" con gare, classifiche e statistiche giocatori. Nota: il pulsante ripubblica solo i dati già presenti sul server; per dati nuovi serve rigenerare l'engine e ripubblicare.

Per aggiornare solo le statistiche giocatori senza ripetere la scansione completa:

```
py data_engine/sc_data_engine.py --players-only
```

(opzioni: `--players-only`, `--skip-players`, `--competition <id>`)

## Statistiche giocatori

- Fonte rose/medie/game log: **PlayBasket** (tab "Giocatori" nella PWA).
- Box completo (minuti, rimbalzi, assist, falli, valutazione…) solo per la **Serie A**: le gare vengono ricostruite dalla Lega Basket seguendo i `prev_matches` a partire dalle gare correnti del sitemap; le partite non ancora giocate non producono righe di box.
- Le competizioni non ancora pubblicate dalla fonte mostrano onestamente "dati non disponibili" (nessun dato inventato).

## Schede squadra

- Dalla vista **Squadre** ogni formazione ha un pulsante **Scheda squadra →** che apre la dashboard dedicata: KPI stagionali, grafico punti fatti/subiti del campionato in corso, **ultime 5 gare**, posizione in classifica e **roster** (dove i dati individuali sono disponibili).
- Link al **sito ufficiale della società** (URL verificati) accanto al link alla fonte della competizione e ai dati individuali.
- Dove un dato manca (sito non pubblicato, competizione non ancora pubblicata, roster assente) la scheda mostra uno stato onesto invece di valori simulati.

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
