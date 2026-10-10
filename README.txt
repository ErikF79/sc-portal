SC PORTAL 0.7 - DATA ENGINE + PWA INTEGRATA

Avvio
- START_SC_PORTAL.bat : avvia un server locale e apre http://localhost:8080
- RUN_DATA_ENGINE.bat : esegue l'acquisizione full season

Data Engine
- HTTP su tre livelli: requests -> urllib (SSL) -> fallback PowerShell (TLS 1.2).
- FIP Serie A: 15 giornate andata + 15 ritorno, filtra Trieste e APU Udine.
- PlayBasket U19 Gold FVG: filtra Falconstar Monfalcone U19G.
- Retry configurabile, logging leggibile, parser regex (default) oppure bs4.
- Deduplica le osservazioni alle esecuzioni successive.

Dipendenze opzionali (consigliate)
- pip install -r data_engine\requirements.txt
- Senza di esse il motore usa solo la standard library (+ fallback PowerShell
  se l'interprete Anaconda non dispone di SSL).

Output
- data_engine/output/sc_portal_data.json      (letto automaticamente dalla PWA)
- data_engine/logs/last_sync.json
- data_engine/observations/observations.json
- data_engine/raw/<competition_id>/...

PWA
- Legge in automatico data_engine/output/sc_portal_data.json all'avvio.
- Pulsante "Importa JSON" per caricare l'export anche da file:// (senza server).
- Calendario e Planner con navigazione mese/settimana e comando "Oggi".
- Rilevamento sovrapposizioni su data/ora reali (finestra 120 minuti).
- Data Center: risultati e trend solo quando esiste il dato sorgente.

CLI Data Engine
- py data_engine\sc_data_engine.py --limit-rounds 2          (debug rapido)
- py data_engine\sc_data_engine.py --competition basket_a1_2026_27
- py data_engine\sc_data_engine.py --parser bs4 --verbose
