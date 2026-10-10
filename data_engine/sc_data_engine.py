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
        home_score = m.get("homeScorePush")
        away_score = m.get("awayScorePush")
        if home_score is None:
            home_score = m.get("providerHomeScore")
        if away_score is None:
            away_score = m.get("providerAwayScore")
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
            "status": "FINISHED" if home_score is not None and away_score is not None else "SCHEDULED",
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
def run(config_path: Path, only: Optional[str], limit: Optional[int], parser: str) -> dict:
    for folder in (RAW, OBS, OUT, LOG):
        folder.mkdir(parents=True, exist_ok=True)

    config = read_json(config_path, {"competitions": [], "request_policy": {}})
    policy = config.get("request_policy", {})
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

    summary = {
        "competitions": len(reports),
        "pages_requested": sum(len(r["pages"]) for r in reports),
        "pages_completed": sum(1 for r in reports for p in r["pages"] if p["status"] in ("PARSED", "EMPTY")),
        "new_matches": new_count,
        "updated_matches": updated,
        "total_matches": len(matches),
        "errors": len(errors),
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
        run(args.config, args.competition, args.limit_rounds, args.parser)
    except KeyboardInterrupt:
        log.error("Interrotto dall'utente.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.exception("Errore fatale: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
