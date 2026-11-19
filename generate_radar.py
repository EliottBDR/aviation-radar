#!/usr/bin/env python3
"""
generate_radar.py - Génère aviation-stats.svg : un radar ATC animé (SVG pur,
sans JavaScript) piloté par les statistiques GitHub d'un utilisateur.

Variables d'environnement :
  GITHUB_TOKEN   (obligatoire) token GitHub (GITHUB_TOKEN d'Actions ou PAT)
  USERNAME       (obligatoire) login GitHub à représenter
  OUTPUT_PATH    (optionnel)   défaut : aviation-stats.svg
  MAX_AIRCRAFT   (optionnel)   défaut : 5  (1 à 8)
  ROTATION_SECONDS (optionnel) défaut : 10 (4 à 30)

Mapping des données :
  - un aéronef = un dépôt public, non-fork, non-archivé (hors dépôt de profil)
  - distance au centre = ancienneté du dernier commit (échelle logarithmique)
  - couleur = langage principal du dépôt
  - callsign = 3 lettres du nom du dépôt + 3 chiffres dérivés d'un hash
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import requests

API_URL = "https://api.github.com/graphql"
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 4
MAX_WAIT_S = 120          # attente maximale acceptée en cas de rate limit
REPO_SCAN_LIMIT = 100     # nombre de dépôts examinés (les plus récemment poussés)
FALLBACK_COLOR = "#8b949e"

# Palette radar
BG = "#0a0f14"
PANEL_BG = "#0d1a14"
GRID = "#1d5c3a"
EDGE = "#16402b"
PHOS = "#3dff9a"
TEXT = "#a7f3c8"
DIM = "#4f8f6b"

FONT_STACK = (
    "ui-monospace, SFMono-Regular, Menlo, Consolas, "
    "'Liberation Mono', 'Courier New', monospace"
)

log = logging.getLogger("radar")

QUERY = """
query($login: String!, $count: Int!) {
  user(login: $login) {
    login
    name
    followers { totalCount }
    contributionsCollection {
      contributionCalendar { totalContributions }
    }
    repositories(
      first: $count
      ownerAffiliations: OWNER
      privacy: PUBLIC
      isFork: false
      orderBy: {field: PUSHED_AT, direction: DESC}
    ) {
      totalCount
      nodes {
        name
        isArchived
        stargazerCount
        pushedAt
        primaryLanguage { name color }
        defaultBranchRef {
          target { ... on Commit { committedDate } }
        }
      }
    }
  }
  rateLimit { remaining resetAt cost }
}
"""


# --------------------------------------------------------------------------
# Erreurs et configuration
# --------------------------------------------------------------------------
class ConfigError(Exception):
    pass


class GitHubError(RuntimeError):
    pass


def _env_number(name: str, default, cast, low, high):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} n'est pas un nombre valide.") from None
    return max(low, min(high, value))


@dataclass(frozen=True)
class Config:
    token: str
    username: str
    output: Path
    max_aircraft: int
    rotation_s: float

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        username = os.environ.get("USERNAME", "").strip()
        problems = []
        if not token:
            problems.append("GITHUB_TOKEN est vide ou absent")
        if not username:
            problems.append("USERNAME est vide ou absent")
        elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", username):
            problems.append(
                f"USERNAME={username!r} n'est pas un login GitHub valide "
                "(sous Windows, USERNAME contient par défaut le nom de session : "
                "redéfinis-le)"
            )
        if problems:
            raise ConfigError("; ".join(problems))
        return cls(
            token=token,
            username=username,
            output=Path(os.environ.get("OUTPUT_PATH", "aviation-stats.svg")),
            max_aircraft=_env_number("MAX_AIRCRAFT", 5, int, 1, 8),
            rotation_s=_env_number("ROTATION_SECONDS", 10.0, float, 4.0, 30.0),
        )


# --------------------------------------------------------------------------
# Modèle de données
# --------------------------------------------------------------------------
@dataclass
class Aircraft:
    repo: str
    callsign: str
    language: str
    color: str
    age_days: float
    stars: int


@dataclass
class Profile:
    login: str
    followers: int
    public_repos: int
    stars: int
    contributions: int


# --------------------------------------------------------------------------
# Accès API GitHub (GraphQL) avec gestion du rate limit
# --------------------------------------------------------------------------
def _rate_limit_wait(resp: requests.Response) -> float | None:
    """Retourne le délai d'attente en secondes si la réponse est un rate limit,
    None si le 403/429 a une autre cause (permissions, etc.)."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return float(retry_after) + 1
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return max(0.0, float(reset) - time.time()) + 1
        return 60.0
    if "rate limit" in resp.text.lower():
        return 60.0
    return None


def _backoff(attempt: int, reason: str) -> None:
    delay = min(2 ** attempt, 30)
    log.warning("%s - nouvelle tentative dans %ss (%d/%d)", reason, delay, attempt, MAX_RETRIES)
    time.sleep(delay)


def graphql(session: requests.Session, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    last_error = "erreur inconnue"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(
                API_URL,
                json={"query": query, "variables": variables},
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            last_error = f"erreur réseau : {exc}"
            _backoff(attempt, last_error)
            continue

        if resp.status_code == 401:
            raise GitHubError("401 : token refusé (GITHUB_TOKEN invalide ou expiré).")

        if resp.status_code in (403, 429):
            wait = _rate_limit_wait(resp)
            if wait is None:
                raise GitHubError(f"HTTP {resp.status_code} : accès refusé ({resp.text[:200]}).")
            if wait > MAX_WAIT_S:
                raise GitHubError(
                    f"Rate limit atteint, réinitialisation dans {wait:.0f}s "
                    f"(> {MAX_WAIT_S}s). Réessaie plus tard."
                )
            last_error = "rate limit"
            log.warning("Rate limit : attente de %.0fs", wait)
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code} côté GitHub"
            _backoff(attempt, last_error)
            continue

        if resp.status_code != 200:
            raise GitHubError(f"HTTP {resp.status_code} : {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError:
            last_error = "réponse JSON invalide"
            _backoff(attempt, last_error)
            continue

        errors = payload.get("errors") or []
        data = payload.get("data")
        if errors:
            types = {e.get("type") for e in errors if isinstance(e, dict)}
            messages = "; ".join(str(e.get("message", e)) for e in errors if isinstance(e, dict))
            if "RATE_LIMITED" in types:
                last_error = "rate limit GraphQL"
                log.warning("Rate limit GraphQL : attente de %ss", 30 * attempt)
                time.sleep(30 * attempt)
                continue
            if "NOT_FOUND" in types and not (data or {}).get("user"):
                raise GitHubError(f"Utilisateur {variables.get('login')!r} introuvable.")
            if not data:
                raise GitHubError(f"Erreur GraphQL : {messages}")
            log.warning("Réponse partielle de l'API : %s", messages)

        if not isinstance(data, dict):
            last_error = "champ 'data' absent"
            _backoff(attempt, last_error)
            continue
        return data

    raise GitHubError(f"Échec après {MAX_RETRIES} tentatives ({last_error}).")


# --------------------------------------------------------------------------
# Extraction / normalisation des données (tolérante aux champs manquants)
# --------------------------------------------------------------------------
def _dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sanitize_color(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return FALLBACK_COLOR


def _hash(text: str, salt: str) -> int:
    return int(hashlib.sha1(f"{salt}:{text.lower()}".encode("utf-8")).hexdigest()[:8], 16)


def make_callsign(repo_name: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", repo_name).upper()
    prefix = (letters + "XXX")[:3]
    number = _hash(repo_name, "callsign") % 900 + 100
    return f"{prefix}{number}"


def parse_snapshot(data: dict[str, Any], cfg: Config) -> tuple[Profile, list[Aircraft]]:
    user = data.get("user")
    if not isinstance(user, dict):
        raise GitHubError(f"Utilisateur {cfg.username!r} introuvable.")

    login = user.get("login") or cfg.username
    nodes = _dig(user, "repositories", "nodes") or []
    now = datetime.now(timezone.utc)

    total_stars = 0
    candidates: list[tuple[datetime, str, str, str, int]] = []
    for node in nodes:
        if not isinstance(node, dict) or not node.get("name"):
            continue
        name = node["name"]
        stars = _as_int(node.get("stargazerCount"))
        total_stars += stars
        # Le dépôt de profil est mis à jour par le bot toutes les 6 h :
        # il serait toujours le plus récent, donc on l'exclut.
        if node.get("isArchived") or name.lower() == login.lower():
            continue
        last = (
            parse_iso(_dig(node, "defaultBranchRef", "target", "committedDate"))
            or parse_iso(node.get("pushedAt"))
        )
        if last is None:  # dépôt vide
            continue
        candidates.append((
            last,
            name,
            _dig(node, "primaryLanguage", "name") or "N/A",
            sanitize_color(_dig(node, "primaryLanguage", "color")),
            stars,
        ))

    candidates.sort(key=lambda c: c[0], reverse=True)
    aircraft = [
        Aircraft(
            repo=name,
            callsign=make_callsign(name),
            language=lang,
            color=color,
            age_days=max(0.0, (now - last).total_seconds() / 86400),
            stars=stars,
        )
        for last, name, lang, color, stars in candidates[: cfg.max_aircraft]
    ]

    profile = Profile(
        login=login,
        followers=_as_int(_dig(user, "followers", "totalCount")),
        public_repos=_as_int(_dig(user, "repositories", "totalCount")),
        stars=total_stars,  # somme sur les REPO_SCAN_LIMIT dépôts examinés
        contributions=_as_int(
            _dig(user, "contributionsCollection", "contributionCalendar", "totalContributions")
        ),
    )
    return profile, aircraft


def fetch_snapshot(cfg: Config) -> tuple[Profile, list[Aircraft]]:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"bearer {cfg.token}",
        "User-Agent": "aviation-stats-radar/1.0",
        "Accept": "application/json",
    })
    data = graphql(session, QUERY, {"login": cfg.username, "count": REPO_SCAN_LIMIT})
    rl = data.get("rateLimit") or {}
    log.info("Rate limit restant : %s (coût de la requête : %s)", rl.get("remaining", "?"), rl.get("cost", "?"))
    return parse_snapshot(data, cfg)


# --------------------------------------------------------------------------
# Géométrie et formatage
# --------------------------------------------------------------------------
def age_to_radius(age_days: float, r_min: float, r_max: float) -> float:
    """Échelle logarithmique : 0 jour -> r_min, 365 jours et plus -> r_max."""
    frac = math.log1p(max(age_days, 0.0)) / math.log1p(365.0)
    return r_min + min(1.0, frac) * (r_max - r_min)


def polar(cx: float, cy: float, radius: float, bearing_deg: float) -> tuple[float, float]:
    """Relèvement en degrés, 0 = nord, sens horaire."""
    a = math.radians(bearing_deg)
    return cx + radius * math.sin(a), cy - radius * math.cos(a)


def fmt_age(days: float) -> str:
    hours = days * 24
    if hours < 1:
       return "&lt;1H"
    if hours < 48:
        return f"{int(hours)}H"
    if days < 60:
        return f"{int(days)}D"
    if days < 730:
        return f"{int(days / 30.44)}MO"
    return f"{int(days / 365.25)}Y"


def compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def attr_escape(text: str) -> str:
    return escape(text, {'"': "&quot;"})


# --------------------------------------------------------------------------
# Génération du SVG
# --------------------------------------------------------------------------
STYLE = """
.t{font-family:inherit}
.dim{fill:%(dim)s}
.txt{fill:%(txt)s}
.hi{fill:%(hi)s}
.strip{animation:strip-in .9s ease-out both}
@keyframes strip-in{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:translateX(0)}}
.live{animation:blink 1.6s ease-in-out infinite}
@keyframes blink{0%%,55%%{opacity:1}56%%,100%%{opacity:.15}}
""" % {"dim": DIM, "txt": TEXT, "hi": PHOS}


def beam_slices(cx: float, cy: float, radius: float, steps: int = 22,
                step_deg: float = 2.0, peak: float = 0.30) -> list[str]:
    """Traînée du balayage : secteurs successifs d'opacité décroissante,
    placés derrière la tête du faisceau (qui pointe au nord avant rotation)."""
    out = []
    for k in range(steps):
        x1, y1 = polar(cx, cy, radius, -(k + 1) * step_deg)
        x2, y2 = polar(cx, cy, radius, -k * step_deg + 0.4)  # léger recouvrement
        opacity = peak * (1 - k / steps) ** 2
        out.append(
            f'<path d="M{cx} {cy} L{x1:.2f} {y1:.2f} A{radius} {radius} 0 0 1 {x2:.2f} {y2:.2f} Z" '
            f'fill="{PHOS}" fill-opacity="{opacity:.3f}"/>'
        )
    return out


def build_svg(profile: Profile, aircraft: list[Aircraft], cfg: Config, now: datetime) -> str:
    W, H = 880, 500
    CX, CY, R = 250, 262, 196
    R_MIN, R_MAX = 26, R - 12
    T = cfg.rotation_s
    dur = f"{T:.2f}s"
    stamp = now.strftime("%Y-%m-%d %H:%MZ")

    label = f"Radar ATC animé de {profile.login} : {len(aircraft)} dépôts récents"
    p: list[str] = []
    p.append('<?xml version="1.0" encoding="UTF-8"?>')
    p.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-label="{attr_escape(label)}" '
        f'font-family="{FONT_STACK}">'
    )
    p.append(f"<title>{escape(label)}</title>")
    p.append(f"<style>{STYLE}</style>")
    p.append(f"""<defs>
<filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
<feGaussianBlur stdDeviation="2.2" result="b"/>
<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
</filter>
<radialGradient id="vignette" cx="28%" cy="52%" r="75%">
<stop offset="0" stop-color="#0f2a1d" stop-opacity="0.55"/>
<stop offset="1" stop-color="{BG}" stop-opacity="0"/>
</radialGradient>
<pattern id="scan" width="4" height="4" patternUnits="userSpaceOnUse">
<rect width="4" height="1" fill="#000" fill-opacity="0.25"/>
</pattern>
</defs>""")

    # Fond
    p.append(f'<rect width="{W}" height="{H}" rx="14" fill="{BG}" stroke="{EDGE}"/>')
    p.append(f'<rect width="{W}" height="{H}" rx="14" fill="url(#vignette)"/>')

    # En-tête
    p.append(f'<circle class="live" cx="30" cy="29" r="4" fill="{PHOS}" filter="url(#glow)"/>')
    p.append(f'<text x="42" y="34" font-size="13" font-weight="bold" class="hi">'
             f'ATC RADAR // @{escape(profile.login)}</text>')
    p.append(f'<text x="{W - 24}" y="34" font-size="10" text-anchor="end" class="dim">'
             f'UPDATED {stamp}</text>')
    p.append(f'<line x1="24" y1="46" x2="{W - 24}" y2="46" stroke="{EDGE}"/>')

    # Radar : fond, anneaux, graduations
    p.append(f'<circle cx="{CX}" cy="{CY}" r="{R}" fill="#07110c" stroke="{GRID}" stroke-width="1.5"/>')
    for days, text in ((1, "24H"), (7, "7D"), (30, "30D"), (90, "90D"), (365, "1Y")):
        r = age_to_radius(days, R_MIN, R_MAX)
        p.append(f'<circle cx="{CX}" cy="{CY}" r="{r:.1f}" fill="none" stroke="{GRID}" '
                 f'stroke-width="0.8" stroke-dasharray="2 4" opacity="0.8"/>')
        p.append(f'<text x="{CX + 3}" y="{CY - r - 2:.1f}" font-size="8" class="dim">{text}</text>')
    p.append(f'<line x1="{CX - R}" y1="{CY}" x2="{CX + R}" y2="{CY}" stroke="{GRID}" stroke-width="0.8"/>')
    p.append(f'<line x1="{CX}" y1="{CY - R}" x2="{CX}" y2="{CY + R}" stroke="{GRID}" stroke-width="0.8"/>')

    cardinals = {0: "N", 90: "E", 180: "S", 270: "W"}
    for deg in range(0, 360, 10):
        major = deg % 30 == 0
        x1, y1 = polar(CX, CY, R, deg)
        x2, y2 = polar(CX, CY, R - (8 if major else 4), deg)
        p.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                 f'stroke="{GRID}" stroke-width="{1.2 if major else 0.7}"/>')
        if major:
            lx, ly = polar(CX, CY, R + 13, deg)
            txt = cardinals.get(deg, f"{deg:03d}")
            p.append(f'<text x="{lx:.1f}" y="{ly:.1f}" dy="0.35em" font-size="9" '
                     f'text-anchor="middle" class="dim">{txt}</text>')

    # Balayage
    p.append("<g>")
    p.append(f'<animateTransform attributeName="transform" type="rotate" '
             f'from="0 {CX} {CY}" to="360 {CX} {CY}" dur="{dur}" repeatCount="indefinite"/>')
    p.extend(beam_slices(CX, CY, R))
    p.append(f'<line x1="{CX}" y1="{CY}" x2="{CX}" y2="{CY - R}" stroke="{PHOS}" '
             f'stroke-width="1.6" filter="url(#glow)"/>')
    p.append("</g>")

    # Centre
    p.append(f'<rect x="{CX - 2.5}" y="{CY - 2.5}" width="5" height="5" fill="{PHOS}" filter="url(#glow)"/>')

    # Aéronefs
    n = len(aircraft)
    offset = _hash(profile.login, "offset") % 360
    for i, ac in enumerate(aircraft):
        jitter = (_hash(ac.repo, "jitter") % 41) - 20
        bearing = (offset + i * 360.0 / n + jitter) % 360
        radius = age_to_radius(ac.age_days, R_MIN, R_MAX)
        x, y = polar(CX, CY, radius, bearing)

        # Le blip s'illumine au passage du faisceau (même période que la rotation)
        t0 = bearing / 360.0 * T
        begin = f"{t0 - T:.3f}s"

        heading = _hash(ac.repo, "heading") % 360
        hx, hy = polar(x, y, 11, heading)
        p.append("<g>")
        p.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="none" stroke="{ac.color}" '
                 f'stroke-width="1.2" opacity="0">'
                 f'<animate attributeName="r" values="4;18;18" keyTimes="0;0.2;1" dur="{dur}" '
                 f'begin="{begin}" repeatCount="indefinite"/>'
                 f'<animate attributeName="opacity" values="0.9;0;0" keyTimes="0;0.2;1" dur="{dur}" '
                 f'begin="{begin}" repeatCount="indefinite"/></circle>')
        # Vecteur de cap et historique de trace
        p.append(f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{hx:.2f}" y2="{hy:.2f}" '
                 f'stroke="{ac.color}" stroke-width="1" opacity="0.6"/>')
        for dist, rad, op in ((7, 1.7, 0.5), (13, 1.4, 0.35), (19, 1.1, 0.2)):
            dx, dy = polar(x, y, dist, (heading + 180) % 360)
            p.append(f'<circle cx="{dx:.2f}" cy="{dy:.2f}" r="{rad}" fill="{ac.color}" opacity="{op}"/>')
        p.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.6" fill="{ac.color}" opacity="0.4" '
                 f'filter="url(#glow)">'
                 f'<animate attributeName="opacity" values="1;0.4;0.4" keyTimes="0;0.6;1" dur="{dur}" '
                 f'begin="{begin}" repeatCount="indefinite"/></circle>')
        # Étiquette
        right = x >= CX
        lx = x + 9 if right else x - 9
        anchor = "start" if right else "end"
        p.append(f'<text x="{lx:.1f}" y="{y - 1:.1f}" font-size="10" font-weight="bold" '
                 f'text-anchor="{anchor}" class="txt">{escape(ac.callsign)}</text>')
        p.append(f'<text x="{lx:.1f}" y="{y + 10:.1f}" font-size="8" '
                 f'text-anchor="{anchor}" class="dim">{fmt_age(ac.age_days)}</text>')
        p.append("</g>")

    if not aircraft:
        p.append(f'<text x="{CX}" y="{CY + 70}" font-size="12" text-anchor="middle" '
                 f'class="dim">NO TRAFFIC IN SECTOR</text>')

    # Panneau de droite : strips de vol
    PX, PW = 520, 336
    p.append(f'<text x="{PX}" y="76" font-size="11" class="dim">'
             f'TRAFFIC // {len(aircraft)} AIRCRAFT</text>')
    y0, row_h, gap = 90, 54, 6
    for i, ac in enumerate(aircraft):
        y = y0 + i * (row_h + gap)
        p.append(f'<g class="strip" style="animation-delay:{i * 0.15:.2f}s">')
        p.append(f'<rect x="{PX}" y="{y}" width="{PW}" height="{row_h}" rx="4" '
                 f'fill="{PANEL_BG}" stroke="{EDGE}"/>')
        p.append(f'<rect x="{PX}" y="{y}" width="4" height="{row_h}" rx="2" fill="{ac.color}"/>')
        p.append(f'<text x="{PX + 16}" y="{y + 21}" font-size="15" font-weight="bold" '
                 f'class="hi">{escape(ac.callsign)}</text>')
        p.append(f'<text x="{PX + PW - 10}" y="{y + 21}" font-size="11" text-anchor="end" '
                 f'class="dim">LAST COMMIT {fmt_age(ac.age_days)} AGO</text>')
        p.append(f'<text x="{PX + 16}" y="{y + 37}" font-size="11" class="txt">'
                 f'{escape(truncate(ac.repo, 34))}</text>')
        p.append(f'<circle cx="{PX + 20}" cy="{y + 46}" r="3" fill="{ac.color}"/>')
        p.append(f'<text x="{PX + 28}" y="{y + 49}" font-size="10" class="dim">'
                 f'{escape(truncate(ac.language, 18))}  ·  ★ {ac.stars}</text>')
        p.append("</g>")
    if not aircraft:
        p.append(f'<text x="{PX}" y="{y0 + 20}" font-size="11" class="dim">'
                 f'No eligible public repository found.</text>')

    # Bloc de statistiques
    sy = 408
    p.append(f'<rect x="{PX}" y="{sy}" width="{PW}" height="56" rx="4" fill="{PANEL_BG}" stroke="{EDGE}"/>')
    stats = (
        ("CONTRIBS/12M", compact(profile.contributions)),
        ("REPOS", compact(profile.public_repos)),
        ("STARS", compact(profile.stars)),
        ("FOLLOWERS", compact(profile.followers)),
    )
    for i, (name, value) in enumerate(stats):
        sx = PX + 16 + i * 84
        p.append(f'<text x="{sx}" y="{sy + 28}" font-size="18" font-weight="bold" '
                 f'class="hi">{escape(value)}</text>')
        p.append(f'<text x="{sx}" y="{sy + 44}" font-size="9" class="dim">{name}</text>')

    p.append(f'<text x="{PX}" y="486" font-size="9" class="dim">'
             f'RANGE = LAST COMMIT AGE (LOG) · COLOUR = LANGUAGE</text>')

    # Lignes de balayage CRT par-dessus l'ensemble
    p.append(f'<rect width="{W}" height="{H}" rx="14" fill="url(#scan)" opacity="0.35" pointer-events="none"/>')
    p.append("</svg>")
    return "\n".join(p) + "\n"


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("Configuration : %s", exc)
        return 2

    try:
        profile, aircraft = fetch_snapshot(cfg)
    except GitHubError as exc:
        log.error("API GitHub : %s", exc)
        if cfg.output.exists():
            log.error("Le fichier existant %s n'a pas été modifié.", cfg.output)
        return 1

    log.info("%d aéronef(s) retenu(s) : %s", len(aircraft), ", ".join(a.callsign for a in aircraft) or "aucun")

    svg = build_svg(profile, aircraft, cfg, datetime.now(timezone.utc))

    cfg.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.output.with_suffix(cfg.output.suffix + ".tmp")
    tmp.write_text(svg, encoding="utf-8")
    tmp.replace(cfg.output)  # écriture atomique
    log.info("SVG écrit : %s (%d octets)", cfg.output, len(svg.encode("utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
