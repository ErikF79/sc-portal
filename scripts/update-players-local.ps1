# Aggiorna solo le statistiche giocatori (PlayBasket) dal PC locale.
#
# PlayBasket risponde 403 agli IP di GitHub Actions: la CI aggiorna calendari,
# risultati e classifiche, mentre rose e game log vengono aggiornati da qui.
# Eseguibile a mano oppure con Task Scheduler (vedi README.md).

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logDir = Join-Path $repo 'data_engine\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir 'update_players_local.log'

function Write-Log([string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -Path $logPath -Value $line -Encoding UTF8
}

$out = Join-Path $repo 'data_engine\output\sc_portal_data.json'

function Get-Summary {
    if (-not (Test-Path $out)) { return $null }
    try { return (Get-Content -Raw -Encoding UTF8 $out | ConvertFrom-Json).sync_summary }
    catch { return $null }
}

$summaryBefore = Get-Summary
$gamesBefore = if ($summaryBefore) { [int]$summaryBefore.players_games } else { 0 }
Write-Log "Avvio aggiornamento giocatori (gare con statistiche attuali: $gamesBefore)"

Write-Log 'Allineamento con il repository remoto...'
git pull --ff-only 2>&1 | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log 'git pull fallito: aggiornamento annullato.'
    exit 1
}

Write-Log 'Esecuzione data engine (--players-only)...'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 data_engine\sc_data_engine.py --players-only 2>&1 | ForEach-Object { Write-Log $_ }
} else {
    & python data_engine\sc_data_engine.py --players-only 2>&1 | ForEach-Object { Write-Log $_ }
}
if ($LASTEXITCODE -ne 0) {
    Write-Log "Engine terminato con codice ${LASTEXITCODE}: nessun commit."
    exit 1
}

$summaryAfter = Get-Summary
$gamesAfter = if ($summaryAfter) { [int]$summaryAfter.players_games } else { 0 }
$staleTeams = if ($summaryAfter) { [int]$summaryAfter.players_stale } else { 0 }
Write-Log "Gare con statistiche dopo l'aggiornamento: $gamesAfter (squadre non aggiornate: $staleTeams)"

if ($gamesAfter -le 0) {
    Write-Log 'Nessuna statistica acquisita: nessun commit.'
    exit 1
}
if ($gamesBefore -ge 50 -and $gamesAfter -lt [math]::Floor($gamesBefore / 2)) {
    Write-Log 'Dati degradati (meno della meta delle gare precedenti): nessun commit.'
    exit 1
}

$changed = git status --porcelain -- data_engine/output/sc_portal_data.json
if (-not $changed) {
    Write-Log 'Nessuna modifica ai dati.'
    exit 0
}

$message = 'data: players update {0}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mmZ')
git add data_engine/output/sc_portal_data.json
git commit -m $message 2>&1 | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log 'Commit fallito.'
    exit 1
}
git push 2>&1 | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log 'Push fallito.'
    exit 1
}

Write-Log 'Aggiornamento completato e pubblicato.'
