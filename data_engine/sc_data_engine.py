#!/usr/bin/env python3
"""SC Portal Data Engine 0.7 - Full Season Acquisition.

Acquisisce i calendari delle competizioni configurate (FIP, PlayBasket,
Lega Serie A via api-sdp.legaseriea.it), normalizza le osservazioni,
deduplica e produce l'export consumato dalla PWA.

Ottimizzato rispetto alla 0.5B:
  * HTTP su tre livelli: requests -> urllib (SSL) -> PowerShell (fallback).
  * Retry con backoff configurabile.
  * Logging leggibile, argparse, pathlib, f-string, type hints.
  * Output invariato: output/sc_portal_data.json + observations.json + logs.
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib import parse as urlparse

try:  # requests e' opzionale: se manca si usa urllib/PowerShell
    import requests
except Exception:  # pragma: no cover
    requests = None

try:  # urllib.request puo' non essere importabile se SSL e' assente
    import urllib.request as urllib_request
except Exception:  # pragma: no cover
    urllib_request = None

try:
    import ssl
except Exception:  # pragma: no cover
    ssl = None

try:  # BeautifulSoup e' opzionale: parser DOM alternativo (--parser bs4)
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

ENGINE_VERSION = "0.7"
DEFAULT_UA = f"Mozilla/5.0 SC-Portal-Data-Engine/{ENGINE_VERSION}"

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config" / "competitions.json"
RAW = BASE / "raw"
OBS = BASE / "observations"
OUT = BASE / "output"
LOG = BASE / "logs"

MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

log = logging.getLogger("sc_engine")


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def now() -> str:
    return datetime.now().astimezone().isoformat()


def safe_name(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, obj: Any) -> None:
    """Scrittura atomica: evita file corrotti se il processo viene interrotto."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def obs_id(*values: Any) -> str:
    joined = "|".join(str(v or "") for v in values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


def as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and re.fullmatch(r"\d{1,3}", str(value)) else None
    except Exception:
        return None


def norm_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def python_https_available() -> bool:
    return bool(ssl is not None and getattr(ssl, "OPENSSL_VERSION", None))


def fetch_powershell(url: str, timeout: int = 90) -> str:
    fd, path = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    ps = (
        "$ProgressPreference='SilentlyContinue';"
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
        "Invoke-WebRequest -UseBasicParsing -Uri '%s' -OutFile '%s'"
        % (url.replace("'", "''"), path.replace("'", "''"))
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError("PowerShell download failed: " + (result.stderr.strip() or result.stdout.strip()))
        raw = Path(path).read_bytes()
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def fetch(url: str, *, timeout: int = 60, retries: int = 1, backoff: float = 1.5) -> tuple[str, str]:
    """Scarica una URL con retry. Restituisce (html, transport)."""
    attempts = max(1, retries)
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            if requests is not None:
                response = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout)
                response.raise_for_status()
                response.encoding = response.apparent_encoding or response.encoding or "utf-8"
                return response.text, "REQUESTS"
            if urllib_request is not None and python_https_available():
                request = urllib_request.Request(url, headers={"User-Agent": DEFAULT_UA})
                with urllib_request.urlopen(request, timeout=timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, "replace"), "URLLIB"
            return fetch_powershell(url, timeout=timeout), "POWERSHELL"
        except Exception as exc:  # noqa: BLE001 - vogliamo gestire qualsiasi errore di rete
            last_error = exc
            log.warning("Tentativo %d/%d fallito per %s: %s", attempt, attempts, url, exc)
            if attempt < attempts:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Impossibile scaricare {url}: {last_error}")


# --------------------------------------------------------------------------- #
# HTML -> flusso di righe
# --------------------------------------------------------------------------- #
_BLOCK_TAGS = r"div|p|h\d|li|tr|td|th|section|article|a"
_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&apos;": "'"}


def _clean_text(text: str) -> str:
    for src, dst in _ENTITIES.items():
        text = text.replace(src, dst)
    return text


def extract_lines_regex(html: str) -> list[str]:
    x = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    x = re.sub(r"<style[\s\S]*?</style>", " ", x, flags=re.I)
    x = re.sub(r"<br\s*/?>", "\n", x, flags=re.I)
    x = re.sub(rf"</?(?:{_BLOCK_TAGS})[^>]*>", "\n", x, flags=re.I)
    x = re.sub(r"<[^>]+>", " ", x)
    x = _clean_text(x)
    return [re.sub(r"[ \t]+", " ", row).strip() for row in x.splitlines() if row.strip()]


def extract_lines_bs4(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    text = _clean_text(text)
    return [re.sub(r"[ \t]+", " ", row).strip() for row in text.splitlines() if row.strip()]


def extract_lines(html: str, mode: str) -> list[str]:
    if mode == "bs4":
        if BeautifulSoup is None:
            log.warning("Parser bs4 richiesto ma BeautifulSoup non e' installato: uso regex.")
            return extract_lines_regex(html)
        try:
            return extract_lines_bs4(html)
        except Exception as exc:  # noqa: BLE001
            log.warning("Parser bs4 fallito (%s): uso regex.", exc)
    return extract_lines_regex(html)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def team_ids(home: str, away: str, tracked: Iterable[dict]) -> list[str]:
    blob = f"{home} {away}".casefold()
    found: list[str] = []
    for team in tracked:
        names = [team.get("canonical_name", "")] + list(team.get("aliases", []))
        if any(name and name.casefold() in blob for name in names):
            found.append(team["team_id"])
    return found


def parse_fip_round(html: str, comp: dict, leg: str, round_no: int, url: str, parser: str) -> list[dict]:
    """Parsa una giornata FIP.

    Con BeautifulSoup disponibile usa il DOM (squadre, punteggi e campo dal
    blocco '.results-matches__match'); altrimenti ricade sull'estrazione
    testuale dei marcatori 'Squadra di casa' / 'Squadra ospite'.
    """
    if BeautifulSoup is not None:
        try:
            return parse_fip_round_dom(html, comp, leg, round_no, url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Parsing DOM FIP fallito (%s): uso regex.", exc)
    return parse_fip_round_regex(html, comp, leg, round_no, url)


def parse_fip_round_dom(html: str, comp: dict, leg: str, round_no: int, url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    date_pattern = re.compile(r"^(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})$", re.I)
    for match in soup.select(".results-matches__match"):
        names = match.select(".teams .team .team__name")
        if len(names) < 2:
            continue
        home = " ".join(names[0].get_text(" ", strip=True).split())
        away = " ".join(names[1].get_text(" ", strip=True).split())
        if not home or not away:
            continue
        flags = match.select(".teams .team .team__flag img")
        home_logo = flags[0].get("src") if len(flags) > 0 and flags[0].get("src") else None
        away_logo = flags[1].get("src") if len(flags) > 1 and flags[1].get("src") else None
        points = match.select(".teams .team .team__points")
        home_score = away_score = None
        if len(points) >= 2:
            raw = [p.get_text(" ", strip=True).strip() for p in points[:2]]
            if len(raw) == 2 and raw[0].isdigit() and raw[1].isdigit():
                home_score, away_score = int(raw[0]), int(raw[1])
        date_value = None
        time_value = None
        datetime_box = match.select_one(".results-matches__match__info .datetime")
        if datetime_box is not None:
            el_date = datetime_box.select_one(".date")
            el_time = datetime_box.select_one(".time")
            if el_date is not None:
                date_value = el_date.get_text(" ", strip=True)
            if el_time is not None:
                time_value = el_time.get_text(" ", strip=True)
        date_match = date_pattern.match(date_value or "")
        if not date_match:
            continue
        day, month, year = date_match.groups()
        date = f"{year}-{MONTHS[month.casefold()]:02d}-{int(day):02d}"
        ref_box = match.select_one(".results-matches__match__info .ref")
        number = None
        if ref_box is not None:
            first = next((w for w in ref_box.get_text(" ", strip=True).split() if w.isdigit()), None)
            number = first
        venue = None
        moreinfo = match.select_one(".results-matches__match__moreinfo")
        if moreinfo is not None:
            for label in moreinfo.find_all("div", class_="label"):
                if label.get_text(strip=True).casefold() == "campo di gioco":
                    value = label.find_next_sibling("div", class_="value")
                    if value is not None:
                        venue = " ".join(value.get_text(" ", strip=True).split())
                    break
        tracked = team_ids(home, away, comp.get("tracked_teams", []))
        if not tracked:
            continue
        out.append({
            "observation_id": obs_id("FIP", comp["competition_id"], number or "", home, away, date),
            "provider": "FIP",
            "competition_id": comp["competition_id"],
            "season_id": comp["season_id"],
            "leg": leg,
            "round": round_no,
            "external_match_id": number,
            "home": home,
            "away": away,
            "home_logo": home_logo,
            "away_logo": away_logo,
            "date": date,
            "time": time_value,
            "venue": venue,
            "home_score": home_score,
            "away_score": away_score,
            "status": "FINISHED" if home_score is not None and away_score is not None else "SCHEDULED",
            "tracked_team_ids": tracked,
            "source_url": url,
            "observed_at": now(),
        })
    return out


def parse_fip_round_regex(html: str, comp: dict, leg: str, round_no: int, url: str) -> list[dict]:
    """Estrattore testuale legacy (marker 'Squadra di casa'/'Squadra ospite')."""
    lines = extract_lines(html, "regex")
    pattern = re.compile(r"^(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})$", re.I)
    out: list[dict] = []
    for i, value in enumerate(lines):
        match = pattern.match(value)
        if not match:
            continue
        after = lines[i + 1:i + 16]
        time_value = next((z for z in after if re.fullmatch(r"\d{1,2}:\d{2}", z)), None)
        if not time_value:
            continue
        home = away = None
        if "Squadra di casa" in after and "Squadra ospite" in after:
            casa = after.index("Squadra di casa")
            ospite = after.index("Squadra ospite")
            home = after[casa + 1] if casa + 1 < len(after) else None
            away = after[ospite + 1] if ospite + 1 < len(after) else None
        if not home or not away:
            before = lines[max(0, i - 8):i]
            teams = [
                z for z in before
                if len(z) > 2 and not z.isdigit()
                and z.upper() not in ("INFO", "RISULTATI")
                and not re.match(r"^\d+[°a]?\s*-", z)
            ]
            if len(teams) < 2:
                continue
            home, away = teams[-2], teams[-1]
        day, month, year = match.groups()
        date = f"{year}-{MONTHS[month.casefold()]:02d}-{int(day):02d}"
        out.append({
            "observation_id": obs_id("FIP", comp["competition_id"], number or "", home, away, date),
            "provider": "FIP",
            "competition_id": comp["competition_id"],
            "season_id": comp["season_id"],
            "leg": leg,
            "round": round_no,
            "external_match_id": number,
            "home": home,
            "away": away,
            "home_logo": None,
            "away_logo": None,
            "date": date,
            "time": time_value,
            "venue": venue,
            "home_score": None,
            "away_score": None,
            "status": "SCHEDULED",
            "tracked_team_ids": tracked,
            "source_url": url,
            "observed_at": now(),
        })
    return out


def fip_url(comp: dict, leg_code: str, round_no: int) -> str:
    c = comp["configuration"]
    params = {
        "group": c["group"],
        "regione_codice": c.get("regione_codice", ""),
        "comitato_codice": c["comitato_codice"],
        "sesso": c["sesso"],
        "codice_campionato": c["codice_campionato"],
        "codice_fase": c["codice_fase"],
        "codice_girone": c["codice_girone"],
        "codice_ar": leg_code,
        "giornata": round_no,
    }
    return comp["base_url"] + "?" + urlparse.urlencode(params)


def parse_playbasket(html: str, comp: dict, url: str, parser: str) -> list[dict]:
    """Parsa il calendario PlayBasket (mod=cl).

    Struttura osservata: 'DD/MM' -> 'Casa' -> 'Ospite' -> ('HH:MM' | punteggio casa + ospite).
    Il punteggio indica gara conclusa; l'orario indica gara programmata.
    L'anno si deduce dalla stagione (>= agosto -> anno iniziale, altrimenti +1).
    """
    lines = extract_lines(html, parser)
    start_year = int(comp["configuration"]["season_start_year"])
    date_re = re.compile(r"^(\d{2})/(\d{2})$")
    time_re = re.compile(r"^\d{1,2}:\d{2}$")
    out: list[dict] = []
    i = 0
    total = len(lines)
    while i < total:
        match = date_re.match(lines[i])
        if not match or i + 2 >= total:
            i += 1
            continue
        home, away = lines[i + 1], lines[i + 2]
        if "/" in home or "/" in away or home.isdigit() or away.isdigit() or len(home) < 3 or len(away) < 3:
            i += 1
            continue
        time_value: Optional[str] = None
        home_score = away_score = None
        cursor = i + 3
        if cursor < total and time_re.match(lines[cursor]):
            time_value = lines[cursor]
            cursor += 1
        elif cursor + 1 < total and re.fullmatch(r"\d{1,3}", lines[cursor]) and re.fullmatch(r"\d{1,3}", lines[cursor + 1]):
            home_score, away_score = int(lines[cursor]), int(lines[cursor + 1])
            cursor += 2
        else:
            i += 1
            continue
        day, month = match.group(1), match.group(2)
        year = start_year if int(month) >= 8 else start_year + 1
        date = f"{year:04d}-{month}-{day}"
        tracked = team_ids(home, away, comp.get("tracked_teams", []))
        if tracked:
            status = "FINISHED" if home_score is not None and away_score is not None else "SCHEDULED"
            out.append({
                "observation_id": obs_id("PLAYBASKET", comp["competition_id"], home, away, date, time_value or str(home_score)),
                "provider": "PLAYBASKET",
                "competition_id": comp["competition_id"],
                "season_id": comp["season_id"],
                "leg": None,
                "round": None,
                "external_match_id": None,
                "home": home,
                "away": away,
                "home_logo": None,
                "away_logo": None,
                "date": date,
                "time": time_value,
                "venue": None,
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
                "tracked_team_ids": tracked,
                "source_url": url,
                "observed_at": now(),
            })
        i = cursor
    return out


SERIEA_LOGO_CDN = "https://img.legaseriea.it/"


def _seriea_logo(team: Any) -> Optional[str]:
    """URL del logo ufficiale Lega Serie A (immagini in 'imagery')."""
    imagery = (team or {}).get("imagery") or {}
    path = imagery.get("teamLogo") or imagery.get("teamImage")
    return SERIEA_LOGO_CDN + path if path else None


def parse_legaseriea(text: str, comp: dict, url: str, parser: str) -> list[dict]:
    """Parsa il JSON dell'API Lega Serie A (api-sdp.legaseriea.it).

    'matchDateLocal' puo' contenere solo data (orario non ancora fissato) oppure
    data+ora. I punteggi sono int per le gare concluse, None altrimenti.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return []
    matches = data.get("matches", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for m in matches:
        date_local = m.get("matchDateLocal") or m.get("matchDateUtc") or ""
        if not date_local:
            continue
        date_part = date_local[:10]
        time_part: Optional[str] = None
        if "T" in date_local:
            time_part = date_local[11:16]
        home = (m.get("home") or {}).get("shortName") or (m.get("home") or {}).get("officialName")
        away = (m.get("away") or {}).get("shortName") or (m.get("away") or {}).get("officialName")
        if not home or not away:
            continue
        home_logo = _seriea_logo(m.get("home"))
        away_logo = _seriea_logo(m.get("away"))
        api_status = str(m.get("status") or m.get("providerStatus") or "").upper()
        phase = str(m.get("phase") or "").upper()
        finished = api_status in ("FINISHED", "PLAYED", "ENDED") or phase == "FULL_TIME"
        home_score = m.get("homeScorePush")
        away_score = m.get("awayScorePush")
        if home_score is None:
            home_score = m.get("providerHomeScore")
        if away_score is None:
            away_score = m.get("providerAwayScore")
        if not finished:
            home_score = None
            away_score = None
        tracked = team_ids(home, away, comp.get("tracked_teams", []))
        if not tracked:
            continue
        round_name = (m.get("matchSet") or {}).get("name") or ""
        round_match = re.search(r"(\d+)", round_name)
        out.append({
            "observation_id": obs_id("SERIEA", comp["competition_id"], m.get("matchId") or "", home, away, date_part),
            "provider": "LEGA_SERIE_A",
            "competition_id": comp["competition_id"],
            "season_id": comp["season_id"],
            "leg": None,
            "round": int(round_match.group(1)) if round_match else None,
            "external_match_id": m.get("providerId") or m.get("matchId"),
            "home": home,
            "away": away,
            "home_logo": home_logo,
            "away_logo": away_logo,
            "date": date_part,
            "time": time_part,
            "venue": m.get("stadiumName"),
            "home_score": home_score,
            "away_score": away_score,
            "status": "FINISHED" if finished and home_score is not None and away_score is not None else "SCHEDULED",
            "tracked_team_ids": tracked,
            "source_url": url,
            "observed_at": now(),
        })
    return out


def save_snapshot(comp_id: str, label: str, html: str, url: str, transport: str) -> str:
    folder = RAW / safe_name(comp_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (safe_name(label) + ".html")
    path.write_text(html, encoding="utf-8")
    write_json(path.with_suffix(path.suffix + ".meta.json"), {
        "competition_id": comp_id,
        "label": label,
        "source_url": url,
        "transport": transport,
        "retrieved_at": now(),
        "content_hash": "sha256:" + hash_text(html),
        "parser_version": ENGINE_VERSION,
    })
    return str(path.relative_to(BASE))


# --------------------------------------------------------------------------- #
# Classifiche
# --------------------------------------------------------------------------- #
def parse_fip_standings(html: str) -> list[dict]:
    """Estrae dal DOM FIP la classifica ufficiale del girone.

    Colonna presente nella pagina risultati: Pos | Squadra | Punti | G | V |
    P | PF | PS (blocco '.results-ranking-full table').
    """
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one(".results-ranking-full table")
    if table is None:
        return []
    standings: list[dict] = []
    for tr in table.select("tbody tr"):
        if "team__info" in (tr.get("class") or []):
            continue
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in tr.find_all("td")]
        cells = [c for c in cells if c]
        if len(cells) != 8:
            continue
        try:
            rank, played, wins, losses, pf, pa = int(cells[0]), int(cells[3]), int(cells[4]), int(cells[5]), int(cells[6]), int(cells[7])
            points = int(cells[2])
        except ValueError:
            continue
        standings.append({
            "rank": rank,
            "team": cells[1],
            "played": played,
            "wins": wins,
            "losses": losses,
            "draws": 0,
            "points": points,
            "pf": pf,
            "pa": pa,
            "diff": pf - pa,
            "form": "",
        })
    return standings


def parse_legaseriea_standings(text: str, comp: dict, url: str, parser: str) -> list[dict]:
    """Classifica ufficiale Lega Serie A (blocco 'table' di /standings/overall)."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    blocks = data.get("standings") or []
    table = next((b for b in blocks if b.get("type") == "table"), blocks[0] if blocks else None)
    if not table:
        return []
    standings: list[dict] = []
    for item in table.get("teams", []):
        stats = {s.get("statsId"): s.get("statsValue") for s in item.get("stats", [])}
        form = ""
        form_value = stats.get("form")
        if isinstance(form_value, list):
            form = "".join((e or {}).get("formType") or "-" for e in form_value[-5:])
        name = item.get("shortName") or item.get("officialName") or item.get("mediaName")
        standings.append({
            "rank": as_int(stats.get("rank")),
            "team": (name or "").strip(),
            "played": as_int(stats.get("matches-played")),
            "wins": as_int(stats.get("win")),
            "draws": as_int(stats.get("draw")),
            "losses": as_int(stats.get("lose")),
            "points": as_int(stats.get("points")),
            "pf": as_int(stats.get("goals-for")),
            "pa": as_int(stats.get("goals-against")),
            "diff": as_int(stats.get("goal-difference")),
            "form": form,
        })
    return standings


def build_standings(config: dict, scans: dict[str, "ScanResult"], errors: list[dict]) -> list[dict]:
    """Costruisce l'elenco classifiche (OFFICIAL da FIP o Lega Serie A)."""
    out: list[dict] = []
    policy = config.get("request_policy", {})
    for comp in config.get("competitions", []):
        if not comp.get("enabled", True):
            continue
        comp_id = comp["competition_id"]
        tracked = [t["team_id"] for t in comp.get("tracked_teams", [])]
        entry = {
            "competition_id": comp_id,
            "competition_name": comp.get("name", comp_id),
            "season_id": comp.get("season_id"),
            "provider": comp["provider"],
            "tracked_teams": tracked,
            "mode": None,
            "source_url": comp.get("url") or comp.get("base_url", ""),
            "generated_at": now(),
            "rows": [],
        }
        st = comp.get("standings")
        if st and st.get("provider") == "LEGA_SERIE_A":
            entry["mode"] = "OFFICIAL"
            entry["source_url"] = st.get("url")
            try:
                raw, _ = fetch(st["url"], retries=int(policy.get("max_retries", 1)) + 1)
                entry["rows"] = parse_legaseriea_standings(raw, comp, st["url"], "regex")
            except Exception as exc:  # noqa: BLE001
                errors.append({"competition_id": comp_id, "standings": True, "message": str(exc)})
                log.error("Classifica %s fallita: %s", comp_id, exc)
        elif comp["provider"] == "FIP":
            scan = scans.get(comp_id)
            entry["mode"] = "OFFICIAL"
            entry["rows"] = scan.standings if scan is not None else []
        else:
            continue
        out.append(entry)
    return out


def build_team_logos(config: dict, matches: list[dict]) -> dict[str, str]:
    """Mappa team_id -> URL del logo ufficiale (FIP / Lega Serie A / sito club).

    La mappa curata config/team_logos.json (nome normalizzato -> URL) ha la
    priorita': contiene solo fonti ufficiali verificate e raggiungibili, per le
    squadre che la fonte di gara non espone (placeholder FIP, CDN non
    raggiungibile). Le osservazioni correnti servono da ripiego per le squadre
    che non vi sono ancora presenti.
    """
    names_by_id: dict[str, set[str]] = {}
    for comp in config.get("competitions", []):
        for t in comp.get("tracked_teams", []):
            bag = names_by_id.setdefault(t["team_id"], set())
            bag.add(norm_name(t.get("canonical_name")))
            for alias in t.get("aliases", []):
                bag.add(norm_name(alias))
    seed = read_json(BASE / "config" / "team_logos.json", {})
    seed = {norm_name(k): v for k, v in (seed or {}).items() if v}
    dynamic: dict[str, str] = {}
    for match in matches:
        home, away = norm_name(match.get("home")), norm_name(match.get("away"))
        for tid in match.get("tracked_team_ids", []):
            names = names_by_id.get(tid, set())
            if match.get("home_logo") and any(n and n in home for n in names):
                dynamic[tid] = match["home_logo"]
            elif match.get("away_logo") and any(n and n in away for n in names):
                dynamic[tid] = match["away_logo"]
    logos: dict[str, str] = {}
    for tid, names in names_by_id.items():
        hit = next((seed[key] for key in names if key and seed.get(key)), None)
        value = hit or dynamic.get(tid)
        if value:
            logos[tid] = value
    return logos


# --------------------------------------------------------------------------- #
# Statistiche giocatori (PlayBasket + box Lega Basket)
# --------------------------------------------------------------------------- #
PB_BASE = "https://www.playbasket.it/"
LBA_GAMES_SITEMAP = "https://www.legabasket.it/games.xml"
_PB_ROLES = ("guardia", "playmaker", "ala", "centro", "pivot")


def pb_abs(url: str) -> str:
    """URL relativa PlayBasket -> assoluta (i link sono relativi al path regione)."""
    url = (url or "").replace("&amp;", "&").strip()
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return "https://www.playbasket.it" + url
    return PB_BASE + url


def pb_profile_url(url: str) -> str:
    """Forza mod=st: il tab con il game log (altri mod mostrano solo il riepilogo)."""
    if re.search(r"[?&]mod=", url or ""):
        return re.sub(r"([?&])mod=[a-z]+", r"\1mod=st", url, flags=re.I)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}mod=st"


def _text(fragment: Any) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", str(fragment or "")))).strip()


def _to_num(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().replace("%", "").replace(",", ".")
    if not s or s in ("-", "\u2013"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(value: Any) -> Optional[int]:
    number = _to_num(value)
    return int(number) if number is not None else None


def _cells(chunk: str) -> list[str]:
    """Celle di una riga tollerante ai <td> non chiusi (PlayBasket li omette)."""
    raw = re.findall(r"<td[^>]*>([\s\S]*?)(?=<td[^>]*>|</tr>|$)", chunk, flags=re.I)
    return [re.sub(r"</t[dh]>\s*$", "", c, flags=re.I).strip() for c in raw]


def parse_pb_teams(html_src: str) -> list[dict]:
    """Pagina 'Squadre' (mod=tm): nome societa' + URL della rosa."""
    out: list[dict] = []
    for m in re.finditer(
        r"<td class=['\"]team_roster['\"]>\s*<h2>\s*<a href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        html_src or "", flags=re.I | re.S,
    ):
        club = _text(m.group(2))
        if club:
            out.append({"club": club, "roster_url": pb_abs(m.group(1))})
    return out


def parse_pb_roster(html_src: str) -> list[dict]:
    """Rosa del club: nome, Pts, Ppg, eta' (title=anno), ruolo, link al profilo."""
    table = next(
        (m.group(1) for m in re.finditer(r"<table[^>]*>([\s\S]*?)</table>", html_src or "", flags=re.I)
         if re.search(r"COGNOME", m.group(1), flags=re.I)),
        None,
    )
    if table is None:
        return []
    out: list[dict] = []
    for chunk in re.split(r"<tr[^>]*>", table, flags=re.I)[1:]:
        link = re.search(r"<a href=[\"'](profile\.php[^\"']+)[\"'][^>]*>(.*?)</a>", chunk, flags=re.I | re.S)
        if not link:
            continue
        name = _text(link.group(2))
        if not name:
            continue
        cells = _cells(chunk)
        idx = next((i for i, c in enumerate(cells) if "profile.php" in c), None)
        pts = _text(cells[idx + 1]) if idx is not None and idx + 1 < len(cells) else ""
        ppg = _text(cells[idx + 2]) if idx is not None and idx + 2 < len(cells) else ""
        birth = re.search(r"<td[^>]*title=[\"'](\d{4})[\"'][^>]*>([\s\S]*?)</td>", chunk, flags=re.I)
        role = ""
        if birth:
            after = re.match(r"\s*<td[^>]*>([\s\S]*?)(?=<td[^>]*>|</tr>|$)", chunk[birth.end():], flags=re.I)
            role = _text(after.group(1)) if after else ""
        out.append({
            "name": name,
            "profile_url": pb_abs(link.group(1)),
            "birth_year": _int(birth.group(1)) if birth else None,
            "age": _int(birth.group(2)) if birth else None,
            "role": role,
            "points": _int(pts),
            "ppg": _to_num(ppg),
            "no_presence": "Nessuna presenza" in chunk,
        })
    return out


def parse_pb_profile(html_src: str) -> dict:
    """Scheda giocatore (mod=st): eta'/ruolo, totali e game log per partita."""
    info: dict[str, Any] = {"age": None, "role": None, "points_total": None, "points_avg": None, "log": []}
    body = re.sub(r"<script[\s\S]*?</script>", " ", html_src or "", flags=re.I)
    flat = _text(body)
    head = re.search(r"Anni\s+(\d{1,3})(?:\s+([a-zàèéìòù/]+))?", flat, flags=re.I)
    if head:
        info["age"] = _int(head.group(1))
        role = (head.group(2) or "").casefold()
        if any(role.startswith(r) or r in role for r in _PB_ROLES):
            info["role"] = head.group(2)
    totals = re.search(r"(\d+)\s+Punti\s+([\d\.,]+)\s+Media\s+(\d+)\s+Games\s+(\d+)\s+Best", flat)
    if totals:
        info["points_total"] = _int(totals.group(1))
        info["points_avg"] = _to_num(totals.group(2))
        info["games_total"] = _int(totals.group(3))
        info["best"] = _int(totals.group(4))
    table = next(
        (m.group(1) for m in re.finditer(r"<table[^>]*>([\s\S]*?)</table>", body, flags=re.I)
         if re.search(r">Partita<", m.group(1), flags=re.I)),
        None,
    )
    if table is None:
        return info
    headers = [_text(h) for h in re.findall(r"<th[^>]*>([\s\S]*?)</th>", table, flags=re.I)]
    for chunk in re.split(r"<tr[^>]*>", table, flags=re.I)[1:]:
        if "<th" in chunk.casefold():
            continue
        date_hit = re.search(r"data\s+(\d{2})/(\d{2})/(\d{4})", chunk)
        anchor = re.search(r"<a[^>]*>([\s\S]*?)</a>", chunk, flags=re.I)
        if not date_hit or not anchor:
            continue
        inner = anchor.group(1)
        opponent = _text(inner.partition("<span>")[0])
        result = re.search(r"match(?:Won|Lost)['\"]>\s*([WL])\s*</span>\]\s*(\d+-\d+)", inner)
        cells = _cells(chunk)
        row: dict[str, Any] = {
            "date": f"{date_hit.group(3)}-{date_hit.group(2)}-{date_hit.group(1)}",
            "opponent": opponent,
            "home": bool(re.search(r"title=['\"]In casa['\"]", chunk, flags=re.I)),
            "result": result.group(1) if result else None,
            "score": result.group(2) if result else None,
            "points": _int(cells[2]) if len(cells) > 2 else None,
        }
        if headers and len(cells) == len(headers):
            extra = {headers[i]: _text(cells[i]) for i in range(len(headers))
                     if headers[i] and headers[i] not in ("Partita", "Pts") and i < len(cells)
                     and _text(cells[i]) not in ("", "-")}
            if extra:
                row["stats"] = extra
        info["log"].append(row)
    info["log"].sort(key=lambda r: r["date"])
    return info


def parse_lba_sitemap(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*(https://www\.legabasket\.it/game/\d+/[^<\s]+)</loc>", xml or "")


def parse_lba_game(html_src: str, url: str) -> Optional[dict]:
    """Box completo di una gara Lega Basket dal payload __NEXT_DATA__."""
    m = re.search(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>([\s\S]*?)</script>", html_src or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    game = (((data.get("props") or {}).get("pageProps")) or {}).get("game") or {}
    match = game.get("match") or {}
    date = (match.get("match_datetime") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    scores = game.get("scores") or {}

    def rows_for(side: str) -> list[dict]:
        box = scores.get(side) or {}
        rows: list[dict] = []
        for r in box.get("rows") or []:
            surname = (r.get("player_surname") or "").title()
            rows.append({
                "player_id": r.get("player_id"),
                "player": " ".join(x for x in (surname, r.get("player_name") or "") if x),
                "number": r.get("player_num"),
                "minutes": r.get("min"),
                "points": r.get("pun"),
                "rebounds": r.get("rimbalzi_t"),
                "off_rebounds": r.get("rimbalzi_o"),
                "def_rebounds": r.get("rimbalzi_d"),
                "assists": r.get("ass"),
                "steals": r.get("palle_r"),
                "turnovers": r.get("palle_p"),
                "blocks": r.get("stoppate_sub"),
                "fouls_drawn": r.get("falli_sf"),
                "ft": f"{r.get('tl_r')}/{r.get('tl_t')}" if r.get("tl_t") is not None else None,
                "fg": f"{r.get('t2_r')}/{r.get('t2_t')}" if r.get("t2_t") is not None else None,
                "three": f"{r.get('t3_r')}/{r.get('t3_t')}" if r.get("t3_t") is not None else None,
                "value": r.get("val_lega"),
                "plus_minus": r.get("plus_minus"),
            })
        return rows

    return {
        "url": url,
        "date": date,
        "year": match.get("year"),
        "status": as_int(match.get("game_status")),
        "home": match.get("h_team_name") or "",
        "away": match.get("v_team_name") or "",
        "team_ids": {"ht": match.get("h_team_id"), "vt": match.get("v_team_id")},
        "home_score": match.get("home_final_score"),
        "away_score": match.get("visitor_final_score"),
        "prev": [{"match_id": p.get("match_id"), "team_id": p.get("team_id")}
                 for p in (game.get("prev_matches") or [])],
        "rows": {"ht": rows_for("ht"), "vt": rows_for("vt")},
    }


def _aggregate(entry: dict) -> None:
    """Totali dal log quando disponibili, altrimenti dai valori della rosa."""
    pts = [r.get("points") for r in entry.get("log", []) if r.get("points") is not None]
    if pts:
        entry["games"] = len(pts)
        entry["points"] = sum(pts)
        entry["ppg"] = round(sum(pts) / len(pts), 1)
        entry["top"] = max(pts)
    else:
        entry["games"] = None
        entry["points"] = entry.get("roster_points")
        entry["ppg"] = entry.get("roster_ppg")
        entry["top"] = None


def _new_player(item: dict) -> dict:
    profile = item.get("profile_url") or ""
    hit = re.search(r"[?&]obj=(\d+)", profile)
    entry = {
        "player_id": f"pb_{hit.group(1)}" if hit else obs_id("PB", item.get("name", "")),
        "name": item.get("name"),
        "role": item.get("role") or None,
        "birth_year": item.get("birth_year"),
        "age": item.get("age"),
        "roster_points": item.get("points"),
        "roster_ppg": item.get("ppg"),
        "profile_url": profile or None,
        "box_source": None,
        "log": [],
    }
    _aggregate(entry)
    return entry


def _lba_id(url: str) -> Optional[int]:
    hit = re.search(r"/game/(\d+)", url or "")
    return int(hit.group(1)) if hit else None


def _lba_seen_path() -> Path:
    return RAW / "lba_games_seen.json"


_LBA_NICKNAMES = {
    "nich": "nicholas", "nick": "nicholas", "mike": "michael", "bob": "robert",
    "jim": "james", "tom": "thomas", "dan": "daniel", "danny": "daniel",
    "joe": "joseph", "tony": "antonio", "greg": "gregory", "steve": "stephen",
    "chris": "christopher", "dave": "david", "rick": "richard", "ed": "edward",
    "matt": "matthew", "jeff": "jeffrey", "gregg": "gregory",
}


def _lba_keys(name: Any) -> set[str]:
    """Token significativi del nome per accostare PlayBasket e Lega Basket."""
    text = re.sub(r"\b(mc|mac|d)\s+(?=\w)", r"\1", norm_name(name))
    keys = set()
    for token in text.split():
        token = _LBA_NICKNAMES.get(token, token)
        if len(token) >= 3:
            keys.add(token)
    return keys


def _enrich_with_lba(comp_id: str, entry: dict, lcfg: dict, policy: dict, errors: list[dict]) -> None:
    """Arricchisce il log (data/opponente/punti) con il box completo Lega Basket.

    Il sitemap elenca solo alcune gare correnti: da esse si risale alla stagione
    seguendo i prev_matches pubblicati in ogni scheda gara. Solo le partite
    giocate (game_status 2) producono righe di box.
    """
    tokens = {tid: [t.casefold() for t in toks] for tid, toks in (lcfg.get("tokens") or {}).items() if toks}
    if entry["team_id"] not in tokens:
        return
    delay = float(policy.get("delay_seconds", 0.5))
    retries = int(policy.get("max_retries", 1)) + 1
    min_year = int(lcfg.get("min_year", 2026))
    seeds_n = int(lcfg.get("seeds", 12))
    max_games = int(lcfg.get("max_games", 220))
    my_tokens = tokens[entry["team_id"]]

    queue: list[int] = []
    try:
        sitemap, _ = fetch(LBA_GAMES_SITEMAP, retries=retries)
        cands = [u for u in parse_lba_sitemap(sitemap) if any(t in u.casefold() for t in my_tokens)]
        cands.sort(key=lambda u: _lba_id(u) or 0, reverse=True)
        for u in cands[:seeds_n]:
            gid = _lba_id(u)
            if gid:
                queue.append(gid)
    except Exception as exc:  # noqa: BLE001
        errors.append({"competition_id": comp_id, "phase": "players_lba", "url": LBA_GAMES_SITEMAP, "message": str(exc)})
    time.sleep(delay)

    visited: set[int] = set()
    our_ids: set[int] = set()
    kept: set[int] = set()
    boxes: list[dict] = []
    found_any = False

    def _drain() -> None:
        nonlocal found_any
        head = 0
        while head < len(queue) and len(visited) < max_games:
            gid = queue[head]
            head += 1
            if gid in visited:
                continue
            visited.add(gid)
            url = f"https://www.legabasket.it/game/{gid}"
            try:
                page, transport = fetch(url, retries=retries)
            except Exception as exc:  # noqa: BLE001
                errors.append({"competition_id": comp_id, "phase": "players_lba", "url": url, "message": str(exc)})
                continue
            time.sleep(delay)
            game = parse_lba_game(page, url)
            if not game or (game.get("year") or 0) < min_year:
                continue
            home_hit = any(t in game["home"].casefold() for t in my_tokens)
            away_hit = any(t in game["away"].casefold() for t in my_tokens)
            side = "ht" if home_hit else ("vt" if away_hit else None)
            if side is None:
                continue
            save_snapshot(comp_id, f"lba_game_{gid}", page, url, transport)
            kept.add(gid)
            found_any = True
            tid = game["team_ids"].get(side)
            if tid:
                our_ids.add(tid)
            if game.get("status") == 2:
                opponent = game["away"] if side == "ht" else game["home"]
                for row in game["rows"][side]:
                    if not row.get("minutes"):
                        continue  # non giocata: nessuna riga di box
                    box_row = dict(row)
                    box_row.update({"date": game["date"], "opponent": opponent, "home": side == "ht"})
                    boxes.append(box_row)
            for prev in game.get("prev") or []:
                pid, ptid = prev.get("match_id"), prev.get("team_id")
                if pid and ptid in our_ids and pid not in visited and pid not in queue[head:]:
                    queue.append(pid)

    _drain()
    if not found_any:
        cached = [i for i in read_json(_lba_seen_path(), []) if isinstance(i, int) and i not in visited]
        if cached:
            queue = cached
            _drain()
    if kept:
        write_json(_lba_seen_path(), sorted(set(read_json(_lba_seen_path(), [])) | kept))
    if not boxes:
        return
    entry["box_source"] = "LEGA_BASKET"
    by_player: dict[str, list[dict]] = {}
    for row in boxes:
        by_player.setdefault(norm_name(row.get("player")), []).append(row)

    claimed: set[str] = set()

    def _match(player_key: str) -> Optional[str]:
        keys = _lba_keys(player_key)
        if not keys:
            return None
        best, best_score = None, 0
        for item in entry["roster"]:
            roster_key = norm_name(item.get("name"))
            if not roster_key or roster_key in claimed:
                continue
            pkeys = _lba_keys(item.get("name"))
            common = keys & pkeys
            if not common or not (keys <= pkeys or pkeys <= keys or len(common) >= 2):
                continue
            if len(common) > best_score:
                best, best_score = roster_key, len(common)
        if best:
            claimed.add(best)
        return best

    leftovers: dict[str, list[dict]] = {}
    for player_key, rows in by_player.items():
        leftovers.setdefault(_match(player_key) or player_key, []).extend(rows)

    for item in entry["roster"]:
        rows = leftovers.pop(norm_name(item.get("name")), None)
        if not rows:
            continue
        item["box_source"] = "LEGA_BASKET"
        known = {r["date"] for r in item["log"]}
        by_date: dict[str, dict] = {}
        for row in rows:
            by_date.setdefault(row["date"], row)
        for log_row in item["log"]:
            box_row = by_date.get(log_row["date"])
            if box_row:
                for key, value in box_row.items():
                    if key in ("date", "opponent", "home", "player", "player_id") or value is None:
                        continue
                    log_row[key] = value
        for date_key in sorted(by_date):
            if date_key in known:
                continue
            box_row = by_date[date_key]
            extra = {k: v for k, v in box_row.items()
                     if k not in ("date", "opponent", "home", "player", "player_id") and v is not None}
            item["log"].append({
                "date": date_key, "opponent": box_row["opponent"], "home": box_row["home"],
                "result": None, "score": None, "points": box_row.get("points"), **extra,
            })
        item["log"].sort(key=lambda r: r["date"])
        _aggregate(item)
    # giocatori presenti solo nel box (non censiti nella rosa PlayBasket)
    for player_key, rows in leftovers.items():
        if not player_key or not rows:
            continue
        by_date = {}
        for row in rows:
            by_date.setdefault(row["date"], row)
        item = {
            "player_id": "lba_" + obs_id("LBA", comp_id, player_key),
            "name": rows[0].get("player"),
            "role": None, "birth_year": None, "age": None,
            "roster_points": None, "roster_ppg": None, "profile_url": None,
            "box_source": "LEGA_BASKET",
            "log": [
                {
                    "date": date_key, "opponent": by_date[date_key]["opponent"], "home": by_date[date_key]["home"],
                    "result": None, "score": None, "points": by_date[date_key].get("points"),
                    **{k: v for k, v in by_date[date_key].items()
                       if k not in ("date", "opponent", "home", "player", "player_id") and v is not None},
                }
                for date_key in sorted(by_date)
            ],
        }
        _aggregate(item)
        entry["roster"].append(item)
    entry["roster"].sort(key=lambda p: (p.get("name") or "").casefold())



def collect_players(config: dict, policy: dict, errors: list[dict], only: Optional[str] = None) -> dict:
    """Acquisisce rosa e statistiche per ogni squadra seguita.

    Flusso: pagina squadre (mod=tm) -> rosa del club -> scheda giocatore
    (mod=st, game log). Per l'A1 il game log e' arricchito con il box completo
    della Lega Basket. Le competizioni non ancora pubblicate su PlayBasket
    restituiscono status 'no_data' (nessun dato inventato).
    """
    delay = float(policy.get("delay_seconds", 0.5))
    retries = int(policy.get("max_retries", 1)) + 1
    payload: dict[str, Any] = {"generated_at": now(), "teams": {}}
    for comp in config.get("competitions", []):
        if not comp.get("enabled", True):
            continue
        if only and comp["competition_id"] != only:
            continue
        pcfg = comp.get("players")
        if not pcfg:
            continue
        comp_id = comp["competition_id"]
        tracked = comp.get("tracked_teams", [])
        for t in tracked:
            payload["teams"][t["team_id"]] = {
                "team_id": t["team_id"],
                "competition_id": comp_id,
                "competition_name": comp.get("name", comp_id),
                "season_id": comp.get("season_id"),
                "club": t.get("canonical_name"),
                "status": "no_data",
                "reason": "acquisizione non eseguita",
                "source": pcfg.get("provider", "PLAYBASKET"),
                "source_url": pcfg.get("league_url"),
                "box_source": None,
                "generated_at": now(),
                "roster": [],
            }
        league_url = pcfg.get("league_url")
        if not league_url:
            continue
        log.info("Statistiche giocatori %s", comp_id)
        try:
            page, transport = fetch(league_url, retries=retries)
            save_snapshot(comp_id, "players_teams", page, league_url, transport)
        except Exception as exc:  # noqa: BLE001
            errors.append({"competition_id": comp_id, "phase": "players", "url": league_url, "message": str(exc)})
            log.error("Elenco squadre %s fallito: %s", comp_id, exc)
            for t in tracked:
                payload["teams"][t["team_id"]]["reason"] = f"fonte non raggiungibile ({exc})"
            time.sleep(delay)
            continue
        time.sleep(delay)
        published = parse_pb_teams(page)
        for t in tracked:
            tid = t["team_id"]
            entry = payload["teams"][tid]
            match_tokens = (pcfg.get("team_match") or {}).get(tid) or [t.get("canonical_name", "")]
            tokens = [x.casefold() for x in match_tokens if x]
            match = next((p for p in published if all(tok in p["club"].casefold() for tok in tokens if tok)), None)
            if match is None:
                entry["reason"] = (
                    "competizione non ancora pubblicata su PlayBasket"
                    if not published else "squadra non presente nell'elenco PlayBasket"
                )
                log.warning("%s: %s", tid, entry["reason"])
                continue
            entry["club_name"] = match["club"]
            entry["source_url"] = match["roster_url"]
            try:
                roster_html, transport = fetch(match["roster_url"], retries=retries)
                save_snapshot(comp_id, f"players_roster_{tid}", roster_html, match["roster_url"], transport)
            except Exception as exc:  # noqa: BLE001
                errors.append({"competition_id": comp_id, "phase": "players", "url": match["roster_url"], "message": str(exc)})
                entry["reason"] = f"rosa non recuperabile ({exc})"
                time.sleep(delay)
                continue
            time.sleep(delay)
            roster = parse_pb_roster(roster_html)
            if not roster:
                entry["reason"] = "rosa non pubblicata su PlayBasket"
                continue
            entry["status"] = "ok"
            entry["reason"] = None
            for item in roster:
                player = _new_player(item)
                if not item.get("no_presence"):
                    url = pb_profile_url(item["profile_url"])
                    try:
                        profile_html, _ = fetch(url, retries=retries)
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"competition_id": comp_id, "phase": "players", "url": url, "message": str(exc)})
                        time.sleep(delay)
                        entry["roster"].append(player)
                        continue
                    time.sleep(delay)
                    profile = parse_pb_profile(profile_html)
                    if profile["log"]:
                        player["log"] = profile["log"]
                    player["age"] = player["age"] or profile.get("age")
                    player["role"] = player["role"] or profile.get("role")
                    _aggregate(player)
                entry["roster"].append(player)
            entry["roster"].sort(key=lambda p: (p.get("name") or "").casefold())
            if pcfg.get("lba"):
                _enrich_with_lba(comp_id, entry, pcfg["lba"], policy, errors)
                time.sleep(delay)
    return payload


# --------------------------------------------------------------------------- #
# Scansione
# --------------------------------------------------------------------------- #
@dataclass
class ScanResult:
    observations: list[dict] = field(default_factory=list)
    standings: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def scan_fip(comp: dict, policy: dict, parser: str, limit: Optional[int]) -> ScanResult:
    result = ScanResult()
    rounds = comp["scan"]["configured_rounds"]
    legs = [
        ("ANDATA", comp["scan"].get("andata_code", "1"), int(rounds["andata"])),
        ("RITORNO", comp["scan"].get("ritorno_code", "0"), int(rounds["ritorno"])),
    ]
    for leg, code, count in legs:
        if limit:
            count = min(count, limit)
        for number in range(1, count + 1):
            url = fip_url(comp, code, number)
            record = {"leg": leg, "round": number, "url": url, "status": "STARTED"}
            try:
                html, transport = fetch(url, retries=int(policy.get("max_retries", 1)) + 1)
                snapshot = save_snapshot(comp["competition_id"], f"{leg.lower()}_giornata_{number:02d}", html, url, transport)
                parsed = parse_fip_round(html, comp, leg, number, url, parser)
                tracked = [o for o in parsed if o["tracked_team_ids"]]
                result.observations.extend(tracked)
                if not result.standings:
                    result.standings = parse_fip_standings(html)
                record.update({
                    "status": "PARSED" if tracked else "EMPTY",
                    "observations": len(tracked),
                    "snapshot": snapshot,
                    "transport": transport,
                })
            except Exception as exc:  # noqa: BLE001
                record.update({"status": "FAILED", "error": str(exc)})
                result.errors.append({"competition_id": comp["competition_id"], "leg": leg, "round": number, "message": str(exc)})
                log.error("FIP %s giornata %s fallita: %s", leg, number, exc)
            result.pages.append(record)
            time.sleep(float(policy.get("delay_seconds", 0)))
    return result


def scan_single_url(comp: dict, policy: dict, parser: str, parse_single: Any) -> ScanResult:
    result = ScanResult()
    url = comp["url"]
    record = {"url": url, "status": "STARTED"}
    try:
        raw, transport = fetch(url, retries=int(policy.get("max_retries", 1)) + 1)
        snapshot = save_snapshot(comp["competition_id"], "competition_calendar", raw, url, transport)
        parsed = parse_single(raw, comp, url, parser)
        result.observations.extend(parsed)
        record.update({
            "status": "PARSED" if parsed else "EMPTY",
            "observations": len(parsed),
            "snapshot": snapshot,
            "transport": transport,
        })
    except Exception as exc:  # noqa: BLE001
        record.update({"status": "FAILED", "error": str(exc)})
        result.errors.append({"competition_id": comp["competition_id"], "message": str(exc)})
        log.error("%s fallito: %s", comp["provider"], exc)
    result.pages.append(record)
    return result


_SINGLE_URL_PARSERS = {
    "PLAYBASKET": parse_playbasket,
    "LEGA_SERIE_A": parse_legaseriea,
}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def players_summary(players: dict) -> dict:
    teams = players.get("teams", {})
    return {
        "players_teams": len(teams),
        "players_with_data": sum(1 for t in teams.values() if t.get("status") == "ok"),
        "players_roster": sum(len(t.get("roster", [])) for t in teams.values()),
        "players_games": sum(len(p.get("log", [])) for t in teams.values() for p in t.get("roster", [])),
    }


def run_players_only(config: dict, policy: dict, only: Optional[str] = None) -> dict:
    """Solo statistiche giocatori: aggiorna l'output esistente senza ri-scansionare."""
    errors: list[dict] = []
    players = collect_players(config, policy, errors, only=only)
    out_path = OUT / "sc_portal_data.json"
    data = read_json(out_path, None)
    if data is None:
        raise RuntimeError("output sc_portal_data.json assente: eseguire prima la scansione completa")
    data["players"] = players
    summary = data.setdefault("sync_summary", {})
    summary.update(players_summary(players))
    kept = [e for e in (data.get("errors") or []) if e.get("phase") not in ("players", "players_lba")]
    data["errors"] = kept + errors
    write_json(out_path, data)
    write_json(LOG / "last_players_sync.json", {
        "engine_version": f"{ENGINE_VERSION} Players",
        "completed_at": now(),
        "status": "COMPLETED" if not errors else "PARTIAL",
        "summary": summary,
        "errors": errors,
    })
    log.info("Giocatori: %d squadre | %d con dati | %d atleti", summary["players_teams"],
             summary["players_with_data"], summary["players_roster"])
    log.info("Errori giocatori: %d", len(errors))
    return summary


def run(config_path: Path, only: Optional[str], limit: Optional[int], parser: str,
        skip_players: bool = False, players_only: bool = False) -> dict:
    for folder in (RAW, OBS, OUT, LOG):
        folder.mkdir(parents=True, exist_ok=True)

    config = read_json(config_path, {"competitions": [], "request_policy": {}})
    policy = config.get("request_policy", {})
    if players_only:
        return run_players_only(config, policy, only)
    old = read_json(OBS / "observations.json", [])

    incoming: list[dict] = []
    reports: list[dict] = []
    errors: list[dict] = []
    results_by_comp: dict[str, ScanResult] = {}
    for comp in config.get("competitions", []):
        if not comp.get("enabled", True):
            continue
        if only and comp["competition_id"] != only:
            continue
        log.info("Scansione %s (%s)", comp["competition_id"], comp["provider"])
        if comp["provider"] == "FIP":
            result = scan_fip(comp, policy, parser, limit)
        elif (parse_fn := _SINGLE_URL_PARSERS.get(comp["provider"])) is not None:
            result = scan_single_url(comp, policy, parser, parse_fn)
        else:
            log.error("Provider %s non supportato", comp["provider"])
            errors.append({"competition_id": comp["competition_id"], "message": f"provider non supportato: {comp['provider']}"})
            continue
        incoming.extend(result.observations)
        results_by_comp[comp["competition_id"]] = result
        errors.extend(result.errors)
        reports.append({
            "competition_id": comp["competition_id"],
            "provider": comp["provider"],
            "pages": result.pages,
            "observations": len(result.observations),
            "errors": len(result.errors),
        })

    merged = {x["observation_id"]: x for x in old}
    valid_sources = {(c["competition_id"], c["provider"]) for c in config.get("competitions", []) if c.get("enabled", True)}
    merged = {k: v for k, v in merged.items() if (v.get("competition_id"), v.get("provider")) in valid_sources}
    new_count = updated = 0
    for observation in incoming:
        key = observation["observation_id"]
        if key not in merged:
            new_count += 1
        elif merged[key] != observation:
            updated += 1
        merged[key] = observation
    observations = sorted(merged.values(), key=lambda x: (x.get("date") or "", x.get("time") or "", x.get("home") or ""))
    write_json(OBS / "observations.json", observations)

    matches = []
    for observation in observations:
        match = dict(observation)
        match["match_id"] = "obs_" + observation["observation_id"]
        match["verification_status"] = "OFFICIAL" if observation["provider"] in ("FIP", "LEGA_SERIE_A") else "SECONDARY"
        matches.append(match)

    standings = build_standings(config, results_by_comp, errors)
    team_logos = build_team_logos(config, matches)
    players = {} if skip_players else collect_players(config, policy, errors, only=only)

    summary = {
        "competitions": len(reports),
        "pages_requested": sum(len(r["pages"]) for r in reports),
        "pages_completed": sum(1 for r in reports for p in r["pages"] if p["status"] in ("PARSED", "EMPTY")),
        "new_matches": new_count,
        "updated_matches": updated,
        "total_matches": len(matches),
        "errors": len(errors),
        **players_summary(players),
    }
    transport = "REQUESTS" if requests is not None else ("URLLIB" if python_https_available() else "POWERSHELL")
    write_json(OUT / "sc_portal_data.json", {
        "schema_version": ENGINE_VERSION,
        "engine_version": f"{ENGINE_VERSION} Full Season",
        "generated_at": now(),
        "parser": parser,
        "transport": transport,
        "sync_summary": summary,
        "matches": matches,
        "standings": standings,
        "team_logos": team_logos,
        "players": players,
        "errors": errors,
    })
    write_json(LOG / "last_sync.json", {
        "engine_version": f"{ENGINE_VERSION} Full Season",
        "completed_at": now(),
        "status": "COMPLETED" if not errors else "PARTIAL",
        "python_https_transport": transport,
        "summary": summary,
        "competitions": reports,
        "errors": errors,
    })

    log.info("Trasporto HTTPS: %s", transport)
    log.info("Pagine completate: %d / %d", summary["pages_completed"], summary["pages_requested"])
    log.info("Nuove gare: %d | Aggiornate: %d | Totale: %d", new_count, updated, len(matches))
    log.info("Errori: %d", len(errors))
    log.info("Output: %s", OUT / "sc_portal_data.json")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SC Portal Data Engine")
    parser.add_argument("--config", type=Path, default=CFG_PATH, help="file di configurazione competizioni")
    parser.add_argument("--competition", help="scansiona solo questa competition_id")
    parser.add_argument("--limit-rounds", type=int, help="limita il numero di giornate per lato (debug)")
    parser.add_argument("--parser", choices=("regex", "bs4"), default="regex", help="motore di estrazione testo")
    parser.add_argument("--skip-players", action="store_true", help="non acquisire le statistiche giocatori")
    parser.add_argument("--players-only", action="store_true", help="aggiorna solo le statistiche giocatori sull'output esistente")
    parser.add_argument("--verbose", action="store_true", help="log dettagliato")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("SC Portal Data Engine %s - Full Season Acquisition", ENGINE_VERSION)
    try:
        run(
            args.config,
            args.competition,
            args.limit_rounds,
            args.parser,
            skip_players=args.skip_players,
            players_only=args.players_only,
        )
    except KeyboardInterrupt:
        log.error("Interrotto dall'utente.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.exception("Errore fatale: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
