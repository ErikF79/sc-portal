# SC Portal

PWA (Progressive Web App) per il **Sport Center**: calendari delle squadre, agenda, planner settimanale, analisi delle classifiche, **statistiche giocatori** e **schede squadra** con sito ufficiale e grafici.

- **Versione:** 0.9.0
- **Dati:** tutti derivati da `data_engine/output/sc_portal_data.json` (nessun dato simulato: dove mancano i risultati si mostra `—` / "nessun risultato")
- **Squadre:** FIP (basket, calendari + risultati), PlayBasket (rose, medie e game log), Lega Basket (box completo Serie A), Lega Pallacanestro LNP (tabellini ufficiali Serie A2), Lega Serie A (calcio), CarpeDiem Baskin (calendario Baskin CSAIN/EISI), gare personali

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

rigenera `data_engine/output/sc_portal_data.json` scaricando i calendari/risultati dalle fonti attive (FIP, PlayBasket, Lega, LNP). La PWA rileva il file ad ogni avvio (network-first).

Le schede gara LNP sono lette rispettando il `Crawl-delay: 10` dichiarato dal sito: l'arricchimento A2 richiede circa 1-2 minuti per nove gare.

In PWA il pulsante **↻ Aggiorna** (in alto) riscarica il JSON più recente dal server senza svuotare la cache, e in basso nella barra filtri mostra "ultimo aggiornamento" con gare, classifiche e statistiche giocatori. Nota: il pulsante ripubblica solo i dati già presenti sul server; per dati nuovi serve rigenerare l'engine e ripubblicare.

Per aggiornare solo le statistiche giocatori senza ripetere la scansione completa:

```
py data_engine/sc_data_engine.py --players-only
```

(opzioni: `--players-only`, `--skip-players`, `--competition <id>`)

## Statistiche giocatori

- Fonte rose/medie/game log: **PlayBasket** (tab "Giocatori" nella PWA). Oltre ai punti vengono lette, quando pubblicate, le colonne del game log `TL/T2/T3/RD/RO/FF/FS/PR/PP/Ass/St/Min` (tiri, rimbalzi, falli, palle, assist, stoppate, minuti); il campo `rebounds` è la somma di rimbalzi offensivi e difensivi. Se la fonte lascia le colonne a `-`, l'app mostra solo i punti.
- Box completo (minuti, rimbalzi, assist, falli, valutazione…) per la **Serie A** e per la **Serie A2**: per la A1 le gare vengono ricostruite dalla Lega Basket seguendo i `prev_matches` a partire dalle gare correnti del sitemap; per la A2 il Data Engine legge la pagina squadra e le schede gara ufficiali **Lega Pallacanestro (LNP)** e fonde il tabellino nei game log di Cividale. Le partite non ancora giocate non producono righe di box.
- **PlayBasket** per i campionati regionali pubblica spesso solo i punti, oppure tabellini parziali inseriti dagli utenti (non ufficiali): l'app li mostra come dati secondari e non li usa per inventare colonne mancanti.
- **FIP Stats** (boxscore/play-by-play ufficiale di B Interregionale e U19 Eccellenza) è disponibile solo tramite app mobile e non espone API web: non è integrato, nessun dato viene ricavato con credenziali di terzi.
- **Divisione Regionale 2** (ex Promozione): su PlayBasket il codice corretto è `lc=DR2` (girone Gorizia/Trieste), non più `lc=PM`.
- Su FIP la squadra è pubblicata a volte con il nome dello sponsor ("Percorso Sicurezza" per la Nuova Basket Isonzo): l'app mostra il nome del club e riconosce la gara dal **codice gara FIP**, così cambi di nome non creano gare duplicate e i risultati continuano ad aggiornarsi.
- Le competizioni non ancora pubblicate dalla fonte mostrano onestamente "dati non disponibili" (nessun dato inventato). Per la **Under 19 Silver FVG** il calendario arriva da FIP; PlayBasket non usa il codice `lc=U19S` ma pubblica lo stesso girone Gorizia/Trieste come **Under 19 Reg** (`lt=2&lf=M&lr=FR&lp=TS&lc=U19&lg=1`): da lì arrivano rosa e game log di Falconstar Monfalcone sq.B.

## Baskin CarpeDiem

- Fonte: la pagina pubblica del club **https://erikf79.github.io/carpediem-baskin/** (snapshot `data.json`, gestita dal gestionale della società). Il Data Engine legge solo le gare pubblicate: data, ora, competizione (CSAIN/EISI/Torneo), squadre, palestra.
- La fonte **non pubblica punteggi né statistiche individuali**: le gare restano "in programma" e nessun risultato viene inventato. I dati personali dei giocatori presenti nello snapshot (email, telefono) non vengono acquisiti.
- La competizione è secondaria (`verification_status: SECONDARY`): nell'app il dettaglio gara rimanda alla pagina del club.

## Schede squadra

- Dalla vista **Squadre** ogni formazione ha un pulsante **Scheda squadra →** che apre la dashboard dedicata: KPI stagionali, grafico punti fatti/subiti del campionato in corso, **ultime 5 gare**, posizione in classifica e **roster** (dove i dati individuali sono disponibili).
- Link al **sito ufficiale della società** (URL verificati) accanto al link alla fonte della competizione e ai dati individuali.
- Dove un dato manca (sito non pubblicato, competizione non ancora pubblicata, roster assente) la scheda mostra uno stato onesto invece di valori simulati.

## Dettaglio gara

- Nella vista **Agenda** il click su una gara già disputata (punteggio presente) apre la finestra **Dettaglio gara**: intestazione con squadre e punteggio, data/ora, campo, competizione e stato.
- Link diretti alla **fonte** della gara (FIP / PlayBasket / Lega Serie A) e al **sito ufficiale** della squadra (dove verificato).
- Per il basket con box disponibile (Serie A via Lega Basket; Serie A2 via tabellino ufficiale LNP; competizioni PlayBasket quando il game log pubblica le colonne statistiche) viene ricostruito il **tabellino giocatori** di quella gara (minuti, punti, rimbalzi, assist, tiri, falli, valutazione…), filtrando i log individuali; dove la fonte non lo pubblica il dato non viene simulato. Il tabellino indica sempre la fonte usata (PlayBasket, Lega Basket o LNP).

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
