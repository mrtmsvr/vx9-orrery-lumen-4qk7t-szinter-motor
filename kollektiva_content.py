#!/usr/bin/env python3
"""
Kollektíva (kollektiva.hu) – napi tartalomautomatizálás
=======================================================

Két JSON-t állít elő a statikus oldalnak:
  • horoscope.json        – a mai nap horoszkópja mind a 12 jegyre
  • retro_articles.json   – "Ekkor történt" cikkek archívuma (legújabb elöl)

A cikkek a Kollektíva cikk-sémáját követik (v1: id, slug, status, category,
title, lead, content, sources[], authorship, seo, monetization, category_meta…),
így ugyanaz a szerkezet később változtatás nélkül mehet Supabase-be / Vercelre.

Működés:
  - Ha van API-kulcs (Anthropic vagy OpenAI), AI-val ír szöveget.
  - Ha nincs kulcs vagy az API hibázik: logol, és beépített fallback tartalmat
    használ – a futás SOHA nem áll le emiatt.
  - A retro cikkek TÉNYEI a kurált `data/retro_events.json` fájlból jönnek;
    az AI csak megfogalmaz, nem talál ki eseményt. Ha egy napra nincs kurált
    esemény, a cikk kimarad (rossz dátumú/kitalált történelem rosszabb, mint semmi).

Futtatás:
  python kollektiva_content.py                 # mai nap (Europe/Budapest)
  python kollektiva_content.py --date 2026-09-26
  python kollektiva_content.py --provider mock # AI nélkül
  python kollektiva_content.py --dry-run       # csak kiírja, nem ment

Csak a Python standard könyvtárát használja (Python 3.9+), nincs pip install.
"""

from __future__ import annotations

import argparse
import html
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Ha szkriptként fut (python kollektiva_content.py), a többi modul (telegram_review, polls, quiz…) „import
# kollektiva_content” hívása ugyanezt a példányt kapja – különben két külön AIError osztály lenne, és egy
# AI-hiba elkerülné a hibakezelést (így omlott össze a robot a grafika gombnál).
if __name__ == "__main__":
    sys.modules.setdefault("kollektiva_content", sys.modules[__name__])

BASE_DIR = Path(__file__).resolve().parent
# A kanonikus webcím (kollektíva.hu punycode alakja). Ékezet nélküli kollektiva.hu MÁS domain!
SITE_URL = os.getenv("SITE_URL", "https://xn--kollektva-m5a.hu").rstrip("/")
SITE_NAME = "Kollektíva"
log = logging.getLogger("kollektiva")


# ---------------------------------------------------------------------------
# Konfiguráció
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Egyszerű .env betöltő (KULCS=érték). A már beállított környezeti
    változókat nem írja felül – így CI-ban a secretek elsőbbséget élveznek."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    provider: str            # auto | anthropic | openai | mock
    anthropic_key: str
    anthropic_model: str
    openai_key: str
    openai_model: str
    gemini_key: str
    gemini_model: str
    groq_key: str
    groq_model: str
    output_dir: Path
    events_file: Path
    timezone: str
    retro_archive_limit: int
    http_timeout: int
    http_retries: int

    @staticmethod
    def from_env() -> "Config":
        return Config(
            provider=os.getenv("AI_PROVIDER", "auto").lower(),
            anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            openai_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            gemini_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest,gemini-flash-lite-latest,gemini-3.1-flash-lite"),
            groq_key=os.getenv("GROQ_API_KEY", ""),
            groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            output_dir=(BASE_DIR / os.getenv("OUTPUT_DIR", "public/data")).resolve(),
            events_file=(BASE_DIR / os.getenv("RETRO_EVENTS_FILE", "data/retro_events.json")).resolve(),
            timezone=os.getenv("SITE_TIMEZONE", "Europe/Budapest"),
            retro_archive_limit=int(os.getenv("RETRO_ARCHIVE_LIMIT", "60")),
            http_timeout=int(os.getenv("HTTP_TIMEOUT", "90")),
            http_retries=int(os.getenv("HTTP_RETRIES", "3")),
        )


# ---------------------------------------------------------------------------
# Csillagjegyek
# ---------------------------------------------------------------------------

SIGNS = [
    {"id": "kos",      "name": "Kos",      "symbol": "♈", "element": "tűz",  "dates": "03.21–04.19"},
    {"id": "bika",     "name": "Bika",     "symbol": "♉", "element": "föld", "dates": "04.20–05.20"},
    {"id": "ikrek",    "name": "Ikrek",    "symbol": "♊", "element": "levegő", "dates": "05.21–06.20"},
    {"id": "rak",      "name": "Rák",      "symbol": "♋", "element": "víz",  "dates": "06.21–07.22"},
    {"id": "oroszlan", "name": "Oroszlán", "symbol": "♌", "element": "tűz",  "dates": "07.23–08.22"},
    {"id": "szuz",     "name": "Szűz",     "symbol": "♍", "element": "föld", "dates": "08.23–09.22"},
    {"id": "merleg",   "name": "Mérleg",   "symbol": "♎", "element": "levegő", "dates": "09.23–10.22"},
    {"id": "skorpio",  "name": "Skorpió",  "symbol": "♏", "element": "víz",  "dates": "10.23–11.21"},
    {"id": "nyilas",   "name": "Nyilas",   "symbol": "♐", "element": "tűz",  "dates": "11.22–12.21"},
    {"id": "bak",      "name": "Bak",      "symbol": "♑", "element": "föld", "dates": "12.22–01.19"},
    {"id": "vizonto",  "name": "Vízöntő",  "symbol": "♒", "element": "levegő", "dates": "01.20–02.18"},
    {"id": "halak",    "name": "Halak",    "symbol": "♓", "element": "víz",  "dates": "02.19–03.20"},
]
SIGN_IDS = [s["id"] for s in SIGNS]
HU_MONTHS = ["január", "február", "március", "április", "május", "június", "július",
             "augusztus", "szeptember", "október", "november", "december"]
HU_WEEKDAYS = ["hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap"]


def hu_date(d: date) -> str:
    return f"{d.year}. {HU_MONTHS[d.month - 1]} {d.day}., {HU_WEEKDAYS[d.weekday()]}"


# ---------------------------------------------------------------------------
# HTTP + AI kliens
# ---------------------------------------------------------------------------

class AIError(Exception):
    """Bármilyen AI-hívási hiba – a hívó oldalon fallbackre váltunk."""


_GEMINI_OUT: dict = {}  # modell → időpont, ameddig nem próbáljuk (elfogyott napi keret)


def ai_quota_out() -> bool:
    """Igaz, ha ebben a futásban minden beállított Gemini-modell napi ingyenes kerete elfogyott."""
    models = [m.strip() for m in Config.from_env().gemini_model.split(",") if m.strip()]
    return bool(models) and all(_GEMINI_OUT.get(m, 0) > time.time() for m in models)


def model_health() -> list:
    """Napi ellenőrzés: a beállított AI-modellek léteznek-e még (a szolgáltatók időnként megszüntetik / átnevezik őket),
    és tegnap elég volt-e az ingyenes keret. Visszaad: a gondok listája (üres = minden rendben)."""
    cfg = Config.from_env()
    probs = []

    def _get(url: str, headers: dict) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "KollektivaBot/1.0", **headers})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    if cfg.gemini_key:
        try:
            names, tok = set(), ""
            for _ in range(5):
                d = _get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000" + (f"&pageToken={tok}" if tok else ""),
                         {"x-goog-api-key": cfg.gemini_key})
                names |= {m.get("name", "").split("/")[-1] for m in d.get("models", [])}
                tok = d.get("nextPageToken") or ""
                if not tok:
                    break
            for m in [x.strip() for x in cfg.gemini_model.split(",") if x.strip()]:
                if m in names:
                    continue
                try:  # a lista nem mindig tartalmazza az álneveket (…-latest) – egy apró próbahívás dönt
                    post_json("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                              {"Authorization": f"Bearer {cfg.gemini_key}"},
                              {"model": m, "max_tokens": 5, "messages": [{"role": "user", "content": "ok"}]}, 20, 1)
                except AIError as e:
                    if "HTTP 404" in str(e) or "HTTP 400" in str(e):
                        alt = sorted(n for n in names if "flash" in n and not any(x in n for x in ("image", "tts", "audio", "live")))[-4:]
                        probs.append(f"Gemini: a „{m}” modell megszűnt / nem elérhető. Elérhető pl.: {', '.join(alt)}")
        except Exception as e:  # noqa: BLE001
            probs.append(f"Gemini: a modellista nem kérhető le ({str(e)[:120]})")
    if cfg.groq_key:
        try:
            ids = {m["id"] for m in _get("https://api.groq.com/openai/v1/models",
                                          {"Authorization": f"Bearer {cfg.groq_key}"}).get("data", []) if m.get("active", True)}
            if cfg.groq_model not in ids:
                probs.append(f"Groq: a „{cfg.groq_model}” modell megszűnt – a robot a legjobb elérhetőre vált, de érdemes átírni.")
        except Exception as e:  # noqa: BLE001
            probs.append(f"Groq: a modellista nem kérhető le ({str(e)[:120]})")
    y = (datetime.now(ZoneInfo("Europe/Budapest")).date() - timedelta(days=1)).isoformat()
    u = read_json(_usage_file(), {}).get(y, {})
    ok, bad = u.get("gemini", 0), u.get("gemini_hiba", 0)
    if bad > max(20, ok // 2):
        probs.append(f"Tegnap {bad} Gemini-hívás bukott el (elfogyott az ingyenes napi keret) – ilyenkor a gyengébb tartalék "
                     "ír, vagy a cikk kimarad.")
    return probs


def post_json(url: str, headers: dict, payload: dict, timeout: int, retries: int) -> dict:
    """POST JSON, exponenciális visszalépéssel. 429/5xx és hálózati hiba esetén újrapróbál."""
    body = json.dumps(payload).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "KollektivaBot/1.0 (+https://xn--kollektva-m5a.hu)", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if "<html" in detail.lower():  # HTML hibaoldal (pl. Cloudflare-botvédelem) – ne a nyers HTML-t adjuk tovább
                detail = "a szolgáltató HTML hibaoldalt adott (botvédelem vagy hibás végpont/jogosultság)"
            last_err = AIError(f"HTTP {e.code}: {detail}")
            if e.code not in (408, 429, 500, 502, 503, 504, 529) or (e.code == 429 and "quota" in detail.lower()):
                break  # kliens hiba (pl. rossz kulcs) vagy elfogyott napi keret – nincs értelme újrapróbálni
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = AIError(f"Hálózati/válasz hiba: {e}")
        if attempt < retries:
            wait = 2 ** attempt
            log.warning("AI hívás sikertelen (%s), újra %ss múlva…", last_err, wait)
            time.sleep(wait)
    raise last_err or AIError("Ismeretlen hiba")


# ---------------------------------------------------------------------------
# Ingyenes keretek figyelése: minden AI-hívást és képgenerálást napi bontásban számolunk
# (data/usage_<workflow>.json – workflow-nként külön fájl, hogy a párhuzamos robotok ne ütközzenek a gitben).
# A napi keretek a szolgáltatók ingyenes szintjéhez igazíthatók (env), a Telegram 95%-nál szól, /keret: állapot.
# ---------------------------------------------------------------------------
USAGE_LIMITS = {"gemini": ("Gemini (cikkírás)", "GEMINI_DAILY_LIMIT", 250), "groq": ("Groq (segédfeladatok, tartalék)", "GROQ_DAILY_LIMIT", 1000),
                "cf_image": ("Cloudflare képgenerálás", "CF_IMAGE_DAILY_LIMIT", 80)}


def _usage_file() -> Path:
    wf = re.sub(r"[^a-z0-9]+", "-", os.getenv("GITHUB_WORKFLOW", "local").lower()).strip("-") or "local"
    return BASE_DIR / "data" / f"usage_{wf}.json"


def note_usage(kind: str, ok: bool = True) -> None:
    try:
        day = datetime.now(ZoneInfo("Europe/Budapest")).date().isoformat()
        path = _usage_file()
        u = read_json(path, {})
        k = kind if ok else kind + "_hiba"
        u.setdefault(day, {})[k] = u.get(day, {}).get(k, 0) + 1
        for old in sorted(u)[:-14]:
            u.pop(old, None)
        write_json_atomic(path, u)
    except Exception:  # noqa: BLE001 – a számlálás soha ne akassza meg a robotot
        pass


def usage_today() -> dict:
    day = datetime.now(ZoneInfo("Europe/Budapest")).date().isoformat()
    tot: dict = {}
    for f in (BASE_DIR / "data").glob("usage_*.json"):
        for k, v in read_json(f, {}).get(day, {}).items():
            tot[k] = tot.get(k, 0) + v
    return tot


def usage_report() -> str:
    t = usage_today()
    lines = ["📊 Mai használat az ingyenes kerethez képest:"]
    for k, (name, env, default) in USAGE_LIMITS.items():
        lim = int(os.getenv(env, str(default)))
        lines.append(f"• {name}: {t.get(k, 0)} / {lim}" + (f" (hibás: {t[k + '_hiba']})" if t.get(k + "_hiba") else ""))
    lines.append("A Cloudflare-buildeket (havi 500) a robot magától osztja be; a GitHub Actions nyilvános repónál ingyenes.")
    return "\n".join(lines)


def usage_alert() -> Optional[str]:
    t = usage_today()
    for k, (name, env, default) in USAGE_LIMITS.items():
        lim = int(os.getenv(env, str(default)))
        if t.get(k, 0) >= 0.95 * lim or t.get(k + "_hiba", 0) >= 10:
            return f"⚠️ Figyelem: a(z) {name} napi kerete fogyóban vagy hibázik (/keret). Ma kevesebb cikk készülhet."
    return None


class AIClient:
    """Egységes felület Anthropic és OpenAI felé. `provider` = 'mock' esetén nincs hívás."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        p = cfg.provider
        if p == "auto":
            p = ("anthropic" if cfg.anthropic_key else "gemini" if cfg.gemini_key
                 else "openai" if cfg.openai_key else "groq" if cfg.groq_key else "mock")
        if p == "gemini" and not cfg.gemini_key:
            log.warning("AI_PROVIDER=gemini, de nincs GEMINI_API_KEY – mock mód.")
            p = "mock"
        if p == "anthropic" and not cfg.anthropic_key:
            log.warning("AI_PROVIDER=anthropic, de nincs ANTHROPIC_API_KEY – mock mód.")
            p = "mock"
        if p == "openai" and not cfg.openai_key:
            log.warning("AI_PROVIDER=openai, de nincs OPENAI_API_KEY – mock mód.")
            p = "mock"
        self.provider = p
        log.info("Tartalomforrás: %s", self.label)

    @property
    def enabled(self) -> bool:
        return self.provider != "mock"

    @property
    def label(self) -> str:
        if self.provider == "anthropic":
            return f"ai:anthropic:{self.cfg.anthropic_model}"
        if self.provider == "openai":
            return f"ai:openai:{self.cfg.openai_model}"
        if self.provider == "gemini":
            return f"ai:gemini:{self.cfg.gemini_model}"
        if self.provider == "groq":
            return f"ai:groq:{self.cfg.groq_model}"
        return "fallback"

    def _groq_model(self) -> str:
        """A beállított Groq-modell, vagy ha azt a Groq megszüntette, a legjobb elérhető (a lista futásonként egyszer)."""
        if getattr(self, "_gm", None):
            return self._gm
        want = self.cfg.groq_model
        try:
            req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                         headers={"Authorization": f"Bearer {self.cfg.groq_key}", "User-Agent": "KollektivaBot/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ids = [m["id"] for m in json.loads(r.read()).get("data", []) if m.get("active", True)]
        except Exception:  # noqa: BLE001
            ids = []
        if ids and want not in ids:
            skip = ("guard", "whisper", "tts", "compound", "playai", "distil")
            pref = ("llama-4-maverick", "gpt-oss-120b", "llama-4-scout", "kimi-k2", "qwen3-32b", "llama-3.3-70b",
                    "gpt-oss-20b", "llama-3.1-8b")
            cand = [i for p in pref for i in ids if p in i and not any(x in i for x in skip)]
            if cand:
                log.info("Groq: a %s modell nem elérhető, helyette: %s", want, cand[0])
                want = cand[0]
        self._gm = want
        return want

    def _groq(self, system: str, prompt: str, max_tokens: int) -> str:
        """Groq (ingyenes, gyors, OpenAI-kompatibilis): tartalék, ha a fő modell keretet/hibát ad, és a kis
        feladatok (duplikáció, képkulcsszó, szavazás) gyors végrehajtója, hogy a fő keret a cikkírásra maradjon."""
        c = self.cfg
        data = post_json("https://api.groq.com/openai/v1/chat/completions", {"Authorization": f"Bearer {c.groq_key}"},
                         {"model": self._groq_model(), "max_tokens": min(max_tokens, 8000),
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                         c.http_timeout, 2)
        note_usage("groq")
        return data["choices"][0]["message"]["content"]

    def complete(self, system: str, prompt: str, max_tokens: int = 4000, light: bool = False) -> str:
        """light=True: rövid segédfeladat – ha van Groq-kulcs, azzal megy (a fő modell kerete megmarad).
        Ha a fő modell hibát ad (pl. elfogyott a napi keret), Groq-kal próbálja újra."""
        groq = bool(self.cfg.groq_key) and self.provider != "groq"
        if light and groq:
            try:
                return self._groq(system, prompt, max_tokens)
            except Exception as e:  # noqa: BLE001
                log.warning("Groq (segédfeladat) sikertelen, fő modell jön: %s", str(e)[:200])
        try:
            return self._complete_main(system, prompt, max_tokens)
        except Exception as e:  # noqa: BLE001
            if not groq:
                raise
            log.warning("Fő AI sikertelen (%s) – Groq tartalékkal próbálom.", str(e)[:200])
            return self._groq(system, prompt, max_tokens)

    def _complete_main(self, system: str, prompt: str, max_tokens: int) -> str:
        c = self.cfg
        if self.provider == "groq":
            return self._groq(system, prompt, max_tokens)
        if self.provider == "anthropic":
            data = post_json(
                "https://api.anthropic.com/v1/messages",
                {"x-api-key": c.anthropic_key, "anthropic-version": "2023-06-01"},
                {"model": c.anthropic_model, "max_tokens": max_tokens, "system": system,
                 "messages": [{"role": "user", "content": prompt}]},
                c.http_timeout, c.http_retries)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if self.provider == "openai":
            data = post_json(
                "https://api.openai.com/v1/chat/completions",
                {"Authorization": f"Bearer {c.openai_key}"},
                {"model": c.openai_model, "max_tokens": max_tokens,
                 "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": prompt}]},
                c.http_timeout, c.http_retries)
            return data["choices"][0]["message"]["content"]
        if self.provider == "gemini":
            # A Gemini API OpenAI-kompatibilis végpontja (ingyenes szint: aistudio.google.com).
            # GEMINI_MODEL vesszővel elválasztott lista is lehet: túlterhelés (503) esetén
            # a következő modellel próbálkozik.
            models = [m.strip() for m in c.gemini_model.split(",") if m.strip()]
            last: Optional[Exception] = None
            for model in models:
                if _GEMINI_OUT.get(model, 0) > time.time():
                    continue  # ennek a modellnek elfogyott a kerete – egy óráig nem próbáljuk (gyorsabb, kevesebb hiba)
                try:
                    data = post_json(
                        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                        {"Authorization": f"Bearer {c.gemini_key}"},
                        {"model": model, "max_tokens": max_tokens,
                         "response_format": {"type": "json_object"},
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": prompt}]},
                        c.http_timeout, c.http_retries)
                    note_usage("gemini")
                    return data["choices"][0]["message"]["content"]
                except Exception as e:  # noqa: BLE001 – következő modell
                    note_usage("gemini", ok=False)
                    if "429" in str(e) and "quota" in str(e).lower():
                        _GEMINI_OUT[model] = time.time() + 3600
                    log.warning("Gemini modell sikertelen (%s): %s", model, str(e)[:200])
                    last = e
            raise last or AIError("Minden Gemini-modell napi kerete elfogyott (quota)")
        raise AIError("Mock módban nincs AI hívás")

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4000, light: bool = False) -> dict:
        text = self.complete(system, prompt, max_tokens, light)
        return extract_json(text)


def extract_json(text: str) -> dict:
    """JSON kinyerése a modell válaszából (kódkerítés és kísérőszöveg eltávolítása)."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise AIError("A válasz nem tartalmaz JSON objektumot")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"Érvénytelen JSON a válaszban: {e}") from e


# ---------------------------------------------------------------------------
# Fájlkezelés
# ---------------------------------------------------------------------------

def write_json_atomic(path: Path, data: Any) -> None:
    """Ideiglenes fájlba ír, majd átnevez – félkész JSON sosem kerül ki az oldalra."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        log.error("Nem olvasható: %s (%s) – alapértelmezett érték", path, e)
        return default


def seeded_rng(*parts: Any) -> random.Random:
    """Determinisztikus véletlen: ugyanarra a napra és jegyre mindig ugyanazt adja."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def reading_time(text: str, wpm: int = 200) -> int:
    """Olvasási idő percben (magyar szövegre ~200 szó/perc)."""
    return max(1, math.ceil(len(re.findall(r"\w+", text)) / wpm))


# ---------------------------------------------------------------------------
# 1) Napi horoszkóp
# ---------------------------------------------------------------------------

HOROSCOPE_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) asztrológiai rovatának szerkesztője vagy. "
    "Szórakoztató rovatot írsz, amelynek célja, hogy az olvasó úgy érezze: pontosan róla szól. "
    "Természetes, igényes, közérthető magyar nyelven írsz (tegezve); kerülöd a közhelyeket, a "
    "rémisztgetést, az egészségügyi, jogi vagy pénzügyi konkrét tanácsot. Csak érvényes JSON-t adsz vissza."
)


def horoscope_prompt(d: date) -> str:
    signs = ", ".join(f'{s["id"]} ({s["name"]}, {s["element"]})' for s in SIGNS)
    return f"""Írd meg a {hu_date(d)} napi horoszkópot mind a 12 csillagjegyre.

Csillagjegyek (id, név, elem): {signs}

Minden jegyhez:
- "headline": 3–7 szavas, egyedi főcím
- "text": 25–40 szavas (2–3 rövid mondat), személyesnek ható napi szöveg, tegező formában

Írástechnika (Barnum/Forer-hatás – ettől érzi az olvasó személyre szabottnak):
- Olyan állításokat írj, amelyek szinte bárkire igazak, de konkrétnak hatnak
  (pl. „Az utóbbi napokban többször visszatért hozzád egy félbehagyott gondolat.”).
- Használj kétoldalú jellemzést (pl. „kifelé magabiztosnak tűnsz, belül mégis mérlegelsz”).
- Utalj rejtett erősségre vagy ki nem használt lehetőségre, hízelgően, de nem túlzóan.
- Adj egy hétköznapi, felismerhető helyzetet (üzenet, beszélgetés, halogatott teendő, döntés),
  és egy apró, könnyen megtehető javaslatot.
- Időbeli támpont segít (délelőtt, a nap második fele, este).
- Az elem hangulata (tűz, föld, levegő, víz) finoman érződjön, de ne ismételd a jegy nevét.
- Minden jegynél más szerkezettel és más képekkel kezdj; ne ismételj mondatot vagy fordulatot.
- Soha ne jósolj konkrét eseményt, betegséget, pénzösszeget vagy veszteséget.
- "love", "work", "energy": egész szám 1–5
- "focus": egyetlen rövid mondat, a nap kulcsgondolata

Kizárólag ezt a JSON szerkezetet add vissza, más szöveget ne:
{{"signs": {{"kos": {{"headline": "...", "text": "...", "love": 3, "work": 4, "energy": 2, "focus": "..."}}, ... mind a 12 id ...}}}}"""


def validate_horoscope(raw: dict) -> dict:
    """Ellenőrzi és normalizálja az AI kimenetét. Hibás/hiányzó jegynél AIError."""
    signs = raw.get("signs")
    if not isinstance(signs, dict):
        raise AIError("Hiányzik a 'signs' objektum")
    out = {}
    for sid in SIGN_IDS:
        item = signs.get(sid)
        if not isinstance(item, dict) or len(str(item.get("text", ""))) < 150:
            raise AIError(f"Hiányos vagy túl rövid bejegyzés: {sid}")
        out[sid] = {
            "headline": str(item.get("headline", "")).strip(),
            "text": str(item["text"]).strip(),
            "love": min(5, max(1, int(item.get("love", 3)))),
            "work": min(5, max(1, int(item.get("work", 3)))),
            "energy": min(5, max(1, int(item.get("energy", 3)))),
            "focus": str(item.get("focus", "")).strip(),
            "lucky_color": str(item.get("lucky_color", "")).strip(),
        }
    return out


# Fallback: igényes, elemekhez igazított mondatbank, naponta determinisztikusan keverve.
FB_OPENERS = {
    "tűz": ["Ma a lendületed előbb ér célba, mint a kételyeid.",
            "Belső tüzed ma nem kapkodást, hanem irányt kér.",
            "A nap kihívást tartogat, és te ezt nem teherként, hanem meghívásként éled meg."],
    "föld": ["Ma a lassú, biztos lépések hozzák a legtöbbet.",
             "A figyelmed a kézzelfogható dolgok felé fordul, és ez jó iránytű.",
             "Egy régóta halogatott, gyakorlati ügy ma meglepően könnyen a helyére kerül."],
    "levegő": ["Gondolataid ma szokatlanul tiszták, és ezt mások is észreveszik.",
               "Egy beszélgetés ma többet mozdít, mint egy hét tervezgetés.",
               "A kíváncsiságod ma kaput nyit egy új nézőpont felé."],
    "víz": ["Ma az érzéseid pontosabban látnak, mint a logika.",
            "A csend ma nem üresség, hanem válasz.",
            "Egy finom megérzés ma jó irányba terel, ha hagyod."],
}
FB_MIDDLES = [
    "Érdemes most különválasztanod, mi az, amit valóban akarsz, és mi az, amit csak elvárnak tőled.",
    "Egy apró gesztus – egy üzenet, egy visszahívás – most aránytalanul sokat számít.",
    "Ne siesd el a döntést: a délután olyan információt hozhat, ami átrendezi a képet.",
    "A kapcsolataidban most az őszinteség többet ér a diplomáciánál, ha tapintattal teszed.",
    "Munkában a kevesebb most több: egyetlen jól elvégzett feladat felér három félbehagyottal.",
    "Figyelj a testedre is: a pihenés ma nem luxus, hanem befektetés.",
    "Valaki a környezetedben a támogatásodra vár, még ha nem is mondja ki.",
    "A múlt egy darabja ma más megvilágításba kerül, és ez felszabadító lehet.",
]
FB_CLOSERS = [
    "Este adj magadnak időt, hogy a nap tapasztalatai leülepedjenek.",
    "A nap végére kiderül: jó irányba indultál.",
    "Amit ma elengedsz, annak a helyén holnap tér nyílik valami újnak.",
    "Bízz a saját ritmusodban – ma ez a legjobb stratégia.",
]
FB_FOCUS = ["Kevesebb zaj, több figyelem.", "Az egyszerű út most a bölcs út.",
            "Kérdezz, mielőtt ítélsz.", "A türelem is cselekvés.", "Mondd ki, amit gondolsz."]
FB_COLORS = ["mélykék", "arany", "smaragdzöld", "bordó", "gyöngyházfehér", "levendula", "rozsdabarna"]
FB_HEADLINES = ["Csendes erő", "Új irány a láthatáron", "Tisztuló kép", "A bátorság napja",
                "Belső egyensúly", "Váratlan kapuk", "Lassú, biztos lépések", "A szavak súlya"]


def fallback_horoscope(d: date) -> dict:
    out = {}
    for s in SIGNS:
        r = seeded_rng("horoscope", d.isoformat(), s["id"])
        text = " ".join([r.choice(FB_OPENERS[s["element"]]), *r.sample(FB_MIDDLES, 2), r.choice(FB_CLOSERS)])
        out[s["id"]] = {
            "headline": r.choice(FB_HEADLINES), "text": text,
            "love": r.randint(2, 5), "work": r.randint(2, 5), "energy": r.randint(2, 5),
            "focus": r.choice(FB_FOCUS), "lucky_color": r.choice(FB_COLORS),
        }
    return out


def build_horoscope(ai: AIClient, d: date, tz: ZoneInfo) -> dict:
    source = "fallback"
    entries = None
    if ai.enabled:
        try:
            entries = validate_horoscope(ai.complete_json(HOROSCOPE_SYSTEM, horoscope_prompt(d), 6000))
            source = ai.label
        except (AIError, ValueError, TypeError, KeyError) as e:
            log.error("Horoszkóp AI generálás sikertelen: %s – fallback tartalom", e)
    if entries is None:
        entries = fallback_horoscope(d)

    now = datetime.now(tz)
    expires = datetime.combine(d + timedelta(days=1), datetime.min.time(), tz)
    return {
        "date": d.isoformat(),
        "date_label": hu_date(d),
        "generated_at": now.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "period": "daily",
        "source": source,
        "signs": [{**meta, "sign": meta["id"], **entries[meta["id"]]} for meta in SIGNS],
    }


# ---------------------------------------------------------------------------
# 2) "Ekkor történt" – retro cikk
# ---------------------------------------------------------------------------

RETRO_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) retro rovatának írója vagy. Olvasmányos, "
    "atmoszférikus, de pontos magazincikkeket írsz. SZIGORÚ SZABÁLY: kizárólag a megadott "
    "tényekre és általánosan közismert, vitathatatlan háttérinformációra támaszkodhatsz. "
    "Nem találsz ki idézetet, számot, nevet vagy dátumot. Ha valamiben bizonytalan vagy, "
    "hagyd ki. Csak érvényes JSON-t adsz vissza."
)


def retro_prompt(event: dict, d: date) -> str:
    facts = "\n".join(f"- {f}" for f in event.get("facts", []))
    return f"""Írj egy "Ekkor történt" magazincikket erről az eseményről ({HU_MONTHS[d.month - 1]} {d.day}.):

Esemény: {event["title"]} ({event["year"]})
Ellenőrzött tények (lehetnek angolul – a cikket magyarul írd, a neveket a magyar
szakirodalomban szokásos alakjukban):
{facts}

Elvárások:
- "title": figyelemfelkeltő, de nem bulvár cím (max. 12 szó), NE kezdődjön az „Ekkor történt” szavakkal
- "lead": 2–3 mondatos bevezető
- "body": 5–7 bekezdés (tömb), összesen kb. 600–900 szó, magyarul, magazinstílusban
- "pull_quote": egy saját megfogalmazású kiemelés a cikkből (NEM valós személy idézete)
- "tags": 3–5 rövid címke

Kizárólag ezt a JSON-t add vissza:
{{"title": "...", "lead": "...", "body": ["...", "..."], "pull_quote": "...", "tags": ["..."]}}"""


def load_events(path: Path) -> dict:
    events = read_json(path, {})
    if not events:
        log.warning("Nincs kurált eseményfájl vagy üres: %s", path)
    return events


WIKI_EXCLUDE = re.compile(
    r"\b(kill|killed|killing|massacre|bomb|bombing|attack|shoot|shooting|terror|murder|genocide|"
    r"execut|stampede|hostage|suicide|rape|assassinat|explosion|crash|died|dies|death)\w*", re.I)


WIKI_UA = {"User-Agent": "KollektivaBot/1.0 (https://xn--kollektva-m5a.hu; szerkesztoseg@xn--kollektva-m5a.hu)", "Accept": "application/json"}

# Megbízható külső források (a Wikipédia-cikkek hivatkozásaiból válogatva)
TRUSTED_SOURCES = {
    "britannica.com": "Encyclopaedia Britannica", "bbc.co.uk": "BBC", "bbc.com": "BBC",
    "nytimes.com": "The New York Times", "theguardian.com": "The Guardian", "history.com": "History",
    "smithsonianmag.com": "Smithsonian Magazine", "nationalgeographic.com": "National Geographic",
    "loc.gov": "Library of Congress", "archives.gov": "National Archives", "nasa.gov": "NASA",
    "reuters.com": "Reuters", "apnews.com": "Associated Press", "time.com": "TIME",
    "washingtonpost.com": "The Washington Post", "latimes.com": "Los Angeles Times",
    "variety.com": "Variety", "hollywoodreporter.com": "The Hollywood Reporter",
    "rollingstone.com": "Rolling Stone", "nobelprize.org": "Nobel Prize", "un.org": "United Nations",
    "europa.eu": "Európai Unió", "cam.ac.uk": "University of Cambridge", "ox.ac.uk": "University of Oxford",
    "nature.com": "Nature", "science.org": "Science", "esa.int": "ESA", "unesco.org": "UNESCO",
    "rubicon.hu": "Rubicon", "arcanum.com": "Arcanum", "mek.oszk.hu": "Magyar Elektronikus Könyvtár",
    "nemzetiarchivum.hu": "Nemzeti Archívum", "npr.org": "NPR", "theatlantic.com": "The Atlantic",
    "independent.co.uk": "The Independent", "telegraph.co.uk": "The Telegraph", "wsj.com": "The Wall Street Journal",
    "pbs.org": "PBS", "newyorker.com": "The New Yorker", "economist.com": "The Economist", "ft.com": "Financial Times",
    "cbsnews.com": "CBS News", "nbcnews.com": "NBC News", "abcnews.go.com": "ABC News", "olympics.com": "Olympics",
    "fifa.com": "FIFA", "espn.com": "ESPN", "ew.com": "Entertainment Weekly", "people.com": "People",
}
FREE_LICENSE = re.compile(r"^(public domain|pd|cc0|cc[ -]by(-sa)?( \d\.\d)?|cc by(-sa)? \d\.\d.*)", re.I)


def http_get_json(url: str, timeout: int) -> Optional[dict]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=WIKI_UA), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.debug("GET sikertelen: %s (%s)", url, e)
        return None


def _q(title: str) -> str:
    return urllib.parse.quote(title.replace(" ", "_"), safe="")


def wiki_hu_summary(en_title: str, timeout: int) -> Optional[dict]:
    """A magyar Wikipédia megfelelő cikkének kivonata (ha létezik)."""
    data = http_get_json("https://en.wikipedia.org/w/api.php?action=query&format=json&prop=langlinks"
                         f"&lllang=hu&titles={_q(en_title)}", timeout)
    try:
        page = next(iter(data["query"]["pages"].values()))
        hu_title = page["langlinks"][0]["*"]
    except (TypeError, KeyError, IndexError, StopIteration):
        return None
    summ = http_get_json(f"https://hu.wikipedia.org/api/rest_v1/page/summary/{_q(hu_title)}", timeout)
    if not summ or not summ.get("extract"):
        return None
    return {"title": hu_title, "extract": summ["extract"],
            "url": summ.get("content_urls", {}).get("desktop", {}).get("page",
                                                                        f"https://hu.wikipedia.org/wiki/{_q(hu_title)}")}


def wiki_external_sources(en_title: str, timeout: int, limit: int = 2) -> list:
    """A Wikipédia-cikk hivatkozásai közül a megbízható, nem archív külső források."""
    data = http_get_json("https://en.wikipedia.org/w/api.php?action=query&format=json&prop=extlinks"
                         f"&ellimit=500&titles={_q(en_title)}", timeout)
    try:
        links = [l["*"] for l in next(iter(data["query"]["pages"].values())).get("extlinks", [])]
    except (TypeError, KeyError, StopIteration):
        return []
    out, seen = [], set()
    for url in links:
        if not url.startswith("https://") or "web.archive.org" in url or "archive.today" in url:
            continue
        host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc.lower())
        dom = next((d for d in TRUSTED_SOURCES if host == d or host.endswith("." + d)), None)
        if not dom or dom in seen:
            continue
        seen.add(dom)
        out.append({"url": url, "title": TRUSTED_SOURCES[dom], "publisher": TRUSTED_SOURCES[dom]})
        if len(out) >= limit:
            break
    return out


IMAGE_OPTIONS = int(os.getenv("IMAGE_OPTIONS", "8"))  # képjelöltek száma a Telegramos választáshoz (max. 10)
GRAPHIC_HINT = re.compile(r"logo|wordmark|icon|seal|coat[_ ]of[_ ]arms|flag|emblem|map|diagram|\.svg$", re.I)


def _image_from_info(info: dict, fname: str = "") -> Optional[dict]:
    """Commons imageinfo -> cikk-kép. Csak szabad licenc; logó/grafika 'graphic' típust kap
    (a megjelenítés ilyenkor nem vágja, hanem arányosan, háttérrel illeszti)."""
    meta = info.get("extmetadata", {})
    lic = re.sub(r"<[^>]+>", "", meta.get("LicenseShortName", {}).get("value", "")).strip()
    if not FREE_LICENSE.match(lic):
        return None
    w, h = info.get("width") or 0, info.get("height") or 0
    if w < 300 or h < 150:
        return None  # túl kicsi, pixeles lenne
    fname = fname or info.get("descriptionurl", "").rsplit("/", 1)[-1]
    if re.search(r"\.(pdf|djvu|tiff?|webm|ogv|ogg|mp3|wav|stl)$", fname, re.I):
        return None  # nem fotó (dokumentum, videó, hang)
    ratio = w / h if h else 0
    # álló (portré) fotó: nem grafika, hanem kép, amit felülre igazítva vágunk (az arc ne vesszen el)
    portrait = not GRAPHIC_HINT.search(fname) and w >= 600 and 0.5 <= ratio < 1.2
    kind = "photo" if portrait else ("graphic" if (GRAPHIC_HINT.search(fname) or w < 800 or not 1.2 <= ratio <= 2.2) else "photo")
    artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "ismeretlen szerző"
    return {
        "url": info.get("thumburl") or info["url"],
        "width": info.get("thumbwidth") or w,
        "height": info.get("thumbheight") or h,
        "kind": kind,
        "pos": "top" if portrait else "center",
        "alt": re.sub(r"<[^>]+>", "", meta.get("ImageDescription", {}).get("value", ""))[:200].strip(),
        "credit": f"{artist[:80]} / Wikimedia Commons",
        "license": lic,
        "source_url": info.get("descriptionurl", ""),
    }


def commons_image(page: dict, timeout: int) -> Optional[dict]:
    """A Wikipédia-oldal fő képe, CSAK ha a Wikimedia Commonson van és szabad licencű."""
    src = (page.get("originalimage") or page.get("thumbnail") or {}).get("source", "")
    if "/commons/" not in src:
        return None  # helyi (pl. fair use) kép – nem használjuk
    fname = urllib.parse.unquote(src.split("/")[-1] if "/thumb/" not in src else src.split("/thumb/")[1].split("/")[2])
    data = http_get_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo"
                         f"&iiprop=url|extmetadata|size&iiurlwidth=1200&titles=File:{_q(fname)}", timeout)
    try:
        info = next(iter(data["query"]["pages"].values()))["imageinfo"][0]
    except (TypeError, KeyError, IndexError, StopIteration):
        return None
    return _image_from_info(info, fname)


def commons_search_image(query: str, timeout: int, strict: bool = True, avoid: Optional[set] = None) -> Optional[dict]:
    """Szabad licencű kép keresése a Wikimedia Commonson (fotót részesít előnyben)."""
    found = commons_search_images(query, timeout, strict, avoid, limit=1)
    return found[0] if found else None


def commons_search_images(query: str, timeout: int, strict: bool = True, avoid: Optional[set] = None,
                          limit: int = 4) -> list:
    """Több képjelölt a Commonsról (előbb a fotók, aztán a grafikák).
    strict=True: a keresőszó minden jellegzetes szavának (max. 2) szerepelnie kell a fájlnévben/leírásban."""
    if not query:
        return []
    data = http_get_json("https://commons.wikimedia.org/w/api.php?action=query&format=json&generator=search"
                         f"&gsrnamespace=6&gsrlimit=10&gsrsearch={urllib.parse.quote(query)}"
                         "&prop=imageinfo&iiprop=url|extmetadata|size&iiurlwidth=1200", timeout)
    try:
        pages = sorted(data["query"]["pages"].values(), key=lambda p: p.get("index", 99))
    except (TypeError, KeyError):
        return []
    # Csak olyan képet fogadunk el, amelynek fájlneve/leírása tényleg a keresett dologról szól
    # (különben pl. egy ELTE-s hírhez egy indiai előadás képe jönne be).
    def norm(t: str) -> str:
        return t.lower().translate(str.maketrans("áéíóöőúüű", "aeiooouuu"))
    generic = {"with", "from", "that", "this", "university", "building", "people", "city", "photo", "image",
               "picture", "center", "centre", "house", "street", "group", "meeting", "hungary", "hungarian"}
    tokens = [w for w in re.findall(r"\w{4,}", norm(query)) if w not in generic] or re.findall(r"\w{4,}", norm(query))
    need = 2 if strict else 1
    photos, graphics = [], []
    for pg in pages:
        info = (pg.get("imageinfo") or [None])[0]
        if not info:
            continue
        desc = norm(pg.get("title", "") + " " + re.sub(r"<[^>]+>", " ", info.get("extmetadata", {})
                                                          .get("ImageDescription", {}).get("value", ""))[:300])
        if sum(1 for t in tokens if t in desc) < min(need, len(tokens)):
            continue
        img = _image_from_info(info, pg.get("title", ""))
        if img and avoid and img["url"] in avoid:
            continue  # ezt a képet nemrég már használtuk
        if img:
            (photos if img["kind"] == "photo" else graphics).append(img)
    return (photos + graphics)[:limit]


def openverse_image(query: str, timeout: int, avoid: Optional[set] = None) -> Optional[dict]:
    """Általános témához (pl. „coffee cup”) szabad licencű fotó az Openverse-ből (Flickr CC stb.)."""
    found = openverse_images(query, timeout, avoid, limit=1)
    return found[0] if found else None


def openverse_images(query: str, timeout: int, avoid: Optional[set] = None, limit: int = 4) -> list:
    if not query:
        return []
    out = []
    data = http_get_json("https://api.openverse.org/v1/images/?page_size=12&license_type=commercial"
                         f"&mature=false&q={urllib.parse.quote(query)}", timeout)
    words = [w for w in re.findall(r"\w{3,}", query.lower())]
    for r in (data or {}).get("results", []):
        w, h = r.get("width") or 0, r.get("height") or 0
        title = (r.get("title") or "").lower() + " " + " ".join(t.get("name", "") for t in r.get("tags") or [])
        if (w < 1000 or not h or not 1.25 <= w / h <= 2.0 or (words and words[0] not in title)
                or (avoid and r.get("url") in avoid)):
            continue
        lic = f"{(r.get('license') or '').upper()} {r.get('license_version') or ''}".strip()
        lic = "Public domain" if lic.startswith(("PDM", "CC0")) else ("CC " + lic.replace("BY-SA", "BY-SA"))
        out.append({"url": r["url"], "width": w, "height": h, "kind": "photo", "alt": (r.get("title") or "")[:200],
                    "credit": f"{(r.get('creator') or 'ismeretlen szerző')[:80]} / {r.get('source') or 'Openverse'}",
                    "license": lic, "source_url": r.get("foreign_landing_url") or r["url"]})
        if len(out) >= limit:
            break
    return out


def find_image(specific: list, generic: list, timeout: int, avoid: Optional[set] = None) -> Optional[dict]:
    """Előbb a konkrét (név, hely, intézmény) keresések a Commonson, szigorúan; utána az általános
    témakép (Openverse, majd Commons lazábban). Ha semmi nem illik, inkább nincs kép, mint rossz kép."""
    for q in specific:
        q = str(q or "").strip()[:60]
        if re.search(r"parliament|országház", q, re.I) and len(specific) > 1:
            continue  # a Parlament-kép túl általános; csak végső esetben
        img = commons_search_image(q, timeout, strict=True, avoid=avoid)
        if img:
            return img
    for q in generic:
        q = str(q or "").strip()[:40]
        img = openverse_image(q, timeout, avoid) or commons_search_image(q, timeout, strict=False, avoid=avoid)
        if img:
            return img
    return None


def find_inline_images(raw: dict, n_paras: int, timeout: int, avoid: Optional[set] = None) -> list:
    """Szövegközi képek (max. 2): amit a cikk konkrétan említ (hajó, épület, eszköz, helyszín, személy), azt mutatjuk
    meg a megfelelő bekezdés után. Csak szigorú (nevében egyező) Commons-fotó jöhet; ha nincs, kimarad."""
    out, seen = [], set(avoid or ())
    for item in (raw.get("inline_images") or [])[:3]:
        if not isinstance(item, dict) or len(out) >= 2:
            continue
        q = str(item.get("query") or "").strip()[:60]
        if not q or re.search(r"parliament|országház", q, re.I):
            continue
        found = [im for im in commons_search_images(q, timeout, strict=True, avoid=seen, limit=2) if im["kind"] == "photo"]
        if not found:
            continue
        try:
            after = int(item.get("after", 1))
        except (TypeError, ValueError):
            after = 1
        after = max(0, min(after, n_paras - 2))
        if any(o["after"] == after for o in out):
            after = min(after + 1, n_paras - 2)
        seen.add(found[0]["url"])
        out.append({**found[0], "after": after, "caption": str(item.get("caption") or "").strip()[:160]})
    return out


def img_keys(im: Optional[dict]) -> set:
    """Egy kép azonosítói (URL és forrásoldal, lekérdezés nélkül) – így a Wikimedia bélyegkép és az eredeti, vagy a
    letöltött Pixabay-másolat és az eredeti is ugyanannak számít."""
    if not im or im.get("generated"):
        return set()
    return {k.split("?")[0] for k in (im.get("url"), im.get("source_url"), im.get("orig_url")) if k and "/info/" not in k}


def used_images(articles: list) -> set:
    """Az elmúlt IMG_REUSE_DAYS (alap 21) nap kint lévő (és függő) cikkeinek főképe: ugyanaz a fotó ne legyen két
    friss cikk főképe. Régebbi cikk képe (pl. egy politikus portréja) három hét után újra előkerülhet – a lapok is így
    dolgoznak, és enélkül a szabad licencű képkészlet hamar elfogyna."""
    out = set()
    since = (date.today() - timedelta(days=int(os.getenv("IMG_REUSE_DAYS", "21")))).isoformat()
    for a in articles:
        if (a.get("date") or a.get("created_at") or "9999")[:10] < since:
            continue
        out |= img_keys(a.get("hero_image"))
        rv, opts = a.get("review") or {}, a.get("image_options") or []
        if opts and 0 <= int(rv.get("image", 0)) < len(opts):  # függő cikk: a kiválasztott kép
            out |= img_keys(opts[int(rv.get("image", 0))])
    return out


def drop_used(images: list, avoid: Optional[set]) -> list:
    return [im for im in images if not (img_keys(im) & (avoid or set()))] if avoid else images


def find_images(specific: list, generic: list, timeout: int, avoid: Optional[set] = None, limit: int = 4) -> list:
    """Képjelöltek a Telegramos kiválasztáshoz: a konkrét találatok elöl, utána az általános hangulatképek."""
    out, seen = [], set(avoid or ())
    def add(items: list) -> None:
        for im in items:
            if im["url"] not in seen and len(out) < limit:
                seen.add(im["url"])
                out.append(im)
    for q in specific:
        q = str(q or "").strip()[:60]
        if q and not (re.search(r"parliament|országház", q, re.I) and len(specific) > 1):
            add(commons_search_images(q, timeout, strict=True, avoid=seen, limit=2))
    for q in generic:
        q = str(q or "").strip()[:40]
        if q and len(out) < limit:
            add(pexels_images(q, timeout, seen, limit=2))
            add(pixabay_images(q, timeout, seen, limit=2))
            add(openverse_images(q, timeout, seen, limit=2))
            if len(out) < limit:
                add(commons_search_images(q, timeout, strict=False, avoid=seen, limit=1))
    return out


# ---------------------------------------------------------------------------
# Képminőség: Pexels (prémium ingyenes fotók), AI-ellenőrzés (illik-e a kép), generált illusztráció
# ---------------------------------------------------------------------------

def pexels_images(query: str, timeout: int, avoid: Optional[set] = None, limit: int = 3) -> list:
    """Pexels: jó minőségű, szabadon használható fotók általános témákra (PEXELS_API_KEY kell, ingyenes)."""
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key or not query:
        return []
    req = urllib.request.Request(f"https://api.pexels.com/v1/search?per_page=8&orientation=landscape&query={_q(query)}",
                                 headers={"Authorization": key, "User-Agent": "KollektivaBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    out = []
    for p in data.get("photos") or []:
        url = (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("large")
        if not url or (avoid and url in avoid):
            continue
        out.append({"url": url, "width": p.get("width"), "height": p.get("height"), "kind": "photo",
                    "alt": (p.get("alt") or "")[:200], "credit": f"{(p.get('photographer') or 'ismeretlen')[:80]} / Pexels",
                    "license": "Pexels License", "source_url": p.get("url") or url})
        if len(out) >= limit:
            break
    return out


def pixabay_images(query: str, timeout: int, avoid: Optional[set] = None, limit: int = 3) -> list:
    """Pixabay: ingyenes, szabadon (kereskedelmi célra is) használható fotók (PIXABAY_API_KEY, pixabay.com/api/docs)."""
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key or not query:
        return []
    url = (f"https://pixabay.com/api/?key={urllib.parse.quote(key)}&q={urllib.parse.quote(query)}&image_type=photo"
           "&orientation=horizontal&safesearch=true&per_page=10&min_width=1200")
    data = http_get_json(url, timeout) or {}
    out = []
    for h in data.get("hits") or []:
        img = h.get("largeImageURL") or h.get("webformatURL")
        if not img or (avoid and img in avoid):
            continue
        out.append({"url": img, "width": h.get("imageWidth"), "height": h.get("imageHeight"), "kind": "photo",
                    "alt": (h.get("tags") or "")[:200], "credit": f"{(h.get('user') or 'ismeretlen')[:80]} / Pixabay",
                    "license": "Pixabay License", "source_url": h.get("pageURL") or img})
        if len(out) >= limit:
            break
    return out


VISION_SYSTEM = ("Képszerkesztő vagy egy magyar hírmagazinnál. Szigorúan pontozod, hogy a képek mennyire illenek a "
                 "cikkhez. Csak JSON-t adsz vissza.")


def _thumb_b64(url: str, timeout: int) -> Optional[str]:
    import base64
    u = re.sub(r"/(\d{3,4})px-", "/480px-", url) if "upload.wikimedia.org" in url else url
    try:
        req = urllib.request.Request(u, headers={"User-Agent": WIKI_UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=min(timeout, 30)) as resp:
            raw = resp.read(900_000)
            ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not ctype.startswith("image/") or len(raw) < 2000:
        return None
    return f"data:{ctype};base64," + base64.b64encode(raw).decode()


def more_images(ai: "AIClient", art: dict, avoid: set, want: int = 3) -> list:
    """Tartalék képkeresés: ha a szigorú keresés semmit (vagy alig valamit) talált, az AI a cikkből angol kulcsszavakat
    ad csökkenő fontossági sorrendben (konkrét személy/hely/tárgy → téma → hangulat), és ezeken megy végig."""
    try:
        r = ai.complete_json("Képszerkesztő vagy. Csak JSON-t adsz vissza.",
                             f"Cikk: {art['title']}\n{art.get('lead', '')}\nCímkék: {', '.join(art.get('tags') or [])}\n\n"
                             "Adj 6 angol képkereső kifejezést csökkenő fontossági sorrendben: előbb a konkrét szereplő, hely, "
                             "tárgy, aztán a téma, végül egy hangulatkép. JSON: {\"q\": [\"...\"]}", 300, light=True)
        queries = [str(x).strip()[:60] for x in r.get("q") or [] if str(x).strip()][:6]
    except (AIError, ValueError, TypeError, KeyError):
        queries = []
    out, seen = [], set(avoid)
    for q in queries:
        found = find_images([q], [], CFG_TIMEOUT, seen, limit=4)
        seen |= {im["url"] for im in found}
        out += vision_rank(ai, art["title"], art.get("lead", ""), found, min_score=4)
        if len(out) >= want:
            break
    if out:
        log.info("Tartalék képkeresés: %d kép (%s)", len(out), ", ".join(queries[:3]))
    return out[:want]


CFG_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))


def _clean_title(t: str) -> str:
    """Cím: nincs pont a végén (kérdő- és felkiáltójel maradhat)."""
    t = str(t).strip()
    if t.strip(" \"'").lower() in ("null", "none"):
        return ""
    return t[:-1].rstrip() if t.endswith(".") and not t.endswith("...") else t


def vision_rank(ai: "AIClient", title: str, lead: str, images: list, min_score: int = 5) -> list:
    """AI-szem: a Gemini megnézi a képjelölteket, és kidobja, ami nem illik a cikkhez (pl. bulvárcikkhez egy
    idegen ember, közéleti cikkhez egy random épület). Hibánál/kulcs nélkül változatlanul visszaadja a listát."""
    c = ai.cfg
    if (not images or not c.gemini_key or ai.provider == "mock"
            or os.getenv("VISION_CHECK", "true").lower() not in ("1", "true", "yes")):
        return images
    parts, kept = [], []
    for im in images[:8]:
        if im.get("generated"):
            continue
        b64 = _thumb_b64(im["url"], c.http_timeout)
        if b64:
            kept.append(im)
            parts.append({"type": "image_url", "image_url": {"url": b64}})
    if not kept:
        return images
    prompt = (f"Cikk címe: {title}\nBevezető: {lead}\n\nA következő {len(kept)} kép a cikk főképe lehetne. Pontozd 0–10-ig "
              "mindegyiket: 10 = pontosan azt mutatja, akiről/amiről a cikk szól (vagy nagyon kifejező hangulatkép); "
              "0 = nincs köze hozzá, félrevezető (pl. másik ember, másik ország, logó helyett random tárgy), rossz minőségű "
              "vagy szöveges/diagram. Ha a cikk konkrét személyről szól, más ember képe max. 2 pont. "
              'JSON: {"scores": [szám, ...]} – pontosan ' + str(len(kept)) + " szám, a képek sorrendjében.")
    models = [m.strip() for m in c.gemini_model.split(",") if m.strip()]
    for model in models[:2]:
        try:
            data = post_json("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                             {"Authorization": f"Bearer {c.gemini_key}"},
                             {"model": model, "max_tokens": 300, "response_format": {"type": "json_object"},
                              "messages": [{"role": "system", "content": VISION_SYSTEM},
                                           {"role": "user", "content": [{"type": "text", "text": prompt}, *parts]}]},
                             c.http_timeout, 2)
            scores = extract_json(data["choices"][0]["message"]["content"]).get("scores") or []
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Képellenőrzés (AI) sikertelen (%s): %s", model, str(e)[:160])
            scores = []
    if len(scores) != len(kept):
        return images
    ranked = sorted(((float(s), im) for s, im in zip(scores, kept) if float(s) >= min_score), key=lambda x: -x[0])
    log.info("Képellenőrzés: %d/%d kép maradt (pontok: %s)", len(ranked), len(kept), scores)
    for s, im in ranked:
        im["vision_score"] = s
    return [im for _, im in ranked]


GEN_SYSTEM = ("Art director vagy. Egy magyar hírmagazin cikkéhez írsz angol nyelvű képgenerálási promptot. "
              "Csak JSON-t adsz vissza.")


def localize_image(img: Optional[dict], timeout: int = 20) -> Optional[dict]:
    """Pixabay-kép: a feltételek szerint nem linkelhetjük tartósan az ő szerverükről, ezért kirakáskor
    letöltjük a repóba (public/img/px/), és onnan szolgáljuk ki. Más képeken nem változtat."""
    if not img or img.get("local") or "pixabay.com" not in str(img.get("url", "")):
        return img
    import hashlib
    try:
        req = urllib.request.Request(img["url"], headers=WIKI_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Pixabay-kép letöltése sikertelen: %s", e)
        return img
    if len(data) < 5000:
        return img
    out_dir = BASE_DIR / "public" / "img" / "px"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(img["url"].encode()).hexdigest()[:16] + ".jpg"
    (out_dir / name).write_bytes(data)
    return {**img, "url": f"{SITE_URL}/public/img/px/{name}", "local": str(out_dir / name), "orig_url": img["url"]}


LAST_GEN_ERROR = ""
CF_IMG_EXHAUSTED = False  # a Cloudflare napi ingyenes keret (10 000 neuron) elfogyott → ebben a futásban tartalék


def _gen_image(acct: str, token: str, prompt: str, timeout: int, seed: int) -> bytes:
    """Egy kép: Cloudflare FLUX-1-schnell (4 lépés – ennyire tervezték, fele annyi keretet fogyaszt, mint 8); ha a napi
    ingyenes keret elfogyott, a Pollinations nyílt FLUX-szolgáltatása (kulcs nélkül) a tartalék."""
    import base64
    global CF_IMG_EXHAUSTED
    if acct and token and not CF_IMG_EXHAUSTED:
        try:
            data = post_json(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                             {"Authorization": f"Bearer {token}"},
                             {"prompt": prompt[:2000] + ", no watermark, 16:9 composition", "steps": 4},
                             timeout, 1)
            img = base64.b64decode((data.get("result") or {}).get("image") or "")
            if len(img) >= 5000:
                note_usage("cf_image")
                return img
        except Exception as e:  # noqa: BLE001 – bármilyen Cloudflare-hiba (keret, 5006 bemeneti hiba, időtúllépés): tartalék
            msg = str(e)
            if "4006" in msg or "daily free allocation" in msg or "429" in msg:
                CF_IMG_EXHAUSTED = True
                log.warning("Cloudflare képkeret elfogyott – tartalék generátor.")
            else:
                log.warning("Cloudflare képgenerálás hiba (%s) – tartalék generátor.", msg[:200])
    if os.getenv("IMAGE_FALLBACK", "pollinations") != "pollinations":
        raise AIError("A Cloudflare napi képkerete elfogyott (10 000 neuron), és nincs tartalék generátor.")
    q = urllib.parse.quote(prompt[:700] + ", editorial illustration, no watermark")
    req = urllib.request.Request(f"https://image.pollinations.ai/prompt/{q}?width=1280&height=720&model=flux&nologo=true"
                                 f"&seed={seed}", headers={"User-Agent": WIKI_UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=max(timeout, 90)) as resp:
        img = resp.read()
    if len(img) < 5000:
        raise AIError("A tartalék képgenerátor üres képet adott.")
    note_usage("pollinations_image")
    return img


def generate_illustration(ai: "AIClient", title: str, lead: str, tag: str, n: int = 2) -> list:
    """Saját illusztráció a Cloudflare Workers AI-jal (FLUX, ingyenes napi keret): szerkesztőségi grafika, NEM fotó
    valós személyről (valódi embert nem generálunk le). Kell: CF_ACCOUNT_ID + CF_AI_TOKEN. A kép a repóba kerül."""
    import base64
    global LAST_GEN_ERROR
    LAST_GEN_ERROR = ""
    acct, token = os.getenv("CF_ACCOUNT_ID", "").strip(), os.getenv("CF_AI_TOKEN", "").strip()
    if (not acct or not token) and os.getenv("IMAGE_FALLBACK", "pollinations") != "pollinations":
        return []
    try:
        raw = ai.complete_json(GEN_SYSTEM, f"Cikk: {title}\n{lead}\n\nÍrj {n} eltérő promptot egy szerkesztőségi illusztrációhoz: "
                               "konkrét, a témát jól mutató jelenet vagy szimbolikus kompozíció. A stílust és a színeket a témához "
                               "válaszd szabadon (lehet fotószerű, festői, színes grafika, montázs). Lehetnek rajta emberek, arcok, "
                               "logók, zászlók (pontosan, torzítás nélkül), rövid felirat csak ha nem rontja a képet. Közszereplő "
                               "(pl. politikus) is szerepelhet a nevével (angolul írd bele, pl. 'Hungarian politician Péter Magyar'), "
                               "de csak semleges, a cikkhez illő helyzetben – megalázó, hamis vagy kompromittáló jelenet nem. "
                               "Ha a cikk egy konkrét közszereplőről szól, az ELSŐ prompt próbálja őt felismerhetően ábrázolni (nevével), "
                               "a MÁSODIK pedig ember nélkül, szimbolikusan mutassa a témát (helyszín, tárgyak). "
                               'JSON: {"prompts": ["...", "..."], "person": "a közszereplő neve, vagy üres"}', 1400, light=True)
        prompts = [str(p)[:900] for p in raw.get("prompts") or [] if str(p).strip()][:max(n, 2)]
        person = str(raw.get("person") or "").strip()
    except Exception as e:  # noqa: BLE001 – AI-hiba esetén egyszerű prompt a címből, a grafika így is elkészül
        log.warning("Illusztráció-prompt sikertelen, egyszerű prompttal megyek: %s", str(e)[:200])
        prompts, person = [], ""
    if not prompts:
        base = (f"Editorial illustration for a news magazine article titled \"{title}\". Expressive, colorful, "
                "rich detail, cinematic light")
        prompts = [base, base + ", wide establishing view"][:n]
    out_dir = BASE_DIR / "public" / "img" / "gen"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, p in enumerate(prompts):
        try:
            img = _gen_image(acct, token, p, ai.cfg.http_timeout, int(time.time()) % 100000 + i)
        except Exception as e:  # noqa: BLE001
            LAST_GEN_ERROR = str(e)[:300]
            log.warning("Illusztráció generálása sikertelen: %s", LAST_GEN_ERROR)
            continue
        if len(img) < 5000:
            continue
        name = f"{re.sub(r'[^a-z0-9]', '', tag.lower())[:10]}-{int(time.time())}-{i}.jpg"
        (out_dir / name).write_bytes(img)
        likeness = bool(person) and i == 0  # valós személyt ábrázoló kép: kötelező AI-jelölés
        out.append({"url": f"{SITE_URL}/public/img/gen/{name}", "local": str(out_dir / name), "kind": "photo",
                    "alt": title[:200], "likeness": likeness,
                    "credit": "AI-generált illusztráció – Kollektíva" if likeness else "Kollektíva illusztráció",
                    "license": "saját (AI-generált illusztráció)" if likeness else "saját grafika",
                    "source_url": f"{SITE_URL}/info/#impresszum", "generated": True})
    return out


def auto_illustration(ai: "AIClient", art: dict, tag: str) -> list:
    """Ha nem találtunk a cikkhez illő képet: magától generált grafika (AUTO_ILLUSTRATION=false kikapcsolja)."""
    if os.getenv("AUTO_ILLUSTRATION", "true").lower() not in ("1", "true", "yes"):
        return []
    try:
        gen = generate_illustration(ai, art.get("title", ""), art.get("lead", ""), tag)
    except Exception as e:  # noqa: BLE001
        log.warning("Automatikus grafika kimaradt: %s", str(e)[:200])
        return []
    if gen:
        log.info("Nincs illő kép – %d generált grafika készült.", len(gen))
    return gen


def wiki_onthisday_event(d: date, http_timeout: int) -> Optional[dict]:
    """Tartalék: a Wikipédia szerkesztők által válogatott „On this day” eseményei (en).
    A tények a Wikipédiából jönnek (esemény + cikkkivonat), az AI csak megfogalmaz."""
    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{d.month:02d}/{d.day:02d}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "KollektivaBot/1.0 (https://xn--kollektva-m5a.hu; szerkesztoseg@xn--kollektva-m5a.hu)",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("selected", [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warning("Wikipédia On this day nem elérhető: %s", e)
        return None

    min_age = 25
    candidates = []
    for it in items:
        text, year, pages = it.get("text", ""), it.get("year"), it.get("pages") or []
        if not text or not isinstance(year, int) or year > d.year - min_age or not pages:
            continue
        extracts = [p.get("extract", "") for p in pages[:2] if p.get("extract")]
        if WIKI_EXCLUDE.search(text) or not extracts:
            continue
        score = 2 if re.search(r"Hungar|Budapest", text + " ".join(extracts)) else 0
        candidates.append((score, it, pages, extracts))
    if not candidates:
        log.warning("Nincs megfelelő Wikipédia-esemény erre a napra (%s).", d.strftime("%m-%d"))
        return None
    best = max(c[0] for c in candidates)
    pool = [c for c in candidates if c[0] == best]
    _, it, pages, extracts = seeded_rng("wiki", d.isoformat()).choice(pool)
    main = pages[0]
    title = (main.get("normalizedtitle") or main.get("title", "")).replace("_", " ")
    sources = [{"url": p.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "title": (p.get("normalizedtitle") or p.get("title", "")).replace("_", " "),
                "publisher": "Wikipedia", "license": "CC BY-SA 4.0"}
               for p in pages[:2] if p.get("content_urls")]
    facts = [f'{it["year"]}: {it["text"]}', *extracts]
    image = None
    for pg in pages[:3]:
        t = (pg.get("normalizedtitle") or pg.get("title", "")).replace("_", " ")
        hu = wiki_hu_summary(t, http_timeout)
        if hu:
            facts.append(hu["extract"])
            sources.append({"url": hu["url"], "title": hu["title"], "publisher": "Wikipédia",
                            "license": "CC BY-SA 4.0"})
        sources.extend(wiki_external_sources(t, http_timeout, limit=1))
        if image is None:
            image = commons_image(pg, http_timeout)
    uniq, seen_src = [], set()
    for src in sources:  # azonos kiadó/URL csak egyszer
        key = src["url"] if src.get("publisher", "").startswith("Wiki") else src.get("publisher")
        if key and key not in seen_src:
            seen_src.add(key)
            uniq.append(src)
    sources = uniq
    return {
        "year": it["year"],
        "title": it["text"].rstrip(".")[:120] or title,
        "summary": it["text"],
        "facts": facts,
        "tags": [],
        "sources": sources,
        "image": image,
        "origin": "wikipedia",
        "language": "en",
    }


def pick_event(events: dict, d: date) -> Optional[dict]:
    """Az adott naphoz tartozó kurált eseményekből évente rotálva választ."""
    candidates = events.get(f"{d.month:02d}-{d.day:02d}", [])
    if not candidates:
        return None
    return candidates[d.year % len(candidates)]


def validate_retro(raw: dict) -> dict:
    body = raw.get("body")
    if isinstance(body, str):
        body = [p.strip() for p in body.split("\n\n") if p.strip()]
    if not raw.get("title") or not raw.get("lead") or not isinstance(body, list) or len(body) < 3:
        raise AIError("Hiányos retro cikk (title/lead/body)")
    title = re.sub(r"^\s*ekkor történt\s*[:–-]\s*", "", str(raw["title"]).strip(), flags=re.I)
    return {
        "title": title[:1].upper() + title[1:],
        "lead": str(raw["lead"]).strip(),
        "body": [str(p).strip() for p in body if str(p).strip()],
        "pull_quote": str(raw.get("pull_quote", "")).strip(),
        "tags": [str(t).strip() for t in raw.get("tags", [])][:5],
    }


def fallback_retro(event: dict) -> dict:
    """AI nélkül a kurált tényekből épít rövidebb, de korrekt cikket."""
    facts = event.get("facts", [])
    return {
        "title": event["title"],
        "lead": event.get("summary", facts[0] if facts else ""),
        "body": facts or [event.get("summary", "")],
        "pull_quote": "",
        "tags": event.get("tags", []),
    }


def normalize_sources(raw_sources: list, accessed_at: str) -> list:
    """Forrásokat a séma szerinti objektumokká alakít ({url, publisher, title, license, accessed_at})."""
    out = []
    for src in raw_sources or []:
        if isinstance(src, str):
            src = {"url": src}
        if not isinstance(src, dict) or not src.get("url"):
            continue
        host = re.sub(r"^https?://(www\.)?", "", src["url"]).split("/")[0]
        out.append({
            "url": src["url"],
            "title": src.get("title", ""),
            "publisher": src.get("publisher") or host,
            "license": src.get("license"),
            "accessed_at": src.get("accessed_at", accessed_at),
        })
    return out


def to_markdown(article: dict) -> str:
    """A cikk törzse Markdownban (a séma `content` mezője)."""
    parts = []
    if article.get("pull_quote"):
        parts.append(f"> {article['pull_quote']}")
    parts.extend(article.get("body", []))
    return "\n\n".join(parts)


def slugify(text: str) -> str:
    table = str.maketrans("áéíóöőúüű", "aeiooouuu")
    return re.sub(r"[^a-z0-9]+", "-", text.lower().translate(table)).strip("-")[:80].rstrip("-")


def build_retro_article(ai: AIClient, d: date, tz: ZoneInfo, events: dict) -> Optional[dict]:
    event = pick_event(events, d)
    if not event:
        log.info("Nincs kurált esemény erre a napra (%s) – Wikipédia-tartalék.", d.strftime("%m-%d"))
        if not ai.enabled:
            log.warning("AI nélkül a Wikipédia-tartalék nem használható – retro cikk kimarad.")
            return None
        event = wiki_onthisday_event(d, ai.cfg.http_timeout)
        if not event:
            return None

    source = "fallback"
    article = None
    if ai.enabled:
        try:
            article = validate_retro(ai.complete_json(RETRO_SYSTEM, retro_prompt(event, d), 4000))
            source = ai.label
        except (AIError, ValueError, TypeError, KeyError) as e:
            log.error("Retro AI generálás sikertelen: %s – fallback cikk", e)
    if article is None:
        if event.get("origin") == "wikipedia":
            log.warning("Wikipédia-eseményből AI nélkül nem készül cikk – kimarad.")
            return None
        article = fallback_retro(event)

    now_iso = datetime.now(tz).isoformat(timespec="seconds")
    full_text = " ".join([article["lead"], *article["body"]])
    sources = normalize_sources(event.get("sources", []), now_iso)
    if not sources:
        log.warning("A kurált eseménynek nincs forrása – a séma legalább egyet vár: %s", event["title"])
    is_ai = source.startswith("ai:")
    slug = slugify(f"{d.isoformat()}-{article['title']}")
    auto_publish = os.getenv("RETRO_AUTO_PUBLISH", "false").lower() in ("1", "true", "yes")
    status = "published" if (not is_ai or auto_publish) else "needs_review"
    return {
        # --- Azonosítás, állapot ---
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://kollektiva.hu/retro/{slug}")),
        "slug": slug,
        "status": status,
        "category": "retro",
        "subcategory": None,
        "tags": article["tags"],
        # --- Tartalom ---
        "title": article["title"],
        "subtitle": None,
        "lead": article["lead"],
        "content": to_markdown(article),
        "content_format": "markdown",
        "body": article["body"],               # kényelmi mező a statikus frontendnek
        "pull_quote": article["pull_quote"] or None,
        "reading_time_min": reading_time(full_text),
        "word_count": len(re.findall(r"\w+", full_text)),
        "locale": "hu-HU",
        "hero_image": event.get("image"),      # szabad licencű Wikimedia Commons kép (ha van)
        # --- Források, szerzőség ---
        "sources": sources,
        "authorship": {
            "mode": "ai_generated" if is_ai else "human",
            "byline": "Kollektíva szerkesztőség",
            "model": source.split(":", 2)[-1] if is_ai else None,
            "prompt_version": "retro-v1" if is_ai else None,
            "reviewed_by": None,
            "reviewed_at": None,
        },
        # --- Rovatspecifikus ---
        "category_meta": {
            "event_year": event["year"],
            "event_date": f"{d.month:02d}-{d.day:02d}",
            "event_title": event["title"],
        },
        "date": d.isoformat(),                 # kényelmi mezők a statikus frontendnek
        "date_label": f"{HU_MONTHS[d.month - 1]} {d.day}.",
        "url": f"/retro/{slug}/",              # saját, statikus cikkoldal
        # --- SEO, monetizáció ---
        "seo": {
            "meta_title": article["title"][:60],
            "meta_description": article["lead"][:160],
            "canonical_url": f"{SITE_URL}/retro/{slug}/",
            "og_image": None,
            "noindex": status != "published",  # csak a publikált cikk indexelhető
            "schema_type": "Article",
        },
        "monetization": {
            "ads_enabled": True,
            "brand_safety": event.get("brand_safety", "safe"),
            "sponsored": False,
            "sponsor_name": None,
            "affiliate_links": False,
        },
        # --- Pipeline ---
        "related_ids": [],
        "dedupe_hash": hashlib.sha256(f"retro|{event['title'].lower()}|{sources[0]['url'] if sources else ''}".encode()).hexdigest(),
        "pipeline_run_id": os.getenv("GITHUB_RUN_ID"),
        "generator": source,
        "created_at": now_iso,
        "updated_at": now_iso,
        "published_at": now_iso if status == "published" else None,
        "expires_at": None,
    }


def update_retro_archive(path: Path, article: dict, limit: int) -> dict:
    """Hozzáadja a cikket az archívumhoz (azonos napot felülír), legújabb elöl, max `limit` db."""
    data = read_json(path, {"articles": []})
    articles = [a for a in data.get("articles", []) if a.get("date") != article["date"]]
    articles.insert(0, article)
    articles.sort(key=lambda a: a.get("date", ""), reverse=True)
    return {"schema_version": 1, "updated_at": article["updated_at"], "articles": articles[:limit]}


# ---------------------------------------------------------------------------
# 3) Statikus oldalak: cikkoldalak, retro archívum, sitemap, hírsitemap, RSS
# ---------------------------------------------------------------------------
# A Cloudflare Pages a repó gyökerét szolgálja ki; a gyökérben lévő `_redirects`
# a /retro/*, /sitemap.xml, /news-sitemap.xml és /feed.xml címeket a public/ alá irányítja.

E = html.escape

PAGE_CSS = """
:root{--night:#0E1024;--vault:#171A36;--line:#2A2D52;--parch:#ECE6D8;--dusk:#9492B3;--brass:#C9A45C}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--night);color:var(--parch);font:17px/1.75 Manrope,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--parch)}a:hover{color:var(--brass)}
header,main,footer{max-width:720px;margin:0 auto;padding:0 20px}
.share{position:relative;display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:28px 0 8px;padding-top:18px;border-top:1px solid var(--line)}.share .sh-main{display:inline-flex;align-items:center;gap:8px;font:600 14px/1 Manrope,system-ui,sans-serif;color:#0E1024;background:var(--brass);border:0;border-radius:999px;padding:11px 18px;cursor:pointer}.sh-pop{display:flex;flex-wrap:wrap;gap:8px}.sh-pop[hidden]{display:none}.share .sh-save,.sh-pop a,.sh-pop button{display:inline-flex;align-items:center;gap:6px;font:600 13px/1 Manrope,system-ui,sans-serif;color:var(--parch);background:transparent;border:1px solid var(--line);border-radius:999px;padding:9px 14px;text-decoration:none;cursor:pointer}.share .sh-save:hover,.share .sh-save[data-on="1"],.sh-pop a:hover,.sh-pop button:hover{border-color:var(--brass);color:var(--brass)}.share .sh-save[data-on="1"] svg{fill:currentColor}.share .sh-main:hover,.share .sh-main:focus,.share .sh-main:active{color:#0E1024;background:#D8B46B}.share .sh-main:active,.share .sh-save:active{transform:scale(.96)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3FBF6F;box-shadow:0 0 0 3px rgba(63,191,111,.2);margin-right:6px;vertical-align:middle}.pbtn{display:inline-block;margin-top:12px;background:var(--brass);color:#0E1024!important;font:600 13px/1 Manrope,system-ui,sans-serif;border-radius:999px;padding:9px 14px;text-decoration:none}.pbtn:hover{background:#D8B46B}.tagrow{display:flex;align-items:flex-start;gap:10px;margin:10px 0 0}.tagrow .tags{margin:0;flex:1}.sh-top{flex:none;margin-left:auto;display:inline-flex;align-items:center;justify-content:center;width:42px;height:42px;padding:0;border-radius:50%;border:1px solid var(--brass);background:transparent;color:var(--brass);cursor:pointer}.box.poll{max-width:560px}.share .sh-src{display:inline-flex;align-items:center;gap:6px;font:600 13px/1 Manrope,system-ui,sans-serif;color:var(--parch);background:transparent;border:1px solid var(--line);border-radius:999px;padding:9px 14px;cursor:pointer}.sh-login{margin:4px 0 12px;padding:14px 16px;background:var(--vault);border:1px solid var(--brass);border-radius:12px;font:14px/1.45 Manrope,system-ui,sans-serif;color:var(--parch)}.sh-login[hidden]{display:none}.sh-login p{margin:0 0 10px}.sh-login .gbtn{display:inline-flex;align-items:center;gap:10px;padding:10px 16px;border-radius:999px;border:0;background:#fff;color:#1f1f1f;font:600 14px Manrope,system-ui,sans-serif;cursor:pointer}.sh-login .lx{margin-left:10px;background:transparent;border:0;color:var(--dusk);font:13px Manrope,system-ui,sans-serif;cursor:pointer;text-decoration:underline}.share .sh-src[aria-expanded=true]{border-color:var(--brass);color:var(--brass)}.srclist{margin:10px 0 0;padding:14px 18px;background:var(--vault);border:1px solid var(--line);border-radius:12px;font-size:14px;color:var(--dusk)}.srclist[hidden]{display:none}.share>button{box-sizing:border-box;flex:1 1 0;min-width:0;justify-content:center;height:44px;padding:0 10px!important;white-space:nowrap;font-size:13.5px!important}.share>button svg{flex:none}.share .sh-main{border:1px solid var(--brass)}.share .sh-pop{flex-basis:100%}@media(max-width:430px){.share{gap:6px}.share>button{font-size:13px!important;padding:0 6px!important;gap:5px!important}}@media(max-width:380px){.share>button{font-size:12.5px!important}.share>button svg{width:14px;height:14px}}.sh-top:hover{border-color:var(--brass)}.tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}.tags a{font-size:12px;color:var(--dusk);border:1px solid var(--line);border-radius:999px;padding:4px 10px;text-decoration:none}.tags a:hover{color:var(--brass);border-color:var(--brass)}.poll b{color:var(--brass);font-size:13px;letter-spacing:.12em;text-transform:uppercase}.poll h3{margin:6px 0 12px;font:600 22px/1.3 "Cormorant Garamond",Georgia,serif}.poll button{display:block;width:100%;text-align:left;margin:6px 0;padding:11px 14px;border:1px solid var(--line);border-radius:12px;background:transparent;color:var(--parch);font:15px Manrope,system-ui,sans-serif;cursor:pointer}.poll button:hover{border-color:var(--brass)}.poll .pr{position:relative;overflow:hidden;margin:6px 0;padding:11px 14px;border:1px solid var(--line);border-radius:12px;display:flex;justify-content:space-between;gap:10px}.poll .pr.me{border-color:var(--brass)}.poll .pr span{position:absolute;inset:0 auto 0 0;background:rgba(201,164,92,.15)}.poll .pr em,.poll .pr strong{position:relative;font-style:normal}.poll small{color:var(--dusk)}
.hdr-r{display:flex;align-items:center;gap:10px}.srch{display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;border:1px solid var(--line);border-radius:999px;color:var(--parch)}.srch:hover{color:var(--brass);border-color:var(--brass)}
.gpref{text-align:center;font-size:12px;padding:5px 0;border-bottom:1px solid var(--line)}.gpref a{color:var(--brass);text-decoration:none}.gpref a:hover{color:var(--parch)}.gpref b{color:var(--brass);font-weight:400}
header{display:flex;justify-content:space-between;align-items:center;padding-top:14px;padding-bottom:14px;border-bottom:1px solid var(--line);position:relative}
.menu summary{list-style:none;cursor:pointer;color:var(--dusk);font-size:14px;padding:6px 12px;border:1px solid var(--line);border-radius:999px}
.menu summary::-webkit-details-marker{display:none}
.menu[open] nav{position:absolute;right:20px;top:58px;z-index:60;display:flex;flex-direction:column;gap:10px;min-width:280px;max-height:calc(100vh - 90px);overflow:auto;padding:16px 18px;background:var(--vault);border:1px solid var(--line);border-radius:12px}
.menu nav a{color:var(--parch)}.menu nav .mh{color:var(--brass);font-size:11px;letter-spacing:.16em;text-transform:uppercase}.menu nav .mg{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px}.menu nav hr{width:100%;border:0;border-top:1px solid var(--line);margin:4px 0}
.keypoints{margin:28px 0;padding:16px 20px;border-left:2px solid var(--brass);background:var(--vault);border-radius:0 12px 12px 0}
.keypoints p{margin:0 0 6px;color:var(--brass);font-size:13px;letter-spacing:.12em;text-transform:uppercase}
.keypoints ul{margin:0;padding-left:18px}
article ul li{margin:4px 0;color:rgba(236,230,216,.88)}
details.box summary{cursor:pointer;color:var(--parch);font-weight:600}
.related{margin:48px 0 0}.related h2{font-size:26px;margin-bottom:14px}
.rel-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:18px}
.rel-grid a{text-decoration:none;display:block}.rel-grid img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:var(--vault)}
.rel-grid img.graphic{object-fit:contain;padding:14px;background:#ECE6D8}.rel-grid img.top,.slist img.top{object-position:50% 15%}
.rel-grid .t{margin-top:3px;font-weight:600;line-height:1.35;color:var(--parch)}.rel-grid .k{margin-top:12px;font-size:12px;line-height:1.3;color:var(--brass);text-transform:uppercase;letter-spacing:.1em}
.logo{display:flex;align-items:center;gap:10px;font:600 26px/1 "Cormorant Garamond",Georgia,serif;text-decoration:none}
.logo svg{height:38px;width:auto;flex:none}.logo b{color:var(--brass);font-weight:600}
.menu nav{display:none}nav a{text-decoration:none;font-size:15px}
footer a{color:var(--dusk);margin-right:10px}
.kicker{margin-top:44px;color:var(--brass);font-size:13px;letter-spacing:.14em;text-transform:uppercase}
h1{font:600 clamp(32px,6vw,48px)/1.15 "Cormorant Garamond",Georgia,serif;margin:12px 0 16px}
h2{font:600 28px/1.25 "Cormorant Garamond",Georgia,serif;margin:0 0 6px}
.meta{color:var(--dusk);font-size:14px}
.lead{font-size:20px;line-height:1.6;color:var(--parch)}
blockquote{margin:32px 0;padding-left:18px;border-left:2px solid var(--brass);font:italic 24px/1.4 "Cormorant Garamond",Georgia,serif}
blockquote.q{color:var(--parch)}blockquote cite{display:block;margin-top:8px;font:600 13px/1.4 Manrope,system-ui,sans-serif;font-style:normal;letter-spacing:.04em;color:var(--brass)}
article p{color:rgba(236,230,216,.88)}
.box{margin:40px 0;padding:18px 20px;background:var(--vault);border:1px solid var(--line);border-radius:12px;font-size:14px;color:var(--dusk)}
.box a{color:var(--parch)}.seealso b{color:var(--brass);font-size:13px;letter-spacing:.12em;text-transform:uppercase}.seealso ul{margin:8px 0 0;padding-left:18px}.seealso span{color:var(--dusk)}
.vid{position:relative;aspect-ratio:16/9;margin:28px 0;border-radius:12px;overflow:hidden;background:var(--vault)}.vid iframe{position:absolute;inset:0;width:100%;height:100%;border:0}figure.hero img{max-height:min(56vh,480px);object-fit:cover;object-position:center 30%}figure{margin:32px 0}figure img{display:block;width:100%;height:auto;max-height:70vh;object-fit:contain;border-radius:12px;background:var(--vault)}
figure.graphic img{max-height:340px;padding:28px;background:#ECE6D8}
figcaption{margin-top:6px;color:var(--dusk);font-size:12px}figcaption a{color:var(--dusk)}figcaption .cap{font-size:14px;color:rgba(236,230,216,.75)}
.credit summary{list-style:none;cursor:pointer;display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border:1px solid var(--line);border-radius:50%;font-size:12px}
.credit summary::-webkit-details-marker{display:none}.credit[open] summary{margin-right:8px}
.slist{list-style:none;padding:0;margin:28px 0}
.slist li{border-bottom:1px solid var(--line)}
.slist a{display:grid;grid-template-columns:120px 1fr;gap:16px;padding:16px 0;text-decoration:none;align-items:start}
.slist img,.slist .ph{width:120px;aspect-ratio:4/3;object-fit:cover;border-radius:10px;background:var(--vault)}
.slist img.graphic{object-fit:contain;padding:8px;background:#ECE6D8}
.slist .ph{display:flex;align-items:center;justify-content:center;color:var(--brass);font:600 15px "Cormorant Garamond",Georgia,serif;background:linear-gradient(135deg,#171A36,#2A2D52)}
.slist .t{font-weight:600;font-size:17px;line-height:1.35;color:var(--parch)}.slist .m{font-size:12px;color:var(--dusk);margin-top:4px}
.slist .l{font-size:14px;color:var(--dusk);margin-top:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.slist .y{color:var(--brass);font:600 14px "Cormorant Garamond",Georgia,serif}
@media (min-width:640px){.slist a{grid-template-columns:180px 1fr}.slist img,.slist .ph{width:180px}}
.thumb{display:block;width:100%;aspect-ratio:16/9;height:auto;object-fit:cover;object-position:center;border-radius:10px;margin:10px 0 12px}
.thumb.graphic{object-fit:contain;padding:24px;background:#ECE6D8}
.list{list-style:none;padding:0;margin:32px 0}
.list li{padding:22px 0;border-bottom:1px solid var(--line)}
.year{color:var(--brass);font:600 34px/1 "Cormorant Garamond",Georgia,serif}
footer{margin-top:60px;padding-top:24px;padding-bottom:40px;border-top:1px solid var(--line);color:var(--dusk);font-size:13px}
"""


# Betöltő animáció (K + keringő hold) – csak 150 ms feletti várakozásnál látszik
# Google „kedvenc forrás” link: csak akkor jelenjen meg, ha a Google már felvett a forrásai közé (GOOGLE_PREF_LINK=true)
_GPREF_URL = "https://www.google.com/preferences/source?q=xn--kollektva-m5a.hu"
GPREF_ON = os.getenv("GOOGLE_PREF_LINK", "false").lower() in ("1", "true", "yes")
GPREF_HTML = (f'\n<div class="gpref"><a href="{_GPREF_URL}" target="_blank" rel="noopener"><b>★</b> Kedvenc forrás a Google-ben</a></div>'
              if GPREF_ON else "")
GPREF_FOOT = (f'<br><a href="{_GPREF_URL}" target="_blank" rel="noopener">★ Kollektíva kedvenc forrásként a Google-ben</a>'
              if GPREF_ON else "")

NEW_TOAST_HTML = r"""<div id="knew" role="status" aria-live="polite" style="position:fixed;left:50%;top:14px;transform:translate(-50%,-180%);transition:transform .35s ease;z-index:60;max-width:min(420px,calc(100vw - 32px));display:flex;align-items:center;gap:8px;background:#B3261E;color:#fff;border-radius:999px;padding:5px 6px 5px 12px;box-shadow:0 6px 20px rgba(0,0,0,.4);font:600 12px/1.3 Manrope,system-ui,sans-serif"><span style="flex:none;width:6px;height:6px;border-radius:50%;background:#fff;animation:knp 1.2s infinite"></span><a id="knewA" href="/" style="color:#fff;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></a><button type="button" aria-label="Bezár" onclick="document.getElementById('knew').style.transform='translate(-50%,-180%)'" style="flex:none;background:rgba(255,255,255,.18);border:0;color:#fff;border-radius:50%;width:20px;height:20px;cursor:pointer;font-size:13px;line-height:1">×</button></div>
<style>@keyframes knp{50%{opacity:.25}}</style>
<script>/* Új cikk értesítő: ha az oldalon tartózkodás közben új cikk kerül ki, alul egy piros sáv jelzi. */
(function(){var N={kozelet:'Közélet',vilag:'Világ',penzvilag:'Pénzvilág',tech:'Tech',eletmod:'Életmód',kultura:'Kultúra',univerzum:'Univerzum',bulvar:'Bulvár'};
var known=null,box=document.getElementById('knew'),a=document.getElementById('knewA'),t;
function get(){return fetch('/data/articles.json',{cache:'no-cache'}).then(function(r){if(!r.ok)throw 0;return r.json()}).catch(function(){return fetch('/public/data/articles.json',{cache:'no-cache'}).then(function(r){return r.json()})})}
function check(){if(document.hidden)return;get().then(function(d){var arts=(d.articles||[]).filter(function(x){return x.status==='published'&&x.url});
if(!known){known={};arts.forEach(function(x){known[x.id]=1});return}
var fresh=arts.filter(function(x){return !known[x.id]&&x.url!==location.pathname});arts.forEach(function(x){known[x.id]=1});
if(!fresh.length)return;var n=fresh[0];a.href=n.url;a.textContent='ÚJ · '+(N[n.category]||'Friss')+' · '+n.title+(fresh.length>1?'  (+'+(fresh.length-1)+')':'');
box.style.transform='translate(-50%,0)';clearTimeout(t);t=setTimeout(function(){box.style.transform='translate(-50%,-180%)'},/\/(kozelet|vilag|penzvilag|tech|eletmod|kultura|univerzum|bulvar|retro)\/./.test(location.pathname)?3500:6000)}).catch(function(){})}
check();setInterval(check,90000);document.addEventListener('visibilitychange',check)})();</script>"""

LOADER_HTML = r"""<div id="kload" class="on" aria-hidden="true"><svg viewBox="-480 -340 960 680"><defs><clipPath id="klFront"><rect x="-520" y="-220" width="1040" height="220"/></clipPath></defs><g transform="translate(-20 60) rotate(-24)"><ellipse rx="430" ry="130" fill="none" stroke="#2A2D52" stroke-width="14"/><circle r="38" fill="#C9A45C" stroke="#0E1024" stroke-width="11"><animateMotion dur="1.1s" repeatCount="indefinite" path="M430,0 a430,130 0 1,1 -860,0 a430,130 0 1,1 860,0"/></circle></g><path transform="translate(-344.3,311.6) scale(1,-1)" fill="#ECE6D8" d="M109.3255615234375 81V544Q109.3255615234375 573 103.91302490234375 587.5Q98.50048828125 602 82.03790283203125 607.5Q65.5753173828125 613 33.3751220703125 613Q30.650146484375 613 30.650146484375 619.0Q30.650146484375 625 33.3751220703125 625Q58.8250732421875 625 90.13751220703125 623.5Q121.449951171875 622 156.349853515625 622Q193.5247802734375 622 224.69970703125 623.5Q255.8746337890625 625 280.3245849609375 625Q283.049560546875 625 283.049560546875 619.0Q283.049560546875 613 280.3245849609375 613Q248.1243896484375 613 232.0242919921875 607.0Q215.9241943359375 601 210.149169921875 586.0Q204.3741455078125 571 204.3741455078125 542V81Q204.3741455078125 52 209.649169921875 37.0Q214.9241943359375 22 231.16180419921875 17.0Q247.3994140625 12 280.3245849609375 12Q283.7745361328125 12 283.7745361328125 6.0Q283.7745361328125 0 280.3245849609375 0Q254.8746337890625 0 224.19970703125 1.0Q193.5247802734375 2 156.349853515625 2Q121.449951171875 2 89.2750244140625 1.0Q57.10009765625 0 31.650146484375 0Q29.650146484375 0 29.650146484375 6.0Q29.650146484375 12 31.650146484375 12Q64.5753173828125 12 81.1754150390625 17.0Q97.7755126953125 22 103.550537109375 37.0Q109.3255615234375 52 109.3255615234375 81ZM368.650146484375 145 224.2244873046875 335.2254638671875 293.7735595703125 399.29931640625 444.29833984375 200.1749267578125Q485.49853515625 144.0748291015625 514.0111694335938 108.2998046875Q542.5238037109375 72.5247802734375 561.9863891601562 52.43731689453125Q581.448974609375 32.349853515625 596.2740478515625 23.76239013671875Q611.09912109375 15.1749267578125 625.149169921875 13.58746337890625Q639.19921875 12 656.0242919921875 12Q659.0242919921875 12 659.0242919921875 6.0Q659.0242919921875 0 656.0242919921875 0Q611.749267578125 0 584.749267578125 0.0Q557.749267578125 0 544.0242919921875 0Q531.2244873046875 0 522.0121459960938 -0.86248779296875Q512.7998046875 -1.7249755859375 505.6248779296875 -1.7249755859375Q492.349853515625 -1.7249755859375 482.89990234375 3.2750244140625Q473.449951171875 8.2750244140625 460.7750244140625 23.2750244140625Q448.10009765625 38.2750244140625 426.650146484375 67.2750244140625Q405.2001953125 96.2750244140625 368.650146484375 145ZM143.449951171875 267.90087890625 401.1749267578125 529.70068359375Q438.449951171875 566.9757080078125 430.3250732421875 589.9878540039062Q422.2001953125 613 371.3751220703125 613Q368.650146484375 613 368.650146484375 619.0Q368.650146484375 625 371.3751220703125 625Q397.550048828125 625 424.58746337890625 623.5Q451.6248779296875 622 495.9747314453125 622Q540.949462890625 622 567.1618041992188 623.5Q593.3741455078125 625 617.7239990234375 625Q620.7239990234375 625 620.7239990234375 619.0Q620.7239990234375 613 617.7239990234375 613Q575.0242919921875 613 522.7745361328125 589.9378051757812Q470.5247802734375 566.8756103515625 426.349853515625 523.70068359375L169.6248779296875 266.0009765625Z"/><path d="M-412.8,234.9 A430,130 -24 0 1 372.8,-114.9" fill="none" stroke="#0E1024" stroke-width="40"/><path d="M-412.8,234.9 A430,130 -24 0 1 372.8,-114.9" fill="none" stroke="#ECE6D8" stroke-width="16" stroke-linecap="round" opacity=".85"/><g transform="translate(-20 60) rotate(-24)" clip-path="url(#klFront)"><circle r="38" fill="#C9A45C" stroke="#0E1024" stroke-width="11"><animateMotion dur="1.1s" repeatCount="indefinite" path="M430,0 a430,130 0 1,1 -860,0 a430,130 0 1,1 860,0"/></circle></g></svg></div>
<style>#kload{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(14,16,36,.94);opacity:0;visibility:hidden;pointer-events:none}#kload svg{width:110px;height:auto}#kload.on{visibility:visible;pointer-events:auto;animation:kload-in .15s .15s both}@keyframes kload-in{from{opacity:0}to{opacity:1}}</style>
<script>(function(){var L=document.getElementById('kload');function off(){L.classList.remove('on')}if(document.readyState!=='loading')off();else document.addEventListener('DOMContentLoaded',off);setTimeout(off,2500);addEventListener('pageshow',off);document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a||e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||a.target==='_blank'||a.hasAttribute('download'))return;var u;try{u=new URL(a.href,location.href)}catch(x){return}if(u.origin!==location.origin||(u.pathname===location.pathname&&u.search===location.search))return;L.classList.add('on');setTimeout(off,4000)});})();</script>
"""


_KP = re.search(r'fill="#ECE6D8" d="([^"]+)"', LOADER_HTML).group(1)
LOGO_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="-438 -324 837 647" aria-hidden="true">'
            '<path d="M372.8,-114.9 A430,130 -24 0 1 -412.8,234.9" fill="none" stroke="#ECE6D8" stroke-width="18" stroke-linecap="round"/>'
            f'<path transform="translate(-344.3,311.6) scale(1,-1)" fill="#ECE6D8" d="{_KP}"/>'
            '<path d="M-412.8,234.9 A430,130 -24 0 1 372.8,-114.9" fill="none" stroke="#0E1024" stroke-width="40"/>'
            '<path d="M-412.8,234.9 A430,130 -24 0 1 372.8,-114.9" fill="none" stroke="#ECE6D8" stroke-width="18" stroke-linecap="round"/>'
            '<circle cx="351.7" cy="-38.7" r="47" fill="#0E1024"/><circle cx="351.7" cy="-38.7" r="36" fill="#C9A45C"/></svg>')


def _page(title: str, description: str, canonical: str, body: str, head_extra: str = "",
          noindex: bool = False) -> str:
    robots = "noindex,follow" if noindex else "index,follow,max-image-preview:large"
    return f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)}</title>
<meta name="description" content="{E(description)}">
<meta name="robots" content="{robots}">
<link rel="canonical" href="{E(canonical)}">
<meta name="theme-color" content="#0E1024">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:title" content="{E(title)}">
<meta property="og:description" content="{E(description)}">
<meta property="og:url" content="{E(canonical)}">
<meta property="og:locale" content="hu_HU">
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME} – Retro" href="{SITE_URL}/feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,600;1,500&family=Manrope:wght@400;600&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style>
{head_extra}
</head>
<body>
{LOADER_HTML}
<header><a class="logo" href="/" aria-label="{SITE_NAME} – főoldal">{LOGO_SVG}<span>{SITE_NAME}<b>.</b></span></a><span class="hdr-r"><a class="srch" href="/?kereses" aria-label="Keresés a cikkek között"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></a><details class="menu"><summary aria-label="Menü">☰ Menü</summary><nav>{MENU_HTML}</nav></details></span></header>{GPREF_HTML}
<main>
{body}
</main>
<footer>© {datetime.now().year} {SITE_NAME}<br><a href="/">Főoldal</a>{NAV_LINKS}<a href="/feed.xml">RSS</a><br><a href="/info/#impresszum">Impresszum</a><a href="/info/#adatkezeles">Adatkezelés</a><a href="/info/#sutik">Sütik</a><a href="/info/#hirdetes">Hirdetés</a>{GPREF_FOOT}</footer>
{NEW_TOAST_HTML}
<script src="/poll-widget.js?v=10" defer></script>
<script>try{{navigator.sendBeacon("/api/olvas",JSON.stringify({{p:location.pathname,r:document.referrer}}))}}catch(e){{}}</script>
</body>
</html>
"""


def _article_jsonld(a: dict) -> str:
    url = a["seo"]["canonical_url"]
    data = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": a["title"][:110],
        "description": a["lead"],
        "datePublished": a.get("published_at") or a.get("created_at"),
        "dateModified": a.get("updated_at") or a.get("created_at"),
        "inLanguage": "hu-HU",
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "url": url,
        "articleSection": SECTIONS.get(a.get("category"), {}).get("name", "Retro"),
        "keywords": ", ".join(a.get("tags", [])),
        "author": {"@type": "Organization", "name": a["authorship"]["byline"], "url": SITE_URL},
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": SITE_URL},
        "isBasedOn": [s["url"] for s in a.get("sources", []) if s.get("url")],
    }
    if (a.get("hero_image") or {}).get("url"):
        data["image"] = [a["hero_image"]["url"]]
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>"


BULLET = re.compile(r"^[-•*]\s+")


LINK_MD = re.compile(r"\[([^\]\n]{2,90})\]\((/[a-z0-9/_-]+/|https?://[^\s)]+)\)")


def _links(escaped: str) -> str:
    """[szöveg](url) -> link (a szöveg már HTML-escape-elt); belső link ugyanabban az ablakban, külső újban."""
    def rep(m):
        url = m.group(2)
        ext = url.startswith("http")
        return (f'<a href="{url}"' + (' target="_blank" rel="noopener"' if ext else "") + f">{m.group(1)}</a>")
    return LINK_MD.sub(rep, escaped)


def strip_bad_links(body: list, allowed: set) -> list:
    """Csak a megengedett (saját korábbi cikk) linkek maradnak; a többinél a link szövege marad, a link kikerül."""
    return [LINK_MD.sub(lambda m: m.group(0) if m.group(2) in allowed else m.group(1), str(p)) for p in body]


CITE_LEAK = re.compile(r"\s*\[\d{1,2}\](?!\()(?:\s*,?\s*\[\d{1,2}\](?!\())*")


def _para(p: str) -> str:
    """Bekezdés -> HTML; a „- ” kezdetű sorokból felsorolás lesz."""
    # az AI néha bemásolja a forráskivonatok sorszámát („[1]”, „[1] [1]”, „[1], [2]”) – az olvasónak ez nem mond semmit
    p = CITE_LEAK.sub("", p)
    p = re.sub(r"(\]\([^)\s]+\))([.,;:!?]?)\]", r"\1\2", p)  # link után maradt felesleges „]”
    if p.startswith("## "):  # alcím (összefoglaló cikkekben eseményenként)
        head, _, rest = p[3:].partition("\n")
        head = re.sub(r"(?i)^\s*(?:rövid\s+)?(?:al)?cím\s*[:：\-–]\s*", "", head)  # az AI néha beírja a „Rövid alcím:” címkét
        return f"<h2>{E(head.strip())}</h2>" + (_para(rest.strip()) if rest.strip() else "")
    lines = [l.strip() for l in p.split("\n") if l.strip()]
    bullets = [l for l in lines if BULLET.match(l)]
    if not bullets:
        return f"<p>{_links(E(p))}</p>"
    out, items = [], []
    for l in lines:
        if BULLET.match(l):
            items.append("<li>" + _links(E(BULLET.sub("", l))) + "</li>")
        else:
            if items:
                out.append("<ul>" + "".join(items) + "</ul>")
                items = []
            out.append("<p>" + _links(E(l)) + "</p>")
    if items:
        out.append("<ul>" + "".join(items) + "</ul>")
    return "".join(out)


def _related_html(related: list) -> str:
    if not related:
        return ""
    cards = []
    for r in related[:4]:
        img = r.get("hero_image") or {}
        sec = SECTIONS.get(r.get("category"), RETRO_SECTION)
        pic = (f'<img class="{E(img.get("kind", "photo"))}{" top" if img.get("pos") == "top" else ""}" src="{E(img["url"])}" alt="" loading="lazy">'
               if img.get("url") else "")
        cards.append(f'<a href="{E(r["url"])}">{pic}<div class="k">{E(sec["kicker"])}</div>'
                     f'<div class="t">{E(r["title"])}</div></a>')
    return f'<section class="related"><h2>Olvass tovább</h2><div class="rel-grid">{"".join(cards)}</div></section>'


def _figure(img: dict, alt: str, eager: bool = False, caption: str = "") -> str:
    """Kép kredittel (ⓘ); a szövegközi képeknél látható képaláírással."""
    cap = f'<span class="cap">{E(caption)}</span> ' if caption else ""
    cls = img.get("kind", "photo") + (" hero" if eager else "")
    return (f'<figure class="{E(cls)}"><img src="{E(img["url"])}" alt="{E(img.get("alt") or alt)}" '
            f'width="{E(str(img.get("width") or ""))}" height="{E(str(img.get("height") or ""))}" '
            f'loading="{"eager" if eager else "lazy"}" decoding="async">'
            f'<figcaption>{cap}<details class="credit"><summary title="Képforrás">ⓘ</summary>'
            f'{"Kép" if img.get("kind") == "graphic" or img.get("generated") else "Fotó"}: <a href="{E(img.get("source_url") or img["url"])}" rel="noopener" '
            f'target="_blank">{E(img.get("credit", ""))}</a>{"" if img.get("generated") else ", " + E(img.get("license", ""))}</details></figcaption></figure>')


GOOGLE_CLIENT_ID = "1014482488754-j1k6m2otl4cma3iaec2639i2nbji0p06.apps.googleusercontent.com"


def _share_html(url: str, title: str, n_src: int = 0) -> str:
    """Egyetlen „Megosztás” gomb: a rendszer saját megosztója (telefonon és a legtöbb gépi böngészőben is);
    ahol nincs ilyen (pl. Firefox), ott egy kis lenyíló: Facebook, WhatsApp, X, link másolása."""
    u, t = urllib.parse.quote(url, safe=""), urllib.parse.quote(title, safe="")
    src_btn = (f'<button type="button" class="sh-src" aria-expanded="false" aria-controls="srclist" '
               f'onclick="var l=document.getElementById(\'srclist\'),o=l.hidden;l.hidden=!o;this.setAttribute(\'aria-expanded\',o)">'
               f'Források ({n_src})</button>') if n_src else ""
    return (f'<div class="share" data-url="{E(url)}" data-title="{E(title)}">' + src_btn +
            '<button type="button" class="sh-main"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v13"/></svg>'
            'Megosztás</button><button type="button" class="sh-save" title="Mentés a fiókodba (Google-belépéssel)">'
            '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h12v18l-6-4-6 4z"/></svg>'
            '<span>Mentés</span></button><span class="sh-pop" hidden>'
            f'<a href="https://www.facebook.com/sharer/sharer.php?u={u}" target="_blank" rel="noopener">Facebook</a>'
            f'<a href="https://wa.me/?text={t}%20{u}" target="_blank" rel="noopener">WhatsApp</a>'
            f'<a href="https://x.com/intent/post?url={u}&text={t}" target="_blank" rel="noopener">X</a>'
            '<button type="button" class="sh-copy">Link másolása</button></span></div>'
            "<script>(function(){var d=document.currentScript.previousElementSibling,m=d.querySelector('.sh-main'),"
            "p=d.querySelector('.sh-pop'),c=d.querySelector('.sh-copy'),u=d.dataset.url,t=d.dataset.title;"
            "m.onclick=function(){if(navigator.share){navigator.share({title:t,url:u})"
            ".catch(function(){})}else{p.hidden=!p.hidden}};c.onclick=function(){"
            "(navigator.clipboard?navigator.clipboard.writeText(u):Promise.reject()).then(function(){c.textContent='Másolva ✓'},"
            "function(){prompt('A cikk linkje:',u)})};"
            # Mentés: Google-belépéshez kötve (/api/saved – D1). Ha nincs belépve, itt helyben nyílik a Google-belépés.
            "var s=d.querySelector('.sh-save'),U=null,K='kollektiva_user';try{U=JSON.parse(localStorage.getItem(K)||'null')}catch(e){}"
            "var H=null,pa=new URL(u,location.href).pathname,q='/api/saved?url='+encodeURIComponent(pa);"
            "function hd(){H=U&&U.session?{'Content-Type':'application/json',Authorization:'Bearer '+U.session}:null}hd();"
            "function st(on){s.dataset.on=on?'1':'';s.lastChild.textContent=on?'Mentve':'Mentés'}"
            "function toast(m){var x=document.createElement('div');x.textContent=m;x.style.cssText='position:fixed;left:50%;top:14px;"
            "transform:translateX(-50%);z-index:80;max-width:calc(100vw - 32px);background:#C9A45C;color:#0E1024;border-radius:999px;"
            "padding:9px 16px;font:600 13px Manrope,system-ui,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.4)';"
            "document.body.appendChild(x);setTimeout(function(){x.remove()},5000)}"
            "function save(on){st(on);return fetch(on?'/api/saved':q,{method:on?'POST':'DELETE',headers:H,"
            "body:on?JSON.stringify({url:pa,title:t}):undefined}).then(function(r){if(r.status==401){st(!on);U=null;hd();login()}})"
            ".catch(function(){st(!on)})}"
            "function gis(){return window.google&&google.accounts?Promise.resolve():new Promise(function(ok,no){"
            "var x=document.createElement('script');x.src='https://accounts.google.com/gsi/client';x.onload=ok;x.onerror=no;"
            "document.head.appendChild(x)})}"
            # nincs belépve: előbb egy kis ablak a magyarázattal és a Google-gombbal (nem ugrik fel rögtön a belépés)
            "var lb=null;function login(){gis().catch(function(){});if(lb){lb.hidden=false;return}"
            "lb=document.createElement('div');lb.className='sh-login';lb.innerHTML='<p>A cikkek mentéséhez jelentkezz be – így bármelyik "
            "eszközödön megtalálod őket. Nem küldünk levelet, és nem posztolunk a nevedben.</p><button type=\"button\" class=\"gbtn\">"
            "<svg width=\"18\" height=\"18\" viewBox=\"0 0 48 48\" aria-hidden=\"true\"><path fill=\"#EA4335\" d=\"M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.7 13.3l7.9 6.1C12.5 13.6 17.8 9.5 24 9.5z\"/>"
            "<path fill=\"#4285F4\" d=\"M46.1 24.6c0-1.6-.1-3.1-.4-4.6H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.7c4.3-4 6.9-9.9 6.9-17z\"/>"
            "<path fill=\"#FBBC05\" d=\"M10.6 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.1C1 16.6 0 20.2 0 24s1 7.4 2.7 10.7l7.9-6.1z\"/>"
            "<path fill=\"#34A853\" d=\"M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.7c-2.1 1.4-4.8 2.3-8.5 2.3-6.2 0-11.5-4.1-13.4-9.8l-7.9 6.1C6.6 42.6 14.6 48 24 48z\"/>"
            "</svg>Bejelentkezés Google-fiókkal</button><button type=\"button\" class=\"lx\">Mégse</button>';"
            "d.parentNode.insertBefore(lb,d.nextSibling);lb.querySelector('.lx').onclick=function(){lb.hidden=true};"
            "lb.querySelector('.gbtn').onclick=function(){lb.hidden=true;doLogin()}}"
            "function doLogin(){gis().then(function(){"
            "google.accounts.oauth2.initTokenClient({client_id:'" + GOOGLE_CLIENT_ID + "',scope:'openid email profile',"
            "callback:function(rp){if(!rp||!rp.access_token)return;fetch('/api/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},body:JSON.stringify({access_token:rp.access_token})})"
            ".then(function(r){return r.json()}).then(function(j){if(!j.ok||!j.session)throw 0;"
            "U=Object.assign({},j.user,{session:j.session});try{localStorage.setItem(K,JSON.stringify(U))}catch(e){}"
            "hd();save(true);toast('Elmentve – a főoldalon a nevedre kattintva találod')})"
            ".catch(function(){toast('A belépés nem sikerült, próbáld újra')})}}).requestAccessToken()})"
            ".catch(function(){toast('A Google-belépés most nem érhető el')})}"
            "if(H)fetch(q,{headers:H}).then(function(r){return r.json()}).then(function(j){st(j.saved)}).catch(function(){});"
            "s.onclick=function(){if(!H){login();return}save(!s.dataset.on)}"
            "})();</script>")


def _tags_html(tags: list) -> str:
    """Kulcsszavak a cikk tetején – kattintásra a keresőben az összes kapcsolódó cikk."""
    tags = [str(x).strip() for x in tags or [] if str(x).strip()][:6]
    return ('<p class="tags">' + "".join(f'<a href="/?kereses={E(urllib.parse.quote(x))}" rel="nofollow">{E(x)}</a>' for x in tags)
            + '</p>') if tags else ""


def render_article_page(a: dict, related: Optional[list] = None) -> str:
    year = a.get("category_meta", {}).get("event_year", "")
    section = SECTIONS.get(a.get("category"), RETRO_SECTION)
    inline = {}
    for im in a.get("inline_images") or []:
        if im.get("url"):
            inline.setdefault(int(im.get("after", 0)), []).append(im)
    q = a.get("quote") if isinstance(a.get("quote"), dict) else None
    qhtml = (f'<blockquote class="q">„{E(q["text"])}”<cite>– {E(q["who"])}</cite></blockquote>') if q else ""
    paras = "\n".join(_para(p) + "".join(_figure(im, a["title"], caption=im.get("caption", "")) for im in inline.get(i, []))
                      + (qhtml if q and q.get("after") == i else "")
                      for i, p in enumerate(a.get("body", [])))
    kp = [k for k in a.get("key_points") or [] if k]
    keypoints = (f'<div class="keypoints"><p>Röviden</p><ul>{"".join(f"<li>{E(k)}</li>" for k in kp)}</ul></div>'
                 if kp else "")
    quote = f"<blockquote>{E(a['pull_quote'])}</blockquote>" if a.get("pull_quote") else ""
    sources = "".join(
        f'<li><a href="{E(s["url"])}" rel="noopener" target="_blank">{E(s.get("title") or s["url"])}</a>'
        f'{" (" + E(s["publisher"]) + ")" if s.get("publisher") and s.get("publisher") != s.get("title") else ""}</li>'
        for s in a.get("sources", []) if s.get("url"))
    img = a.get("hero_image") or None
    figure = _figure(img, a["title"], eager=True) if img and img.get("url") else ""
    published = (a.get("published_at") or a.get("created_at") or a["date"])[:10]
    v = a.get("video") if isinstance(a.get("video"), dict) else None
    video = (f'<div class="vid"><iframe src="https://www.youtube-nocookie.com/embed/{E(v["id"])}" title="{E(v.get("title") or "Videó")}" '
             'loading="lazy" allow="accelerometer; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe></div>'
             if v and re.fullmatch(r"[\w-]{11}", str(v.get("id") or "")) else "")
    sa = [x for x in a.get("see_also") or [] if x.get("url")]
    see_also = (f'<div class="box seealso"><b>{"Az ügy előzményei" if a.get("thread_id") else "Korábban írtuk"}</b><ul>' + "".join(
        f'<li><a href="{E(x["url"])}">{E(x["title"])}</a>' + (f' <span>· {E(str(x.get("date", ""))[5:].replace("-", ". "))}.</span>' if x.get("date") else "")
        + '</li>' for x in sa[:3]) + '</ul></div>') if sa else ""
    body = f"""<article>
<p class="kicker">{E(section["kicker"])}{(" · " + E(str(year))) if year else ""}{(" · ▶ Videó" + (f" · {E(str(v.get('minutes')))} perc" if v.get("minutes") else "")) if v else ""}</p>
<h1>{E(a["title"])}</h1>
<p class="meta">{E(a["authorship"]["byline"])} · <time datetime="{E(published)}">{E(published.replace("-", ". "))}.</time> · {a.get("reading_time_min", 1)} perc olvasás</p>
<div class="tagrow">{_tags_html(a.get("tags"))}<button type="button" class="sh-top" aria-label="Megosztás" title="Megosztás" onclick="var d=document.querySelector('.share');if(navigator.share){{navigator.share({{title:d.dataset.title,url:d.dataset.url}}).catch(function(){{}})}}else{{d.scrollIntoView({{behavior:'smooth',block:'center'}});d.querySelector('.sh-main').click()}}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v13"/></svg></button></div>
<p class="lead">{E(a["lead"])}</p>
{figure}
{keypoints}
{video}
{quote}
{paras}
</article>
<div id="artPoll" class="box poll" hidden></div><script>/* A nap kérdése a cikkben is, ha erre a cikkre mutat (szavazatok: /api/poll, Cloudflare D1). */
(function(){{var box=document.getElementById('artPoll');if(!box)return;var KV='';try{{KV=localStorage.getItem('kvid')||'';if(!KV){{KV=crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(36).slice(2);localStorage.setItem('kvid',KV)}}}}catch(x){{}}
function e(v){{return String(v==null?'':v).replace(/[&<>"']/g,function(c){{return{{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]}})}}
fetch('/public/data/polls.json',{{cache:'no-cache'}}).then(function(r){{return r.json()}}).then(function(d){{
var p=(d.polls||[]).filter(function(x){{return x.article_url===location.pathname&&Date.parse(x.closes_at)>Date.now()}})[0];if(!p)return;
function show(r){{var t=r.total||0;box.innerHTML='<b>A nap kérdése</b><h3>'+e(p.question)+'</h3>'+p.options.map(function(o,i){{var pc=t?Math.round(100*(r.counts[i]||0)/t):0;
return '<div class="pr'+(r.voted===i?' me':'')+'"><span style="width:'+pc+'%"></span><em>'+e(o)+(r.voted===i?' ✓':'')+'</em><strong>'+pc+'%</strong></div>'}}).join('')+'<small>'+(r.closed?'lezárult':'<span class="dot"></span>nyitva')+(t>=200?' · '+t+' szavazat':'')+'</small>';box.hidden=false}}
function ask(){{box.innerHTML='<b>A nap kérdése</b><h3>'+e(p.question)+'</h3>'+p.options.map(function(o,i){{return '<button type="button" data-i="'+i+'">'+e(o)+'</button>'}}).join('')+'<small>Szavazz, és utána látod az eredményt.</small>';box.hidden=false;
box.querySelectorAll('button').forEach(function(b){{b.onclick=function(){{fetch('/api/poll',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:p.id,option:+b.dataset.i,v:KV}})}}).then(function(r){{return r.json()}}).then(function(r){{if(r.counts)show(r)}})}}}})}}
fetch('/api/poll?id='+encodeURIComponent(p.id)+'&v='+encodeURIComponent(KV)).then(function(r){{return r.json()}}).then(function(r){{if(!r.ok)return;(r.voted!==null||r.closed)?show(r):ask()}})}}).catch(function(){{}})}})();</script>
{_share_html(a["seo"]["canonical_url"], a["title"], len(a.get("sources", [])))}
<div class="srclist" id="srclist" hidden><ul>{sources or "<li>—</li>"}</ul></div>
{see_also}
{_related_html(related or [])}"""
    og = (f'<meta property="og:image" content="{E(img["url"])}">\n<meta property="og:type" content="article">\n'
          if img and img.get("url") else '<meta property="og:type" content="article">\n')
    return _page(f'{a["seo"]["meta_title"] or a["title"]} – {SITE_NAME}', a["seo"]["meta_description"] or a["lead"],
                 a["seo"]["canonical_url"], body, og + _article_jsonld(a), noindex=a["seo"].get("noindex", False))


def render_section_index(section: dict, articles: list) -> str:
    def item(a: dict) -> str:
        img = a.get("hero_image") or {}
        year = a.get("category_meta", {}).get("event_year", "") if section["id"] == "retro" else ""
        pic = (f'<img class="{E(img.get("kind", "photo"))}{" top" if img.get("pos") == "top" else ""}" src="{E(img["url"])}" alt="" loading="lazy">'
               if img.get("url") else f'<span class="ph">{E(str(year) or section["kicker"])}</span>')
        return (f'<li><a href="{E(a["url"])}">{pic}<span>'
                + (f'<span class="y">{E(str(year))}</span><br>' if year else "")
                + f'<span class="t">{E(a["title"])}</span>'
                f'<span class="m"><br>{E(a.get("date_label", ""))} · {a.get("reading_time_min", 1)} perc</span>'
                f'<span class="l">{E(a["lead"])}</span></span></a></li>')
    items = "\n".join(item(a) for a in articles)
    body = (f'<p class="kicker">Rovat</p><h1>{E(section["name"])}</h1>'
            f'<p class="lead">{E(section["tagline"])}</p><ul class="slist">{items or "<li>Hamarosan…</li>"}</ul>')
    return _page(f'{section["name"]} – {SITE_NAME}', section["tagline"], f'{SITE_URL}/{section["id"]}/', body)


def _rfc822(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%a, %d %b %Y %H:%M:%S %z")
    except (TypeError, ValueError):
        return ""


INFO_BODY = """
<style>article h2{margin-top:40px}</style>
<p class="kicker">Információ</p>
<h1>A Kollektíváról</h1>
<p class="lead">Független online magazin: közélet, világ, pénz, tech, életmód, kultúra, univerzum – és egy kis retro.</p>
<nav class="box"><a href="#rolunk">Rólunk</a> · <a href="#impresszum">Impresszum</a> · <a href="#adatkezeles">Adatkezelési tájékoztató</a> ·
<a href="#sutik">Sütik</a> · <a href="#hirdetes">Hirdetési ajánlat</a></nav>
<article>
<h2 id="rolunk">Rólunk</h2>
<p>A Kollektíva egy budapesti egyetemista fejéből pattant ki – két előadás, egy szakdolgozat és rengeteg kávé között.
Az egész egy hétköznapi bosszúsággal kezdődött: miért kapunk ma minden hír mellé egy adag véleményt is, amit senki
nem kért?</p>
<p>Ezért csináljuk azt az újságot, amit mi magunk is szívesen olvasnánk: gyors, de nem felszínes; érthető, de nem
lekezelő; és nem mondja meg, mit gondolj – csak segít, hogy legyen miből. A napi hírek mellett tudományról, pénzről,
technológiáról és életmódról is írunk, olyan cikkeket, amik egy hét múlva is megérik az olvasást.</p>
<p>Nincs mögöttünk médiacég, befektető vagy párt – csak egy kis csapat, sok lelkesedés és hiteles források.</p>
<p>Ha tetszik, amit csinálunk, oszd meg egy cikkünket, vagy iratkozz fel a heti hírlevélre. Hamarosan egy kávéval
is támogathatsz minket – elvégre innen indult minden. Köszönjük, hogy itt vagy!</p>

<h2 id="impresszum">Impresszum</h2>
<p>Kiadó és szerkesztő: Kollektíva szerkesztőség.<br>Webcím: kollektíva.hu<br>
Kapcsolat: <a href="mailto:szerkesztoseg@xn--kollektva-m5a.hu">szerkesztoseg@kollektíva.hu</a><br>
Tárhelyszolgáltató: Cloudflare, Inc., 101 Townsend St, San Francisco, CA 94107, USA – cloudflare.com</p>
<p>Cikkeink nyilvános forrásokra (hazai és nemzetközi sajtó, hivatalos közlemények) épülnek; a felhasznált
forrásokat minden cikk alján feltüntetjük. A képek szabad licencű forrásokból (Wikimedia Commons, Openverse, Pexels, Pixabay)
származnak, vagy saját illusztrációk; a szerző és a licenc a kép melletti ⓘ jelre kattintva látható. A horoszkóp szórakoztató célú tartalom.</p>

<h2 id="adatkezeles">Adatkezelési tájékoztató</h2>
<p><b>Milyen adatot kezelünk?</b> Csak azt, amit te adsz meg: a hírlevélre való feliratkozáskor az e-mail
címedet és a feliratkozás időpontját.</p>
<p><b>Mire használjuk?</b> Kizárólag a Heti Kollektíva hírlevél kiküldésére (hetente egyszer). Nem adjuk el,
nem adjuk át harmadik félnek hirdetési célra.</p>
<p><b>Hol tároljuk?</b> A hírlevél-listát a Brevo (Sendinblue SAS, Franciaország, EU) levelezőrendszere kezeli.
Az oldalt a Cloudflare szolgálja ki; ha a látogatottságot mérjük, azt sütik és személyes azonosítás nélkül tesszük.</p>
<p><b>Leiratkozás, törlés:</b> minden levél alján van leiratkozó link – egy kattintással megszűnik a feliratkozás.
Adataid törlését, helyesbítését vagy másolatát bármikor kérheted a hírlevélre válaszolva.</p>

<p><b>Google-belépés:</b> ha Google-fiókkal lépsz be, a nevedet, e-mail címedet és profilképedet kapjuk meg
(jelszót soha). Ezeket a Brevóban, a „Kollektíva fiókok” listán tároljuk, hírlevelet csak akkor küldünk, ha külön
feliratkozol. A belépés a böngésződben marad meg; kilépni a nevedre kattintva tudsz.</p>

<h2 id="sutik">Sütik (cookie-k)</h2>
<p>Az oldal nem használ követő vagy hirdetési sütiket, ezért nincs mit elfogadnod. A böngésződ csak egy apró,
helyi beállítást jegyez meg (a horoszkópnál kiválasztott csillagjegyet, és ha belépsz, a nevedet), ez nem hagyja el a gépedet, és a böngésző
beállításaiban bármikor törölheted. Ha a jövőben hirdetések vagy mérőeszközök kerülnek az oldalra, itt jelezzük,
és előtte kérjük a hozzájárulásodat.</p>

<h2 id="hirdetes">Hirdetési ajánlat</h2>
<p>A Kollektíva jelenleg hirdetésmentes. Hirdetési és együttműködési lehetőségekről (szponzorált tartalom,
hírlevél-megjelenés) hamarosan itt tájékoztatunk.</p>
</article>
"""


def build_info_page(public: Path) -> None:
    d = public / "info"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(_page(f"Információ – {SITE_NAME}", "Impresszum, adatkezelés, sütik, hirdetés.",
                                        f"{SITE_URL}/info/", INFO_BODY), encoding="utf-8")


POLLS_BODY = """<article>
<p class="kicker">Olvasói szavazások</p>
<h1>A nap kérdései</h1>
<p>Naponta egy kérdés, 7 napig nyitva. Nem reprezentatív.</p>
<div id="plist"><p>Betöltés…</p></div>
</article>
<script>(function(){var KV='';try{KV=localStorage.getItem('kvid')||'';if(!KV){KV=crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(36).slice(2);localStorage.setItem('kvid',KV)}}catch(x){}
var L=document.getElementById('plist'),E=function(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})};
fetch('/public/data/polls.json',{cache:'no-cache'}).then(function(r){return r.json()}).then(function(d){var ps=(d.polls||[]);if(!ps.length){L.innerHTML='<p>Még nincs szavazás.</p>';return}
L.innerHTML=ps.map(function(p){return '<div class="box poll" id="p'+E(p.id)+'"><small>Betöltés…</small></div>'}).join('');
ps.forEach(function(p){var box=document.getElementById('p'+p.id),head='<b>'+E(p.date)+'</b><h3><a href="'+E(p.article_url)+'" style="color:inherit;text-decoration:none">'+E(p.question)+'</a></h3>',foot='<p class="pa"><a href="'+E(p.article_url)+'">'+E(p.article_title)+'</a></p>';
function show(r){var t=r.total||0;box.innerHTML=head+p.options.map(function(o,i){var pc=t?Math.round(100*(r.counts[i]||0)/t):0;return '<div class="pr'+(r.voted===i?' me':'')+'"><span style="width:'+pc+'%"></span><em>'+E(o)+(r.voted===i?' ✓':'')+'</em><strong>'+pc+'%</strong></div>'}).join('')
+'<small>'+(r.closed?'<span class="dot" style="background:#E5322B;box-shadow:0 0 0 3px rgba(229,50,43,.2)"></span>lezárult':'<span class="dot"></span>nyitva')+(t>=200?' · '+t+' szavazat':'')+'</small>'+foot}
function ask(){box.innerHTML=head+p.options.map(function(o,i){return '<button type="button" data-i="'+i+'">'+E(o)+'</button>'}).join('')+'<small><span class="dot"></span>nyitva · szavazz, és utána látod az eredményt.</small>'+foot;
box.querySelectorAll('button').forEach(function(b){b.onclick=function(){fetch('/api/poll',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:p.id,option:+b.dataset.i,v:KV})}).then(function(r){return r.json()}).then(function(r){if(r.counts)show(r)})}})}
fetch('/api/poll?id='+encodeURIComponent(p.id)+'&v='+encodeURIComponent(KV)).then(function(r){return r.json()}).then(function(r){if(!r.ok){box.innerHTML=head+foot;return}if(!r.closed&&r.voted===null)ask();else show(r)}).catch(function(){box.innerHTML=head+foot})})}).catch(function(){L.innerHTML='<p>A szavazások most nem tölthetők be.</p>'})})();</script>"""


def build_polls_page(public: Path) -> None:
    d = public / "szavazasok"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(_page(f"A nap kérdései – {SITE_NAME}", "Olvasói szavazások és eredményeik.",
                                        f"{SITE_URL}/szavazasok/", POLLS_BODY), encoding="utf-8")


QUIZ_BODY = """<article>
<p class="kicker">Heti kvíz</p>
<h1 id="qzT">Mennyire követted a hetet?</h1>
<p id="qzS">Nyolc kérdés a hét híreiből. Válassz, és rögtön kiderül, eltaláltad-e – minden kérdés alatt ott a cikk is.</p>
<div id="qz"><p>Betöltés…</p></div>
<div id="qzEnd" class="box qzend" hidden></div>
<div id="qzOld"></div>
</article>
<style>.qzq{margin:22px 0;padding:18px 20px;background:var(--vault);border:1px solid var(--line);border-radius:12px}.qzq b{color:var(--brass);font-size:12px;letter-spacing:.12em;text-transform:uppercase}.qzq h3{margin:6px 0 12px;font:600 21px/1.3 "Cormorant Garamond",Georgia,serif;color:var(--parch)}.qzq button{display:block;width:100%;text-align:left;margin:6px 0;padding:11px 14px;border:1px solid var(--line);border-radius:12px;background:transparent;color:var(--parch);font:15px Manrope,system-ui,sans-serif;cursor:pointer}.qzq button:hover:not([disabled]){border-color:var(--brass)}.qzq button[disabled]{cursor:default}.qzq button.ok{border-color:#3FBF6F;background:rgba(63,191,111,.12)}.qzq button.bad{border-color:#B3261E;background:rgba(179,38,30,.14)}.qzq .ex{margin:10px 0 0;font-size:14px;color:var(--dusk)}.qzq .ex a{color:var(--brass)}.qzend{text-align:center}.qzend strong{display:block;font:600 44px/1 "Cormorant Garamond",Georgia,serif;color:var(--brass);margin:6px 0}.qzend p{color:var(--parch);margin:6px 0 12px}.qzend button{font:600 14px/1 Manrope,system-ui,sans-serif;color:#0E1024;background:var(--brass);border:0;border-radius:999px;padding:11px 18px;cursor:pointer}#qzOld a{display:inline-block;margin:4px 8px 4px 0;font-size:13px;color:var(--dusk);border:1px solid var(--line);border-radius:999px;padding:5px 12px;text-decoration:none}#qzOld a:hover{color:var(--brass);border-color:var(--brass)}</style>
<script>(function(){var Q=document.getElementById('qz'),END=document.getElementById('qzEnd'),E=function(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})};
function get(u){return fetch(u,{cache:'no-cache'}).then(function(r){if(!r.ok)throw 0;return r.json()})}
get('/public/data/quizzes.json').catch(function(){return get('/data/quizzes.json')}).then(function(d){var all=(d&&d.quizzes)||[];if(!all.length){Q.innerHTML='<p>Az első kvíz szombaton érkezik.</p>';return}
var want=new URLSearchParams(location.search).get('id'),z=all.filter(function(x){return x.id===want})[0]||all[0],K='kqz_'+z.id,ans={};
try{ans=JSON.parse(localStorage.getItem(K)||'{}')||{}}catch(e){ans={}}
document.getElementById('qzT').textContent=z.title||'Mennyire követted a hetet?';
document.getElementById('qzS').textContent=z.questions.length+' kérdés a hét híreiből ('+z.week.replace('-W','. év ')+'. hét). Válassz, és rögtön kiderül, eltaláltad-e.';
Q.innerHTML=z.questions.map(function(q,i){return '<div class="qzq" data-i="'+i+'"><b>'+(i+1)+' / '+z.questions.length+'</b><h3>'+E(q.question)+'</h3>'+q.options.map(function(o,j){return '<button type="button" data-j="'+j+'">'+E(o)+'</button>'}).join('')+'<p class="ex" hidden></p></div>'}).join('');
function mark(i,j){var q=z.questions[i],box=Q.querySelector('.qzq[data-i="'+i+'"]');box.querySelectorAll('button').forEach(function(b,k){b.disabled=true;if(k===q.correct)b.classList.add('ok');else if(k===j)b.classList.add('bad')});
var ex=box.querySelector('.ex');ex.hidden=false;ex.innerHTML=(j===q.correct?'✓ Eltaláltad. ':'✗ Nem ez volt. ')+E(q.explain)+' <a href="'+E(q.article_url)+'">A cikk →</a>'}
function done(){var n=z.questions.length,got=0,k=0;for(var i in ans){k++;if(ans[i]===z.questions[i].correct)got++}if(k<n)return;
var v=got===n?'Hibátlan – te mindent tudsz a hétről.':got>=n*.75?'Nagyon jó, alig maradt le valami.':got>=n/2?'Nem rossz, de pár hír elkerülte a figyelmed.':'Ez a hét kicsit elszaladt melletted – a cikkek segítenek.';
END.hidden=false;END.innerHTML='<b style="color:var(--brass);font-size:12px;letter-spacing:.12em;text-transform:uppercase">Eredményed</b><strong>'+got+' / '+n+'</strong><p>'+v+'</p><button type="button" id="qzSh">Megosztom</button>';
document.getElementById('qzSh').onclick=function(){var t=got+'/'+n+' pontot értem el a Kollektíva heti hírkvízén. Neked mennyi lesz?',u=location.origin+'/kviz/?id='+z.id;
if(navigator.share)navigator.share({title:'Heti kvíz – Kollektíva',text:t,url:u}).catch(function(){});else if(navigator.clipboard)navigator.clipboard.writeText(t+' '+u).then(function(){document.getElementById('qzSh').textContent='Link kimásolva ✓'})}}
for(var i in ans)if(z.questions[i])mark(+i,ans[i]);done();
Q.addEventListener('click',function(e){var b=e.target.closest('button[data-j]');if(!b||b.disabled)return;var i=+b.closest('.qzq').dataset.i,j=+b.dataset.j;ans[i]=j;try{localStorage.setItem(K,JSON.stringify(ans))}catch(e){}mark(i,j);done()});
var old=all.filter(function(x){return x.id!==z.id}).slice(0,12);if(old.length)document.getElementById('qzOld').innerHTML='<p class="kicker" style="margin-top:32px">Korábbi kvízek</p>'+old.map(function(x){return '<a href="/kviz/?id='+E(x.id)+'">'+E(x.week.replace('-W','/'))+'. hét</a>'}).join('')
}).catch(function(){Q.innerHTML='<p>A kvíz most nem tölthető be.</p>'})})();</script>"""


def build_quiz_page(public: Path) -> None:
    d = public / "kviz"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(_page(f"Heti kvíz – {SITE_NAME}", "Heti hírkvíz: mennyire követted a hét híreit?",
                                        f"{SITE_URL}/kviz/", QUIZ_BODY), encoding="utf-8")


def build_static_site(output_dir: Path, tz: ZoneInfo) -> None:
    """A publikált cikkekből (retro + rovatok) statikus oldalakat, sitemapeket és RSS-t generál a public/ alá."""
    public = output_dir.parent  # public/data -> public
    build_info_page(public)
    build_polls_page(public)
    build_quiz_page(public)
    pool = (read_json(output_dir / "retro_articles.json", {"articles": []}).get("articles", [])
            + read_json(output_dir / "articles.json", {"articles": []}).get("articles", []))
    articles = [a for a in pool if a.get("status") == "published" and a.get("slug") and a.get("seo")]
    by_section: dict = {sid: [] for sid in [RETRO_SECTION["id"], *SECTIONS]}
    for a in articles:
        sid = a.get("category") if a.get("category") in SECTIONS else "retro"
        a["url"] = f"/{sid}/{a['slug']}/"
        a["seo"]["canonical_url"] = f"{SITE_URL}/{sid}/{a['slug']}/"
        by_section[sid].append(a)
    articles.sort(key=lambda a: a.get("published_at") or a.get("created_at") or "", reverse=True)
    for sid, items in by_section.items():
        sec_dir = public / sid
        keep = {a["slug"] for a in items}
        if sec_dir.exists():  # már nem publikált cikkek oldalainak törlése
            for child in sec_dir.iterdir():
                if child.is_dir() and child.name not in keep:
                    for f in child.iterdir():
                        f.unlink()
                    child.rmdir()
        for a in items:
            page_dir = sec_dir / a["slug"]
            page_dir.mkdir(parents=True, exist_ok=True)
            related = [r for r in items if r is not a][:2] + [r for r in articles if r.get("category") != a.get("category")][:4]
            (page_dir / "index.html").write_text(render_article_page(a, related[:4]), encoding="utf-8")
        sec_dir.mkdir(parents=True, exist_ok=True)
        section = SECTIONS.get(sid, RETRO_SECTION)
        items.sort(key=lambda a: a.get("published_at") or a.get("created_at") or "", reverse=True)
        (sec_dir / "index.html").write_text(render_section_index(section, items), encoding="utf-8")

    now = datetime.now(tz)
    urls = [(f"{SITE_URL}/", now.date().isoformat())]
    urls += [(f"{SITE_URL}/{sid}/", now.date().isoformat()) for sid in by_section]
    urls += [(f"{SITE_URL}/kviz/", now.date().isoformat()), (f"{SITE_URL}/szavazasok/", now.date().isoformat())]
    urls += [(a["seo"]["canonical_url"], (a.get("updated_at") or a["date"])[:10]) for a in articles]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sitemap += [f"  <url><loc>{E(u)}</loc><lastmod>{m}</lastmod></url>" for u, m in urls]
    sitemap.append("</urlset>")
    (public / "sitemap.xml").write_text("\n".join(sitemap) + "\n", encoding="utf-8")

    # Google News sitemap: csak az elmúlt 2 nap cikkei
    news = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">']
    for a in articles:
        pub = a.get("published_at") or a.get("created_at")
        try:
            fresh = pub and now - datetime.fromisoformat(pub) <= timedelta(days=2)
        except ValueError:
            fresh = False
        if fresh:
            news.append(f"  <url><loc>{E(a['seo']['canonical_url'])}</loc><news:news>"
                        f"<news:publication><news:name>{SITE_NAME}</news:name><news:language>hu</news:language>"
                        f"</news:publication><news:publication_date>{E(pub)}</news:publication_date>"
                        f"<news:title>{E(a['title'])}</news:title></news:news></url>")
    news.append("</urlset>")
    (public / "news-sitemap.xml").write_text("\n".join(news) + "\n", encoding="utf-8")

    items = "\n".join(
        f"<item><title>{E(a['title'])}</title><link>{E(a['seo']['canonical_url'])}</link>"
        f"<guid isPermaLink=\"true\">{E(a['seo']['canonical_url'])}</guid>"
        f"<pubDate>{_rfc822(a.get('published_at') or a.get('created_at'))}</pubDate>"
        f"<description>{E(a['lead'])}</description></item>" for a in articles[:40])
    rss = (f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
           f"<title>{SITE_NAME}</title><link>{SITE_URL}/</link>"
           f"<description>Kollektíva – online magazin.</description><language>hu</language>\n{items}\n"
           f"</channel></rss>\n")
    (public / "feed.xml").write_text(rss, encoding="utf-8")
    log.info("✔ statikus oldalak: %d cikkoldal, sitemap.xml, news-sitemap.xml, feed.xml", len(articles))


# ---------------------------------------------------------------------------
# 4) Rovatok (Univerzum, Pénzvilág, Tech, Életmód, Kultúra, Közélet)
# ---------------------------------------------------------------------------
# Rovatonként RSS-forrásokból kiválasztja a nap legtöbb forrásban szereplő témáját,
# és a forrásokra hivatkozva SAJÁT megfogalmazású összefoglaló cikket írat.
# Feed: (url, kategória-szűrő regex vagy None, kulcsszó-szűrő regex vagy None)

ECON = (r"forint|árfolyam|infláció|kamat|MNB|jegybank|\bbér|fizetés|nyugdíj|\badó|\bár(ak|a)?\b|drág|olcsó|tőzsde|"
        r"részvény|befektet|hitel|lakás|ingatlan|energiaár|benzin|üzemanyag|gazdaság|GDP|költségvetés|bank|"
        r"megtakarít|euró|dollár|bitcoin|kripto|vállalat|cég|munkanélküli|fogyasztó")
HEALTH = (r"egészs|edzés|mozgás|alvás|táplálkoz|étrend|diéta|vitamin|szív|stressz|mentális|pszich|fogyás|"
          r"elhízás|cukor|kutatás|orvos|betegség|immun|futás|jóga|izom|életmód")
PUBLIC = (r"adat|statisztik|KSH|felmérés|kutatás|oktatás|iskola|egészségügy|kórház|közlekedés|MÁV|BKV|lakhatás|"
          r"nyugdíj|család|népesség|szavazó|választás|törvény|önkormányzat|időjárás|klíma|környezet")
# Minden rovatból kizárt témák: csak a szélsőséges tartalom (bűnügy, háború, vádak mehetnek, tényszerűen)
EXCLUDE_ALL = re.compile(r"öngyilk|pedofil|gyermekpornó|kiskorú.{0,20}(szexuális|bántalmaz)", re.I)
# Nem önálló hír, hanem „hír a hírről” / gyűjtőcikk / élő közvetítés – ezekből nem írunk
# „új felvételek / képek” típusú űrhír: csak akkor ér valamit, ha magukat a képeket mutatjuk – ilyet nem írunk
SPACE_IMG_STORY = re.compile(r"\b(new|stunning|latest) (images?|photos?|views?|mosaic)|\b(image|photo) of the (day|week)|"
                             r"új (képek|képe|felvétel|fotó)|látványos (kép|felvétel|fotó)|lenyűgöző (kép|felvétel|fotó)|mozaik", re.I)
SPACE_STORY = re.compile(r"NASA|ESA\b|\bűr\w*|\bMars\b|\bHold\b|holdbázis|Szaturnusz|Jupiter|csillag|galaxis|bolygó|SpaceX|"
                         r"Artemis|asztronaut|űrhajó|teleszkóp|űrtávcső|rakéta", re.I)
META_STORY = re.compile(r"Google Trends|keresések|keresőben|percről percre|hírösszefoglaló|napi összefoglaló|"
                        r"\bélő\b|élőben|podcast|videó:|galéria|horoszkóp|kvíz|nyereményjáték|ajánlónk", re.I)

RETRO_SECTION = {"id": "retro", "name": "Ekkor történt", "kicker": "Ekkor történt",
                 "tagline": "Minden nap egy történet a múltból."}

SECTIONS = {
    "kozelet": {
        "voice": "tárgyilagos, pontos, higgadt; a tényeket és az érintettek álláspontját egymás mellé teszi, értékelés nélkül",
        "id": "kozelet", "name": "Közélet", "kicker": "Közélet",
        "tagline": "Belpolitika és közügyek – pártatlanul, érthetően.",
        "focus": "belpolitika, közügyek, társadalom, bűnügyek és közérdekű adatok – pártsemlegesen",
        "feeds": [("https://telex.hu/rss", r"Adat", None), ("https://telex.hu/rss", r"Belföld", None),
                  ("https://hvg.hu/rss", r"Itthon", None)],
    },
    "vilag": {
        "voice": "magyarázó külpolitikai elemző: földrajzi, történelmi kontextust ad, és elmondja, mit jelent ez Magyarországnak",
        "id": "vilag", "name": "Világ", "kicker": "Világ",
        "tagline": "Ami a világban történik – háttérrel, magyarul.",
        "focus": "külpolitika, nemzetközi események, háborúk és konfliktusok, világgazdaság – tényszerűen",
        "feeds": [("https://telex.hu/rss", r"Külföld|Világ", None), ("https://hvg.hu/rss", r"Világ|Külföld", None)],
    },
    "penzvilag": {
        "voice": "józan, gyakorlatias gazdasági újságíró: számokkal dolgozik, és mindig lefordítja, mit jelent a pénztárcának",
        "id": "penzvilag", "name": "Pénzvilág", "kicker": "Pénzvilág",
        "tagline": "Árfolyamok, infláció, bérek – mit jelentenek a számok a pénztárcádnak.",
        "focus": "gazdaság, pénzügyek, árfolyamok, infláció, bérek, befektetés – a hétköznapi olvasó szemszögéből",
        "feeds": [("https://www.portfolio.hu/rss/all.xml", None, ECON), ("https://www.vg.hu/feed", None, ECON),
                  ("https://telex.hu/rss", r"Gazdaság|Vállalat", ECON)],
    },
    "tech": {
        "voice": "közérthető, kíváncsi tech-újságíró: a szakszavakat egy félmondatban elmagyarázza, túlzó hype nélkül",
        "id": "tech", "name": "Tech & Tudomány", "kicker": "Tech & Tudomány",
        "tagline": "Mesterséges intelligencia, tudomány és űrkutatás – érthetően.",
        "focus": "technológia, mesterséges intelligencia, digitális eszközök, tudomány, csillagászat és űrkutatás",
        "feeds": [("https://telex.hu/rss", r"Techtud", None), ("https://qubit.hu/feed", None, None),
                  ("https://hvg.hu/rss", r"Tech|Tudomány", None),
                  # a NASA saját hírfolyamából csak a nagy események (a sok „új felvétel” / mozaik-poszt nem hír nálunk)
                  ("https://www.nasa.gov/feed/", None, r"Artemis|launch|landing|astronaut|crew|discover|first|record|asteroid|Mars Sample")],
    },
    "eletmod": {
        "voice": "barátságos, tudományosan megalapozott: a kutatási eredményt a helyén kezeli (egy vizsgálat nem bizonyíték), praktikus",
        "id": "eletmod", "name": "Életmód & Egészség", "kicker": "Életmód",
        "tagline": "Mozgás, alvás, táplálkozás, mentális jóllét – forrásokkal alátámasztva.",
        "focus": "egészség, mozgás, edzés, alvás, táplálkozás, mentális jóllét – kutatási eredmények érthetően",
        "feeds": [("https://www.sciencedaily.com/rss/health_medicine/fitness.xml", None, None),
                  ("https://www.sciencedaily.com/rss/health_medicine/nutrition.xml", None, None),
                  ("https://telex.hu/rss", r"^Élet$", HEALTH), ("https://hvg.hu/rss", r"Élet|egészség", HEALTH)],
    },
    "kultura": {
        "voice": "élvezetes kulturális kritikus: érzékletes, személyes hangú, de nem nagyképű",
        "id": "kultura", "name": "Kultúra & Ajánló", "kicker": "Kultúra",
        "tagline": "Film, sorozat, könyv, zene és programok válogatva.",
        "focus": "film, sorozat, könyv, zene, színház, kiállítás, programajánló",
        "feeds": [("https://telex.hu/rss", r"Kultúra", None), ("https://hvg.hu/rss", r"Kult", None)],
    },
    "bulvar": {
        "voice": "könnyed, szórakoztató, kacsintós, de soha nem bántó vagy lekezelő",
        "id": "bulvar", "name": "Bulvár", "kicker": "Bulvár",
        "tagline": "Sztárok, show és az internet legfurcsább történetei.",
        "focus": ("sztárok, hírességek, showbiznisz, tévéműsorok, virális és furcsa történetek – könnyed hangon, de "
                  "ízlésesen: pletykát, feltételezést soha nem állítunk tényként, magánéleti részleteket nem nagyítunk fel"),
        "feeds": [("https://www.blikk.hu/rss", r"Sztárvilág", None),
                  ("https://www.borsonline.hu/publicapi/hu/rss/bors/articles", None, None)],
    },
    "univerzum": {
        "voice": "lelkes ismeretterjesztő: léptékeket érzékeltet hétköznapi hasonlatokkal (pl. „ez olyan, mintha…”)",
        "legacy": True,  # beolvadt a Tech & Tudományba: új cikk nem készül ide, a régiek oldalai megmaradnak
        "id": "univerzum", "name": "Univerzum", "kicker": "Univerzum",
        "tagline": "Csillagászat és űrkutatás, érthetően.",
        "focus": "csillagászat, űrkutatás, bolygók, űrmissziók",
        "feeds": [("https://www.nasa.gov/feed/", None, None),
                  ("https://qubit.hu/feed", None, r"űr|NASA|ESA|bolygó|csillag|galaxis|Hold|Mars|teleszkóp|rakéta|asztro"),
                  ("https://telex.hu/rss", r"Techtud", r"űr|NASA|ESA|bolygó|csillag|galaxis|Hold|Mars|teleszkóp|rakéta")],
    },
}
ACTIVE_SECTIONS = {k: v for k, v in SECTIONS.items() if not v.get("legacy")}
NAV_LINKS = " ".join(f'<a href="/{sid}/">{html.escape(sec["name"])}</a>'
                     for sid, sec in [*ACTIVE_SECTIONS.items(), ("retro", RETRO_SECTION)])
MENU_HTML = ('<b class="mh">Rovatok</b><div class="mg">' + NAV_LINKS + '</div><hr>'
             '<a href="/?kereses">Keresés</a><a href="/#hirlevel">Heti hírlevél</a><a href="/#horoszkop">Horoszkóp</a>'
             '<a href="/kviz/">Heti kvíz</a><a href="/szavazasok/">Szavazások</a><a href="/?belepes=1">Fiókom, mentett cikkek</a>'
             '<a href="/info/#rolunk">Rólunk</a><a href="/info/">Információ, impresszum</a>')
SPONSORED = re.compile(r"PR-cikk|Támogatott|Szponzor|Hirdetés|Közlemény|partner", re.I)
STOPWORDS = set("""a az és is egy hogy nem de már még meg el ki be le fel van volt lesz lett mint
csak ez azt ezt itt ott mit mi ami aki akik kell után alatt miatt szerint között új több nagy the of and
to in for on with from""".split())

SECTION_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) szerkesztője vagy. Friss hírekből írsz SAJÁT "
    "megfogalmazású, magyarázó magazincikket: nem másolsz, nem fordítasz szó szerint, hanem összefoglalsz, "
    "kontextust adsz és elmagyarázod, mit jelent ez az olvasónak. SZIGORÚ SZABÁLY: csak a megadott "
    "forráskivonatokban szereplő tényekre és vitathatatlan, közismert háttérre támaszkodhatsz; nem találsz ki "
    "számot, idézetet, nevet vagy dátumot. TISZTSÉGEK: egy személy tisztségét (pl. kancellár, miniszter, elnök) mindig "
    "a forrás szerint írd, ne a saját (esetleg elavult) tudásod szerint – ha a forrás „kancellárt” ír, nem lehet "
    "„kancellárjelölt”. Pártpolitikai állást nem foglalsz. "
    "STÍLUS: természetes, gördülékeny, újságírói magyar nyelv; változatos mondathossz; nincs töltelékszöveg, "
    "nincs ismétlődő szó vagy fordulat egymás közelében, nincsenek tükörfordítások és erőltetett szókapcsolatok "
    "(pl. „ez rávilágít arra”, „nem csupán… hanem”, „fontos megjegyezni”, „összességében”). Az első mondat "
    "a lényeget mondja, a bekezdések sorrendje: mi történt → miért fontos → háttér → mi várható. "
    "EREDETISÉG: ne kövesd a forráscikk szerkezetét, szögét és címét – saját felépítést és saját címet írj, "
    "a címet ne másold le (egy-egy ütős közös szó belefér). Minden bekezdésben legyen konkrét tény "
    "(név, szám, dátum, helyszín, döntés, következmény); általános, bármire ráhúzható töltelékmondat tilos. "
    "Ha kevés a tény, inkább legyen rövidebb a cikk. "
    "BŰNÜGY, VÁDAK, HÁBORÚ: ártatlanság vélelme – gyanút és vádat soha ne írj tényként („a rendőrség szerint”, "
    "„a vád szerint”, „a gyanú szerint”); magánszemélyt ne nevezz meg teljes névvel, csak közszereplőt; "
    "nincs naturalisztikus, véres részlet, nincs szenzációhajhászás. "
    "Csak érvényes JSON-t adsz vissza."
)

EDIT_SYSTEM = (
    "Tapasztalt magyar olvasószerkesztő vagy. Kapsz egy cikket JSON-ban. Javítsd a nyelvezetét: helyesírás, "
    "gördülékenység, szóismétlések, kellemetlen szókapcsolatok, gépies (AI-szerű) fordulatok. A TÉNYEKEN, "
    "számokon, neveken, dátumokon és a forrásmegnevezésen NE változtass, új információt ne adj hozzá, ne rövidíts "
    "érdemben. Ugyanazt a JSON-szerkezetet add vissza, csak a szöveges mezőket javítva."
)


# Nem magyar ékezetes betűt tartalmazó szavak (pl. „rávädítnek”) – tipikus AI-elírás; idegen tulajdonneveknél lehet jó.
ODD_WORD = re.compile(r"\b\w*[äëïÿàèìòùâêîôûãõñçøåßæœ]\w*\b", re.I)


def _odd_words(raw: dict) -> list:
    parts = [raw.get("title") or "", raw.get("lead") or ""] + list(raw.get("key_points") or []) + list(raw.get("body") or [])
    return sorted({w for p in parts for w in ODD_WORD.findall(str(p)) if not w[:1].isupper()})


def editorial_polish(ai: "AIClient", raw: dict) -> dict:
    """Második kör: olvasószerkesztői javítás (+ egy célzott kör, ha furcsa, nem magyar betűs szó maradt).
    Hiba esetén az eredeti marad."""
    keep = {k: raw.get(k) for k in ("title", "lead", "key_points", "body", "tags")}
    for attempt in range(2):
        odd = _odd_words(raw)
        if attempt and not odd:
            break
        hint = ("\n\nFIGYELEM, ezek a szavak valószínűleg elgépelések (nem magyar betű van bennük), javítsd őket a "
                "helyes magyar szóra: " + ", ".join(odd[:15])) if odd else ""
        try:
            fixed = ai.complete_json(EDIT_SYSTEM + hint, json.dumps(keep, ensure_ascii=False), 4000)
            if isinstance(fixed.get("body"), list) and len(fixed["body"]) >= max(3, len(keep["body"] or []) - 1):
                raw = {**raw, **{k: fixed[k] for k in keep if fixed.get(k)}}
                keep = {k: raw.get(k) for k in keep}
        except (AIError, ValueError, TypeError, KeyError) as e:
            log.warning("Olvasószerkesztés kimaradt: %s", e)
            break
    if _odd_words(raw):
        log.warning("Gyanús szavak maradtak a cikkben: %s", ", ".join(_odd_words(raw)[:10]))
    return raw


REVIEW_SYSTEM = (
    "Szigorú magyar lektor és tényellenőr vagy egy hírportálnál. Egy kész cikket kapsz a forrásaival. Mondatonként "
    "keresd: (1) értelmetlen, logikailag zavaros vagy félreérthető mondat; (2) a forrásoknak ellentmondó vagy azokban "
    "nem szereplő állítás (szám, név, dátum, helyszín, ki mit tett); (3) egymásnak ellentmondó részek; (4) magyartalan, "
    "tükörfordított mondat; (5) félrevezető cím: hamisat állít, vagy régi/kitalált (sci-fi) történetnek hathat. A "
    "kíváncsiságkeltő, a poént le nem lövő clickbait cím JÓ, azt ne cseréld. Csak valódi hibát jelölj, ami jó, azt "
    "hagyd. Csak JSON-t adsz vissza.")


def critical_review(ai: "AIClient", raw: dict, story: list) -> dict:
    """Harmadik kör: kritikus lektor + tényellenőrzés a forrásokkal szemben; a hibás mondatokat kicseréli
    (vagy törli), a homályos címet konkrétra cseréli. Hiba esetén az eredeti marad."""
    src = "\n\n".join(f"[{i + 1}] {s.get('title', '')}\n"
                       f"{(s.get('fulltext') or s.get('summary') or s.get('text') or s.get('extract') or '')[:1800]}"
                       for i, s in enumerate((story or [])[:4]))
    art = {k: raw.get(k) for k in ("title", "lead", "key_points", "body")}
    prompt = (f"FORRÁSOK:\n{src or '(nincs megadva)'}\n\nCIKK (JSON):\n{json.dumps(art, ensure_ascii=False)}\n\n"
              'Válasz JSON: {"fixes": [{"old": "a hibás részlet PONTOSAN úgy, ahogy a cikkben áll (egy mondat vagy '
              'mondatrész)", "new": "a javított változat (ha törlendő: üres)", "why": "röviden a hiba"}], '
              '"title_ok": true vagy false, "better_title": "ha a cím félrevezető: ütős, igaz, max. 9 szavas új '
              'cím, különben üres"}')
    try:
        r = ai.complete_json(REVIEW_SYSTEM, prompt, 3000)
    except (AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Lektorálás kimaradt: %s", e)
        return raw
    fixes = [f for f in (r.get("fixes") or []) if isinstance(f, dict) and len(str(f.get("old", "")).strip()) >= 8][:12]
    done = []

    def fix(t: str) -> str:
        for f in fixes:
            old, new = str(f["old"]).strip(), str(f.get("new") or "").strip()
            if old in t:
                t = t.replace(old, new, 1)
                done.append(f.get("why") or "javítás")
        return re.sub(r"[ \t]{2,}", " ", t).strip()

    out = dict(raw)
    out["lead"] = fix(str(raw.get("lead") or ""))
    out["key_points"] = [x for x in (fix(str(k)) for k in raw.get("key_points") or []) if x]
    out["body"] = [x for x in (fix(str(p)) for p in raw.get("body") or []) if x]
    if len(out["body"]) < max(3, len(raw.get("body") or []) - 2) or not out["lead"]:
        out = dict(raw)  # túl sokat vágott volna ki – maradjon az eredeti
        done = []
    bt = str(r.get("better_title") or "").strip().strip('"„”')
    if r.get("title_ok") is False and 10 < len(bt) < 110:
        out["title"] = bt
        done.append("homályos cím → " + bt)
    if done:
        log.info("Lektor: %d javítás (%s)", len(done), "; ".join(map(str, done))[:300])
    return out


def wiki_context(story: list, timeout: int, limit: int = 3) -> list:
    """Háttér a hírben szereplő nevekhez/intézményekhez a magyar Wikipédiából (ki kicsoda, mi micsoda)."""
    text = " ".join(s["title"] + ". " + s["summary"][:300] for s in story)
    names = re.findall(r"(?<![\wÁÉÍÓÖŐÚÜŰ])([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+(?:[ -][A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+)+)", text)
    seen, out = set(), []
    for n in names:
        if n in seen or len(out) >= limit:
            continue
        seen.add(n)
        data = http_get_json(f"https://hu.wikipedia.org/api/rest_v1/page/summary/{_q(n)}", timeout)
        if data and data.get("type") == "standard" and data.get("extract"):
            out.append(f"{data.get('title', n)}: {data['extract'][:400]}")
    return out


def _text(el: Optional[ET.Element]) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(el.text or ""))).strip() if el is not None else ""


_FEED_CACHE: dict = {}


def fetch_feed(url: str, timeout: int) -> list:
    """RSS 2.0 / Atom beolvasása -> [{title, link, summary, published, categories, source}] (futásonként cache-elve)"""
    if url not in _FEED_CACHE:
        _FEED_CACHE[url] = _fetch_feed(url, timeout)
    return [dict(i) for i in _FEED_CACHE[url]]


def _fetch_feed(url: str, timeout: int) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA["User-Agent"],
                                               "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError, ValueError) as e:
        log.warning("Feed nem olvasható: %s (%s)", url, e)
        return []
    host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc)
    items = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        pub = None
        try:
            pub = parsedate_to_datetime(_text(it.find("pubDate")))
        except (TypeError, ValueError):
            pass
        items.append({"title": _text(it.find("title")), "link": _text(it.find("link")),
                      "summary": _text(it.find("description"))[:600], "published": pub,
                      "categories": [_text(c) for c in it.findall("category")], "source": host})
    for it in root.iter(atom + "entry"):
        link = it.find(atom + "link")
        pub = None
        try:
            pub = datetime.fromisoformat(_text(it.find(atom + "updated")).replace("Z", "+00:00"))
        except ValueError:
            pass
        items.append({"title": _text(it.find(atom + "title")), "link": link.get("href", "") if link is not None else "",
                      "summary": _text(it.find(atom + "summary"))[:600], "published": pub,
                      "categories": [c.get("term", "") for c in it.findall(atom + "category")], "source": host})
    return [i for i in items if i["title"] and i["link"].startswith("http")]


def same_story(a: dict, b: dict) -> bool:
    """Ugyanarról az eseményről szól-e két hír (nem csak közös témakör, pl. „kormány”, „cég”)."""
    shared = len(a["kw"] & b["kw"])
    sim = shared / max(1, min(len(a["kw"]), len(b["kw"])))
    if a["source"] == b["source"]:
        return shared >= 4 and sim >= 0.5
    return shared >= 3 and sim >= 0.35


def related_story(a: dict, b: dict) -> bool:
    """Kapcsolódó (de nem ugyanaz) esemény más laptól: külön bekezdésben említhető."""
    shared = len(a["kw"] & b["kw"])
    return a["source"] != b["source"] and shared >= 2 and shared / max(1, min(len(a["kw"]), len(b["kw"]))) >= 0.2


def jina_text(url: str, timeout: int, limit: int = 4000) -> str:
    """Jina Reader (r.jina.ai): bármely oldalt tiszta szöveggé alakít – akkor kell, ha a közvetlen letöltés
    üres (JavaScriptes oldal, átirányítás, tiltás). Kulcs nélkül is megy (percenként ~20 kérés); JINA_API_KEY-jel több."""
    if os.getenv("JINA_READER", "true").lower() not in ("1", "true", "yes"):
        return ""
    headers = {"User-Agent": "KollektivaBot/1.0", "Accept": "text/plain", "X-Return-Format": "text"}
    if os.getenv("JINA_API_KEY"):
        headers["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
    try:
        with urllib.request.urlopen(urllib.request.Request("https://r.jina.ai/" + url, headers=headers),
                                    timeout=min(timeout, 40)) as resp:
            raw = resp.read(400_000).decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""
    paras = []
    for line in raw.splitlines():
        t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line).strip(" #*>-\t")
        if len(t) >= 80 and not re.search(r"cookie|feliratkoz|előfizet|hírlevél|Minden jog fenntartva|^URL Source|^Title:", t, re.I):
            paras.append(t)
    return "\n".join(paras)[:limit]


def fetch_article_text(url: str, timeout: int, limit: int = 4000) -> str:
    """A forráscikk bekezdései (csak háttérnek a tényekhez – a szöveget nem vesszük át).
    Ha a közvetlen letöltés kevés szöveget ad, a Jina Readerrel próbálja."""
    direct = _fetch_article_text_direct(url, timeout, limit)
    if len(direct) >= 600:
        return direct
    via_jina = jina_text(url, timeout, limit)
    return via_jina if len(via_jina) > len(direct) else direct


def _fetch_article_text_direct(url: str, timeout: int, limit: int = 4000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; KollektivaBot/1.0)",
                                               "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(600_000).decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|figure|aside|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    paras = []
    for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", raw):
        t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", p))).strip()
        if len(t) >= 80 and not re.search(r"cookie|feliratkoz|előfizet|hírlevél|Minden jog fenntartva", t, re.I):
            paras.append(t)
    return "\n".join(paras)[:limit]


def title_too_similar(title: str, sources: list) -> bool:
    """Igaz, ha a cím szinte szó szerint a forrás címe (4+ egymást követő azonos szó). Egy-egy közös,
    ütős szó (pl. „éjjel”, „csőd”) megengedett."""
    def words(t: str) -> list:
        return re.findall(r"[a-záéíóöőúüű0-9]+", t.lower())
    tw = words(title)
    tri = {" ".join(tw[i:i + 4]) for i in range(len(tw) - 3)}
    for s in sources:
        sw = words(s.get("title", ""))
        if tri & {" ".join(sw[i:i + 4]) for i in range(len(sw) - 3)}:
            return True
    return False


ORPHAN_SOURCE = re.compile(r"^[–-]\s*(írja|közölte|számolt be)\b.{0,60}$", re.I)


def _keywords(text: str) -> set:
    words = re.findall(r"[a-záéíóöőúüű0-9]{4,}", text.lower())
    return {w[:7] for w in words if w not in STOPWORDS}


def pick_story(section: dict, used_links: set, now: datetime, timeout: int, max_age_h: int = 36,
               limit: int = 3) -> list:
    """A rovat friss híreiből témacsoportokat képez (egy téma = 1–4 forrás), pontszám szerint csökkenő
    sorrendben: minél több kiadó írja, annál forróbb."""
    items, seen = [], set()
    for url, cat_re, kw_re in section["feeds"]:
        for it in fetch_feed(url, timeout):
            if it["link"] in seen or it["link"] in used_links:
                continue
            cats = " ".join(it["categories"])
            if cat_re and not re.search(cat_re, cats, re.I):
                continue
            if kw_re and not re.search(kw_re, it["title"] + " " + it["summary"], re.I):
                continue
            if (SPONSORED.search(cats + " " + it["title"]) or EXCLUDE_ALL.search(it["title"] + " " + it["summary"][:300])
                    or META_STORY.search(it["title"] + " " + it["summary"][:200] + " " + it["link"])):
                continue
            if it["published"] and (now - it["published"]).total_seconds() > max_age_h * 3600:
                continue
            seen.add(it["link"])
            it["kw"] = _keywords(it["title"] + " " + it["summary"][:200])
            items.append(it)
    if not items:
        return []
    scored = []
    for it in items:
        group = [it] + [o for o in items if o is not it and same_story(it, o)]
        group += [dict(o, related=True) for o in items
                  if o is not it and o not in group and related_story(it, o)][:2]
        publishers = {g["source"] for g in group if not g.get("related")}
        fresh = 1.0 if it["published"] and (now - it["published"]).total_seconds() < 12 * 3600 else 0.0
        score = len(publishers) * 3 + len(group) + fresh + min(len(it["summary"]), 400) / 400
        scored.append((score, group))
    scored.sort(key=lambda x: -x[0])
    out, taken = [], set()
    for score, group in scored:
        if group[0]["link"] in taken:
            continue
        taken.update(g["link"] for g in group)
        group = [dict(g) for g in group[:5]]
        group[0]["hot_score"] = round(score, 2)
        out.append(group)
        if len(out) >= limit:
            break
    return out


DUP_SYSTEM = ("Hírszerkesztő vagy. Eldöntöd, hogy egy új hír UGYANARRÓL az eseményről/ügyről szól-e, mint valamelyik "
              "már megírt cikkünk (akkor is, ha más szemszögből, más szavakkal vagy más rovatban). Csak JSON-t adsz vissza.")


def same_story_as_recent(ai: "AIClient", group: list, recent_titles: list) -> bool:
    """Rovatok közötti duplikáció szűrése: a kulcsszavas egyezés nem fogja meg, ha két kiadó másképp fogalmaz
    (pl. „Nem írta alá a Sándor-palota…” vs. „Miért akadt el az új orvosi törvény?”), ezért egy rövid AI-ellenőrzés
    dönt. Hibánál nem szűr (inkább legyen cikk)."""
    if not recent_titles or os.getenv("DUP_CHECK", "true").lower() not in ("1", "true", "yes"):
        return False
    cand = "\n".join(f"- {s.get('title', '')}: {(s.get('summary') or '')[:200]}" for s in group[:3])
    prompt = (f"ÚJ HÍR (több forrás címe és kivonata):\n{cand}\n\nMÁR MEGÍRT CIKKEINK (az elmúlt órákból):\n"
              + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(recent_titles[-40:]))
              + "\n\nUgyanarról a konkrét eseményről/ügyről szól az új hír, mint valamelyik megírt cikk? Ha csak a téma "
                "hasonló (pl. két külön egészségügyi hír), az NEM ugyanaz. Ha ugyanannak az ügynek ÚJ fejleménye (új "
                "nyilatkozat, döntés, letartóztatás, reakció, adat), az sem ugyanaz – arról új cikk kell. JSON: {\"same\": true/false, \"which\": sorszám vagy null}")
    try:
        raw = ai.complete_json(DUP_SYSTEM, prompt, 150, light=True)
    except (AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Duplikáció-ellenőrzés kimaradt: %s", e)
        return False
    if raw.get("same") is True:
        log.info("Kihagyva (már megírtuk, %s. cikk): %s", raw.get("which"), group[0].get("title"))
        return True
    return False


ON_DEMAND_SYSTEM = "Hírszerkesztő vagy. Egy hírt a megfelelő rovatba sorolsz. Csak JSON-t adsz vissza."

SECTION_RULES = (
    "Szabályok: ha a hír lényege kormány, párt, politikus, hatóság, bíróság vagy közpénz (akár kulturális, egészségügyi "
    "vagy gazdasági témában is) → kozelet (külföldi politikánál vilag). Árak, árfolyam, tőzsde, cégek, bérek, adók hatása "
    "a pénztárcára, vásárlás, akciók → penzvilag. Egészség, sport, edzés, alvás, táplálkozás, lelki egészség (akár "
    "kutatás is) → eletmod – ajándék, divat, lakberendezés NEM életmód. Könyv, film, sorozat, zene, filozófia, művészet, "
    "hagyományok → kultura. Eszközök, MI, internet, szoftver, tudomány, űr és csillagászat → tech. Sztárok, hírességek, tévéműsorok, celeb-magánélet → bulvar; ismert szereplő nélküli bűnügy vagy "
    "furcsa eset külföldön → vilag, itthon → kozelet.")


def pick_section(ai: "AIClient", story: list, default: Optional[dict] = None) -> Optional[dict]:
    """A hír tartalma alapján választ rovatot (a forrás-feed rovata csak kiindulás; pl. a Techtud alvás-kutatása → életmód)."""
    if not story:
        return default
    try:
        sec = ai.complete_json(ON_DEMAND_SYSTEM, "Rovatok: " + "; ".join(f"{k} = {v['name']}: {v['focus']}" for k, v in SECTIONS.items() if not v.get("legacy"))
                               + f"\n\n{SECTION_RULES}\n\nHír: {story[0]['title']}\n{(story[0].get('summary') or '')[:400]}\n\n"
                               + 'Melyik rovatba való? JSON: {"section": "rovat azonosító"}', 100, light=True).get("section")
    except (AIError, ValueError, TypeError, KeyError, AttributeError):
        sec = None
    return (SECTIONS.get(sec) if not SECTIONS.get(sec, {}).get("legacy") else SECTIONS["tech"]) or default


PITCH_FILE = BASE_DIR / "data" / "review" / "pitches.json"


def send_pitches(review, cands: list, used_links: set, recent_kw: list) -> None:
    """A kör végén a meg NEM írt, de forró témák listája Telegramra (AI nélkül, ingyen): cím, lap, hány lap hozta.
    A „✍️ N” gombra a robot megírja (ugyanúgy, mintha a linket küldted volna). PITCHES_PER_SLOT (alap 6)."""
    n = int(os.getenv("PITCHES_PER_SLOT", "6"))
    if n <= 0:
        return
    out, seen = [], set()
    for score, sid, group in cands:
        g0 = group[0]
        if g0["link"] in used_links or g0["link"] in seen or any(len(g0["kw"] & rk) >= 4 for rk in recent_kw):
            continue
        seen.add(g0["link"])
        pubs = sorted({g["source"] for g in group if not g.get("related")})
        story = [{"title": g.get("title", ""), "link": g.get("link", ""), "summary": (g.get("summary") or "")[:1500],
                  "source": g.get("source", ""), "related": bool(g.get("related")), "hot_score": g.get("hot_score", score)}
                 for g in group[:5]]
        out.append({"id": hashlib.sha1(g0["link"].encode()).hexdigest()[:10], "link": g0["link"], "title": g0["title"][:140],
                    "sid": sid, "pubs": pubs, "score": round(score, 1), "story": story})
        if len(out) >= n:
            break
    if not out:
        return
    st = read_json(PITCH_FILE, {"items": {}})
    items = dict(list(st.get("items", {}).items())[-60:])
    items.update({o["id"]: o for o in out})
    write_json_atomic(PITCH_FILE, {"items": items})
    chat = review.load_state().get("chat_id")
    if not chat:
        return
    lines = [f"{i + 1}. {o['title']} – {', '.join(o['pubs'][:3])}{' +' + str(len(o['pubs']) - 3) if len(o['pubs']) > 3 else ''} "
             f"({SECTIONS.get(o['sid'], {}).get('name', o['sid'])})" for i, o in enumerate(out)]
    btns = [{"text": f"✍️ {i + 1}", "callback_data": f"pw|{o['id']}"} for i, o in enumerate(out)]
    review.tg("sendMessage", {"chat_id": chat, "disable_web_page_preview": True,
                              "text": "📰 További forró témák (nem írtam meg őket). Ha kell valamelyik, nyomd meg a számát:\n\n"
                                      + "\n".join(lines),
                              "reply_markup": {"inline_keyboard": [btns[k:k + 6] for k in range(0, len(btns), 6)]}})


def build_from_pitch(ai: "AIClient", pit: dict, tz: ZoneInfo, articles: list) -> Optional[dict]:
    """✍️ gomb: a témajavaslat eltárolt forráscsoportjából (hírfolyam-címek, kivonatok, több lap) ugyanúgy ír cikket,
    mint a körök automatikus cikkei. Ha nincs eltárolt csoport, a link alapján (build_on_demand)."""
    story = pit.get("story") or []
    sec = SECTIONS.get(pit.get("sid"))
    if story and sec:
        now = datetime.now(tz)
        group = [dict(s, published=now, categories=[], kw=_keywords(s["title"] + " " + (s.get("summary") or "")[:200]))
                 for s in story if s.get("title")]
        pending_imgs = used_images(articles)
        try:
            art = build_section_article(ai, sec, now.date(), tz, group, pending_imgs, related_past(articles, group))
        except Exception as e:  # noqa: BLE001
            log.warning("Témajavaslatból cikk (forráscsoport) sikertelen: %s", e)
            art = None
        if art:
            art["status"] = "pending"
            _log_sent(art)
            return art
    # régi (forráscsoport nélküli) javaslat vagy hiba: a link, ha a lap letiltja, a cím alapján (Google Hírek-keresés)
    return (build_on_demand(ai, pit.get("link", ""), tz, articles)
            or (build_on_demand(ai, pit["title"], tz, articles) if pit.get("title") else None))


def build_on_demand(ai: "AIClient", text: str, tz: ZoneInfo, articles: list) -> Optional[dict]:
    """A szerkesztő Telegramon küld egy linket vagy témát → a robot cikket ír róla (jóváhagyásra).
    Link: a cikk szövege a forrás; téma: a Google Hírek friss találatai (max. 4 forrás)."""
    now = datetime.now(tz)
    url = (re.search(r"https?://\S+", text) or [None])[0]
    yt = re.search(r"(?:youtube\.com/(?:watch\?v=|live/|shorts/)|youtu\.be/)([\w-]{11})", url or "")
    if yt:  # YouTube-videó: a videó tartalmából (Gemini nézi meg), beágyazott lejátszóval
        import videos
        # kérésre (linkként küldve) ugyanúgy megírja, mint egy cikklinkből – nem szűr hírértékre
        return videos.article_from_video(ai, {"id": yt.group(1), "title": "", "url": f"https://www.youtube.com/watch?v={yt.group(1)}",
                                              "author": "", "description": ""}, tz, articles, force=True)
    story = []
    if url:
        body = fetch_article_text(url, ai.cfg.http_timeout, 6000)
        title = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; KollektivaBot/1.0)"})
            with urllib.request.urlopen(req, timeout=ai.cfg.http_timeout) as resp:
                head = resp.read(200_000).decode("utf-8", "ignore")
            m = (re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', head, re.I)
                 or re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S))
            title = html.unescape(m.group(1)).strip() if m else ""
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass
        if not body and not title:
            return None
        host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc)
        story = [{"title": title or text[:120], "link": url, "summary": body[:600], "source": host,
                  "published": now, "categories": [], "fulltext": body}]
        if title:  # további források ugyanerről (más kiadók), hogy ne egyetlen cikkből dolgozzunk
            kw0 = _keywords(title)
            more = fetch_feed("https://news.google.com/rss/search?q=" + urllib.parse.quote(" ".join(list(kw0)[:6]) or title[:80])
                              + "&hl=hu&gl=HU&ceid=HU:hu", ai.cfg.http_timeout)
            story += [it for it in more if it.get("link") != url and len(kw0 & _keywords(it.get("title", ""))) >= 2][:3]
    else:
        q = urllib.parse.quote(text.strip()[:120])
        items = fetch_feed(f"https://news.google.com/rss/search?q={q}&hl=hu&gl=HU&ceid=HU:hu", ai.cfg.http_timeout)
        story = items[:4]
        if not story:
            return None
    for s in story:
        s["kw"] = _keywords(s["title"] + " " + (s.get("summary") or "")[:200])
    section = pick_section(ai, story, SECTIONS.get("kozelet") or next(iter(SECTIONS.values())))
    recent_imgs = used_images(articles)
    art = build_section_article(ai, section, now.date(), tz, story, recent_imgs, related_past(articles, story))
    if art:
        art["status"] = "pending"
        art["requested"] = True
        art["hot_score"] = max(art.get("hot_score") or 0, 5)
    return art


def _proper_keys(text: str) -> set:
    """Tulajdonnevek (nagybetűs szavak, nem mondatkezdők) kulcsai: személy, hely, intézmény, márka."""
    out = set()
    for m in re.finditer(r"(?<![.!?:–]\s)(?<!^)\b([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]{3,})", text.strip()):
        w = m.group(1).lower()
        if w not in STOPWORDS:
            out.add(w[:7])
    return out


def related_past(articles: list, story: list, limit: int = 3, days: int = 45) -> list:
    """„Belső memória”: a korábbi saját cikkeink közül azok, amelyek ugyanarról az ügyről/szereplőről szóltak
    (legalább 3 közös kulcsszó). Ezek háttérként mennek a cikkíráshoz, és „Korábban írtuk” linkként a cikk alá."""
    kw = set()
    for s in story[:3]:
        kw |= s.get("kw") or _keywords(s.get("title", "") + " " + (s.get("summary") or "")[:200])
    links = {s.get("link") for s in story}
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    pool = [a for a in articles if a.get("status") == "published" and (a.get("date") or "") >= cutoff
            and not links & set(a.get("category_meta", {}).get("source_links", []))]
    akw = [(a, _keywords(a.get("title", "") + " " + (a.get("lead") or ""))) for a in pool]
    names = set()
    for s_ in story[:3]:
        names |= _proper_keys(s_.get("title", "") + ". " + (s_.get("summary") or "")[:300])
    df: dict = {}
    for _, ks in akw:
        for k in ks:
            df[k] = df.get(k, 0) + 1
    n_docs = max(len(akw), 1)

    def weight(k: str) -> float:  # ritka szó = erős kapcsolat, gyakori szó = gyenge
        f = df.get(k, 0) / n_docs
        return 1.0 if f <= 0.03 else 0.6 if f <= 0.08 else 0.25
    scored = []
    for a, ks in akw:
        common = kw & ks
        w = round(sum(weight(k) for k in common), 2)
        # kell legalább egy közös tulajdonnév (személy, hely, intézmény) – különben csak a téma hasonló
        shared_names = names & _proper_keys(a.get("title", "") + ". " + (a.get("lead") or ""))
        if len(common) >= 3 and w >= 2.4 and shared_names:
            scored.append((w, a.get("date", ""), a))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)  # legtöbb közös szó, azon belül a legfrissebb
    return [{"id": a.get("id"), "title": a.get("title"), "lead": a.get("lead"), "url": a.get("url"),
             "date": a.get("date"), "score": n, "thread_id": a.get("thread_id") or a.get("id")}
            for n, _, a in scored[:limit]]


def _fold(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (t or "").lower().translate(
        str.maketrans("áéíóöőúüű", "aeiooouuu")))).strip()


def dedupe_quote(body: list, quote_text: str) -> list:
    """Kiveszi a szövegből azt a mondatot, ami a kiemelt idézetet ismétli (pl. „…úgy fogalmazott: <idézet>”).
    Ha a mondat elején körülmény áll (kettőspont előtt), az megmarad: „X a kormányülésről reagált a fejleményekre.”"""
    qw = {w for w in _fold(quote_text).split() if len(w) > 3}
    if len(qw) < 4:
        return body
    out = []
    for para in body:
        if BULLET.match(para.strip()):
            out.append(para)
            continue
        kept = []
        for sent in re.split(r"(?<=[.!?…])\s+", para.strip()):
            sw = {w for w in _fold(sent).split() if len(w) > 3}
            if sw and len(qw & sw) / len(qw) >= 0.5:
                head = sent.split(":", 1)[0].strip().rstrip(",;–- ") if ":" in sent else ""
                head = re.sub(r",?\s*(és\s+)?(a\s+\S+\s+oldalán\s+)?úgy fogalmazott$", "", head).strip().rstrip(",")
                if len(head) > 30:
                    kept.append(head + ".")
                continue
            kept.append(sent)
        if kept:
            out.append(" ".join(kept))
    return out or body


def checked_quote(q, story: list, n_paras: int) -> Optional[dict]:
    """Kiemelt idézet csak ellenőrzötten: a megszólaló neve szerepeljen a forrásokban, és magyar forrásnál az idézet
    eleje betű szerint is (idegen nyelvű forrásnál fordítás, ott a név elég). Különben inkább nincs idézet."""
    if not isinstance(q, dict):
        return None
    text = str(q.get("text") or "").strip().strip("„”\"“ ")
    who = str(q.get("who") or "").strip()
    if not (20 <= len(text) <= 320 and who):
        return None
    src = " ".join(f"{s.get('title', '')} {s.get('summary') or ''} {s.get('fulltext') or ''}" for s in story)
    fsrc, ftext = _fold(src), _fold(text)
    names = [_fold(w) for w in re.findall(r"\b[A-ZÁÉÍÓÖŐÚÜŰ][\wáéíóöőúüű.-]{2,}", who)]
    if not names or not any(n and n in fsrc for n in names):
        return None  # a megszólaló neve nem szerepel a forrásokban
    hungarian = len(re.findall(r"[őűáé]", src.lower())) > len(src) / 200
    if hungarian and ftext[:28] not in fsrc:
        return None
    try:
        after = max(0, min(int(q.get("after", 1)), n_paras - 2))
    except (TypeError, ValueError):
        after = 1
    return {"text": text, "who": who, "after": after}


HEADLINE_FEEDS = [("444", "https://444.hu/feed"), ("Telex", "https://telex.hu/rss"),
                  ("Index", "https://index.hu/24ora/rss/"), ("24.hu", "https://24.hu/feed/"), ("HVG", "https://hvg.hu/rss")]
_HEADLINES: Optional[list] = None


def headline_examples(timeout: int) -> list:
    """A nagy magyar lapok friss címei stílusmintának (futásonként egyszer letöltve; a 444-ből több, mert annak
    a csípős, ironikus clickbait-stílusa a legjobb minta). Csak a cím ritmusát/csavarját tanulja belőle az AI."""
    global _HEADLINES
    if _HEADLINES is None:
        out = []
        for name, url in HEADLINE_FEEDS:
            items = fetch_feed(url, timeout)[: (10 if name == "444" else 4)]
            out += [f"{name}: {it['title']}" for it in items if it.get("title")]
        _HEADLINES = out
        log.info("Címminta: %d friss cím a nagy lapoktól (%s)", len(out), ", ".join(sorted({o.split(':')[0] for o in out})) or "egy sem")
    return _HEADLINES


CONTEXT_FILE = BASE_DIR / "data" / "context_hu.json"
CABINET_PAGE = os.getenv("CABINET_PAGE", "Magyar-kormány")  # kormányváltáskor ezt kell átírni (vagy env)
# Tartalék, ha a Wikipédia nem érhető el (2026. október). A robot naponta frissíti a Wikipédia kormány-szócikkéből.
CABINET_FALLBACK = (
    "Miniszterelnök: Magyar Péter (TISZA, 2026. május 9. óta); Miniszterelnök-helyettes, külügyminiszter: Orbán Anita; "
    "Miniszterelnök-helyettes, Miniszterelnökséget vezető miniszter: Ruff Bálint; Agrár- és élelmiszergazdaságért felelős "
    "miniszter: Bóna Szabolcs; Belügyminiszter: Pósfai Gábor; Egészségügyi miniszter: Hegedűs Zsolt; Élő környezetért "
    "felelős miniszter: Gajdos László; Gazdasági és energetikai miniszter: Kapitány István; Honvédelmi miniszter: "
    "Ruszin-Szendi Romulusz; Igazságügyi miniszter: Görög Márta; Közlekedési és beruházási miniszter: Vitézy Dávid; "
    "Oktatási és gyermekügyi miniszter: Lannert Judit; Pénzügyminiszter: Kármán András; Szociális és családügyi "
    "miniszter: Kátai-Németh Vilmos; Társadalmi kapcsolatokért és kultúráért felelős miniszter: Tarr Zoltán; Tudományos "
    "és technológiai miniszter: Tanács Zoltán; Vidék- és településfejlesztési miniszter: Lőrincz Viktória; "
    "Köztársasági elnök: Baka András; Az ellenzék vezetője: Orbán Viktor (Fidesz, volt miniszterelnök 2010–2026)")
_BOLD = "'" * 3


def _unwiki(t: str) -> str:
    t = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"<[^>]+>|\{\{[^}]*\}\}", "", t).replace(_BOLD, "").replace("''", "")
    return re.sub(r"\s+", " ", t).strip(" |,")


def cabinet_text(timeout: int = 15) -> str:
    """A kormány összetétele a Wikipédia kormány-szócikkéből (wikitext): „Tisztség: Név” sorok + államfő, ellenzékvezető."""
    try:
        req = urllib.request.Request(f"https://hu.wikipedia.org/w/index.php?title={_q(CABINET_PAGE)}&action=raw",
                                     headers=WIKI_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            w = r.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("Kormány-szócikk nem érhető el: %s", e)
        return ""
    out = []
    if '{| class="wikitable"' in w:
        tbl = w.split('{| class="wikitable"', 1)[1].split("|}", 1)[0]
        for row in tbl.split("|-"):
            cells = [c[1:].strip() for c in row.strip().split("\n") if c.startswith("|") and not c.startswith("|}")]
            if len(cells) >= 4 and _BOLD in cells[1] and not cells[3]:  # betöltött tisztség (nincs „hivatal vége”)
                out.append(f"{_unwiki(cells[0])}: {_unwiki(cells[1])}")
    m = re.search(r"vezető név 1 = (.*?)\n\| vezető cím 2", w, re.S)
    if m:
        cur = [x for x in m.group(1).split("*") if "–)" in x]
        if cur:
            out.append("Köztársasági elnök: " + _unwiki(cur[-1].split("<br>")[0]))
    m = re.search(r"ellenzék vezére = ([^\n]+)", w)
    if m:
        out.append("Az ellenzék vezetője: " + _unwiki(m.group(1).split("<br>")[0]))
    return "; ".join(out) if len(out) >= 5 else ""


def current_context(timeout: int = 15) -> str:
    """Napi egyszer frissülő háttér: kik töltik be MOST a legfontosabb tisztségeket (a kormány Wikipédia-szócikkéből,
    tartalékként a beégetett lista). Az AI tudása egy adott dátumnál lezárul, ezért enélkül elavult tisztséget írna
    (pl. Orbán Viktort miniszterelnöknek), és a jogi ellenőr is „javítaná” a helyes tisztséget."""
    c = read_json(CONTEXT_FILE, {})
    today = date.today().isoformat()
    if c.get("date") != today or "Miniszterelnök" not in c.get("text", ""):
        cab = cabinet_text(timeout)
        c = {"date": today, "text": "Magyarország kormánya és vezetői (aktuális): " + (cab or CABINET_FALLBACK),
             "source": "wikipedia" if cab else "fallback"}
        write_json_atomic(CONTEXT_FILE, c)
    return c.get("text", "")


def section_prompt(section: dict, story: list, d: date, context: Optional[list] = None,
                   past: Optional[list] = None, examples: Optional[list] = None) -> str:
    src = "\n\n".join(f"[{i + 1}]{' [KAPCSOLÓDÓ]' if s.get('related') else ''} {s['source']} – {s['title']}\n{s['summary']}"
                      + (f"\nRészletek a cikkből:\n{s['fulltext']}" if s.get("fulltext") else "")
                      for i, s in enumerate(story))
    bg = "\n".join(f"- {c}" for c in (context or []))
    bg_block = f"\nHáttér a szereplőkhöz (magyar Wikipédia – csak magyarázatra, ha tényleg ugyanarról van szó):\n{bg}\n" if bg else ""
    if past and max(p.get("score", 0) for p in past) >= 3.5:
        bg_block += ("\nFOLYTATÁS: ez egy folyamatban lévő ügy ÚJ fejleménye, amelyről korábban már írtunk. A cikk az ÚJ "
                     "fejleményről szóljon (a cím is ezt tükrözze); az előzményeket legfeljebb egy rövid bekezdésben "
                     "foglald össze, és ne ismételd meg a korábbi cikkek tartalmát.\n")
    if past:
        bg_block += ("\nKorábbi cikkeink ugyanebben az ügyben (előzményként használhatod – pl. „ahogy korábban megírtuk” –, "
                     "de csak ha tényleg ugyanarról szól; új tényt ne találj ki belőlük):\n"
                     + "\n".join(f"- {p['date']}: {p['title']} – {p.get('lead') or ''} (link: {p.get('url') or '–'})" for p in past)
                     + "\nHa a szövegben természetesen adódik (pl. „ahogy korábban megírtuk”), legfeljebb 2 helyen linkelj ezekre "
                     "markdown formában: [rövid szövegrész](/rovat/cikk/) – CSAK a fent megadott linkeket használd.\n")
    if section.get("id") != "univerzum":
        ctx = current_context()
        if ctx:
            bg_block += ("\nAKTUÁLIS HÁTTÉR (napi frissítés) – a tisztségeknél (ki a miniszterelnök, ki van "
                         "kormányon, ki az ellenzék) ehhez és a forrásokhoz igazodj, NE a saját emlékeidhez, mert azok elavultak "
                         "lehetnek:\n" + ctx + "\n")
    if examples:
        bg_block += ("\nCÍMSTÍLUS – így címeznek ma a nagy magyar lapok (csak stílusminta: ritmus, csavar, irónia, "
                     "kattintásra csábító fordulat; a címeiket NE másold, a tényeiket ne vedd át):\n"
                     + "\n".join(f"- {e}" for e in examples) + "\n")
    return f"""Rovat: {section['name']} ({section['focus']}). Dátum: {hu_date(d)}.
A rovat hangja: {section.get('voice', 'természetes, újságírói')}.

Forráskivonatok:
{src}
{bg_block}
A forrásszámokat ([1], [2]…) SOHA ne írd a cikk szövegébe – az olvasó nem látja a kivonatokat.
FŐ TÉMA az [1]-es forrás eseménye. A [KAPCSOLÓDÓ] jelű források másik, de összefüggő eseményről szólnak: ha tényleg
tágítják a képet, külön bekezdés(ek)ben, egyértelmű átvezetéssel említsd őket („Közben…”, „Egy másik ügyben…”),
de a tényeiket SOHA ne keverd a fő eseményével. Ha nem illenek, hagyd ki őket.
ÖSSZEFOGLALÓ: ha a cikk végül egynél több, külön eseményről szól, akkor legyen nyíltan összefoglaló: a cím ezt
jelezze (pl. „Tech-körkép: …”, „A nap legérdekesebb űrhírei”), a "key_points"-ban eseményenként egy pont, és minden
esemény külön blokkban szerepeljen, a blokk első bekezdése egy „## ” kezdetű alcímsor legyen
(pl. „## Leállt a Starship-tesztek sora”) – csak maga az alcím, „Alcím:” vagy „Rövid alcím:” címke nélkül.

Írj ebből egy eredeti, magyar nyelvű magazincikket:
- "title": RÖVID (max. 9 szó), ütős, kattintásra csábító cím: csavar, irónia, kíváncsiságot keltő fordulat,
  meglepő szám vagy kérdés. A poént NEM kell lelőni (nem kell minden részletnek benne lennie), de legyen benne
  egy konkrét elem (szereplő, ország, szám, tárgy), hogy ne hasson elvontnak. TILOS a sci-fi- vagy mesehangulatú,
  általánosító cím, amiről azt hihetné az olvasó, hogy régi vagy kitalált történet (rossz: „Robotok fordították
  meg a fronthelyzetet”; jobb: „Robotokkal törték át az ukránok az orosz vonalat”). Legyen igaz,
  pártpolitikailag semleges, ne ijesztgessen
  Ha a téma engedi (politikai húzások, abszurd helyzetek, bulvár), a cím lehet ironikus/szarkasztikus is – de
  tragédiánál, áldozatoknál, betegségnél SOHA.
  Ha a hír egy ismert személyről szól (politikus, híresség, sportoló), a NEVE szerepeljen a címben – ne írd körül
  („a volt miniszter”, „egy ismert színész”), mert a név hozza a kattintást.
- "title_options": 2 további címváltozat (ugyanazokkal a szabályokkal), tömbként. Nem kell teljesen másnak lennie:
  a fontos kulcsszavak (név, helyszín, a lényeg) maradhatnak benne, a változat a szórendben, hangsúlyban,
  hangzásban térjen el
- "clickbait_titles": 3 további cím, ami a lehető legkattintósabb (erős érzelem, rejtély, „ezt nem fogod elhinni”
  hatás, kérdés, szám, csípős irónia, mint a 444 címei) – de továbbra is IGAZ, nem állít olyat, ami nincs a
  cikkben, és nem sértő. TILOS az olcsó, bármire ráhúzható sablon („Ezt nem hinnéd el”, „Nem fogod elhinni”,
  „Döbbenetes”, „Sokkoló”, „Mindenki erről beszél”): a kíváncsiságot a sztori konkrét, meglepő részlete keltse.
  Bűnügynél, tragédiánál, áldozatoknál nincs clickbait-poén.
- Írásjel a címek végén: pont SOHA; kérdőjel csak valódi kérdésnél, felkiáltójel csak ritkán
- "lead": 2 mondatos bevezető: mi történt és miért fontos
- "key_points": 3–5 rövid, egymondatos pont a lényegről („Röviden” doboz)
- "body": bekezdések tömbje. A HOSSZ A TARTALOMHOZ IGAZODJON: egyszerű hírnél 300–450 szó elég; ha a téma
  érdekes és a forrásokban (vagy a háttérben) sok valódi tény, előzmény, szám, álláspont van, mehet 600–900 szó
  (3–5 perc olvasás). SOHA ne nyújtsd a szöveget: minden mondat új információt adjon, ismétlés, általánosság,
  „kerekítő” zárómondat tilos – a kevesebb néha több. Tartalom: a tények, előzmények és háttér (ki kicsoda,
  mi történt korábban), számok és összefüggések, eltérő álláspontok, és hogy mit jelent ez a
  hétköznapi olvasónak. NE a forrás megnevezésével kezdd: az első mondat magáról az eseményről szóljon.
  A forrást elég egyszer, természetesen beépítve említeni valahol a szövegben (pl. „– írta a Telex”), vagy
  el is hagyhatod, mert a források listája a cikk alatt ott van.
  Ha személy szerepel, első említéskor egy rövid jelzővel mutasd be, ki ő (pl. „Kovács Anna, az MNB
  alelnöke”) – csak ha ez a forrásból kiderül. Ahol illik, egy bekezdés lehet felsorolás: sorok „- ” jellel.
  Ha egy fogalom, ügy vagy intézmény nem köztudott (pl. „ügynökakták”), egy mondatban magyarázd el, mi az.
  Ha a forrásokból nem derül ki valami, ne találgass.
- "tags": 3–5 rövid címke
- "image_query": 1–4 szavas keresőkifejezés a Wikimedia Commonshoz. Ha a hír főszereplője egy közszereplő,
  az ő TELJES NEVE legyen (pl. "Ruff Bálint", "Magyar Péter") – a portré a legjobb kép. Egyébként a konkrét
  helyszín, intézmény, cég, termék vagy tárgy (ANGOLUL vagy tulajdonnévként). Az Országház / Parliament CSAK akkor,
  ha a hír magáról a parlamenti ülésről szól. Ha a hír egy konkrét, ismert
  személyről, helyről, intézményről vagy tárgyról szól, AZ legyen (pl. "Hungarian Parliament Building",
  "James Webb Space Telescope", "Viktor Orbán", "Eötvös Loránd University"); különben egy kifejező, konkrét téma
  (pl. "Budapest Stock Exchange"). Magyar hírnél magyar helyszínt/intézményt keress, ne általános külföldi képet.
- "image_query_alt": 2–4 további konkrét angol keresőkifejezés a cikkben szereplő más személyekre, helyekre,
  tárgyakra (pl. ["ELTE Budapest", "Centrál Színház Budapest"])
- "image_generic": 1–2 szavas ANGOL, egyszerű, fotózható téma a hangulatképhez (ha van jobb, 2 ilyen listában)
  (pl. "coffee cup", "courtroom", "police car", "stock market", "theatre stage", "rocket launch")
- "inline_images": 0–2 szövegközi kép. CSAK akkor, ha a cikk egy konkrét, fotózható dolgot említ, amit az olvasó
  szívesen látna, és ami NEM a főkép témája (pl. egy hadihajó-típus, épület, jármű, eszköz, helyszín, másik szereplő).
  Elemei: {{"after": annak a bekezdésnek a sorszáma (0-tól), ami után jöjjön, "query": pontos angol név vagy
  tulajdonnév a Wikimedia Commonshoz (pl. "USS Gerald R. Ford", "Keleti railway station"), "caption": rövid magyar
  képaláírás}}. Ha nincs ilyen, üres tömb.
- "quote": ha a forrásokban egy szereplő SZÓ SZERINTI, idézőjeles mondata szerepel, ami a cikk lényegéhez tartozik,
  azt kiemelt idézetként add meg: {{"text": "az idézet (magyar forrásnál betű szerint, idegen nyelvűnél hű
  fordításban)", "who": "név, rövid szerep (pl. Orbán Viktor miniszterelnök)", "after": bekezdés sorszáma (0-tól)}}.
  SOHA ne találj ki és ne fogalmazz át idézetet; ha nincs valódi idézet, legyen null. Ha adsz idézetet, a "body"
  NE ismételje meg és ne parafrazálja a tartalmát – a szövegben csak a körülmény álljon (ki, hol, mire reagálva mondta).

Kizárólag ezt a JSON-t add vissza:
{{"title": "...", "title_options": ["...", "..."], "lead": "...", "key_points": ["...", "...", "..."], "body": ["...", "..."], "tags": ["..."], "image_query": "...", "image_query_alt": ["..."], "image_generic": "...", "inline_images": [], "quote": null}}"""


def build_section_article(ai: AIClient, section: dict, d: date, tz: ZoneInfo, story: list,
                          avoid_images: Optional[set] = None, past: Optional[list] = None) -> Optional[dict]:
    now = datetime.now(tz)
    if ai and getattr(ai, "enabled", True):
        better = pick_section(ai, story, section)
        if better and better["id"] != section["id"]:
            log.info("Rovat-javítás: %s → %s (%s)", section["id"], better["id"], story[0].get("title", "")[:80])
            section = better
    for s in story[:4]:
        s["fulltext"] = s.get("fulltext") or fetch_article_text(s["link"], ai.cfg.http_timeout)
    try:
        context = wiki_context(story, ai.cfg.http_timeout)
        raw = ai.complete_json(SECTION_SYSTEM, section_prompt(section, story, d, context, past,
                                                                   headline_examples(ai.cfg.http_timeout)), 4000)
        validate_retro(raw)
        raw = editorial_polish(ai, raw)
        raw = critical_review(ai, raw, story)
        if title_too_similar(str(raw.get("title", "")), story):
            try:
                alt = ai.complete_json(SECTION_SYSTEM, (
                    "Ez a cím szinte szó szerint a forrás címe. Írj 3 új, rövid (max. 7 szó), közepesen clickbait, igaz "
                    "címet, más megfogalmazással (egy-egy ütős kulcsszó maradhat).\n"
                    f"Jelenlegi cím: {raw.get('title')}\nForráscímek: " + " | ".join(s["title"] for s in story)
                    + f"\nLead: {raw.get('lead')}\nJSON: {{\"titles\": [\"...\", \"...\", \"...\"]}}"), 600)
                for t in alt.get("titles") or []:
                    if t and not title_too_similar(str(t), story):
                        raw["title"] = str(t).strip()
                        break
            except (AIError, ValueError, TypeError, KeyError) as e:
                log.warning("Címcsere kimaradt: %s", e)
        raw["body"] = [p for p in (raw.get("body") or []) if not ORPHAN_SOURCE.match(str(p).strip())]
        art = validate_retro(raw)
    except (AIError, ValueError, TypeError, KeyError) as e:
        log.error("[%s] AI cikkírás sikertelen: %s – kimarad.", section["id"], e)
        return None
    generic = raw.get("image_generic")
    image_options = find_images([raw.get("image_query"), *(raw.get("image_query_alt") or [])][:5],
                                (generic if isinstance(generic, list) else [generic])[:2], ai.cfg.http_timeout,
                                avoid_images, limit=IMAGE_OPTIONS)
    image_options = drop_used(image_options, avoid_images)
    strict = section["id"] == "bulvar"  # celebhírnél csak nagyon illő kép (különben inkább a rovat grafikája)
    image_options = vision_rank(ai, art["title"], art["lead"], image_options, min_score=7 if strict else 5)
    if len(image_options) < 2 and not strict:
        image_options += drop_used(more_images(ai, art, (avoid_images or set()) | {im["url"] for im in image_options}),
                                   avoid_images)
    if not image_options:  # nincs illő kép -> saját grafika, hogy a cikk ne kép nélkül menjen jóváhagyásra
        image_options = auto_illustration(ai, art, section["id"])
    image = image_options[0] if image_options else None
    inline_images = find_inline_images(raw, len(art["body"]), ai.cfg.http_timeout,
                                       (avoid_images or set()) | {im["url"] for im in image_options})
    art["body"] = strip_bad_links(art["body"], {p.get("url") for p in past or [] if p.get("url")})
    quote = checked_quote(raw.get("quote"), story, len(art["body"]))
    if quote:  # ne legyen kétszer ugyanaz: kiemelt idézet + ugyanaz a mondat a szövegben
        art["body"] = dedupe_quote(art["body"], quote["text"])
        quote["after"] = max(0, min(quote["after"], len(art["body"]) - 2))
    title_options = [art["title"]]
    for t in raw.get("title_options") or []:
        t = _clean_title(str(t).strip())
        if t and t not in title_options and not title_too_similar(t, story):
            title_options.append(t)
    title_options = [c for c in (_clean_title(t) for t in title_options[:3]) if c] or [art["title"]]
    title_options = _rank_titles(ai, title_options, art.get("lead", ""))
    art["title"] = title_options[0]
    clickbait_from = len(title_options)  # innentől a 🔥 „maximum clickbait” címek (Telegramon külön jelölve)
    for t in raw.get("clickbait_titles") or []:
        t = _clean_title(str(t).strip())
        if t and t not in title_options and not title_too_similar(t, story) and len(title_options) < clickbait_from + 3:
            title_options.append(_clean_title(t))
    title_options = proofread_titles(ai, title_options) or title_options
    art["title"] = title_options[0]
    now_iso = now.isoformat(timespec="seconds")
    slug = slugify(f"{d.isoformat()}-{art['title']}")
    sources = normalize_sources([{"url": s["link"], "title": s["title"], "publisher": s["source"]} for s in story], now_iso)
    full_text = " ".join([art["lead"], *art["body"]])
    auto_publish = os.getenv("RETRO_AUTO_PUBLISH", "false").lower() in ("1", "true", "yes")
    status = "published" if auto_publish else "needs_review"
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{SITE_URL}/{section['id']}/{slug}")),
        "slug": slug, "status": status, "category": section["id"], "subcategory": None,
        "tags": art["tags"], "title": art["title"], "subtitle": None, "lead": art["lead"],
        "key_points": [str(k).strip() for k in (raw.get("key_points") or []) if str(k).strip()][:4],
        "content": to_markdown(art), "content_format": "markdown", "body": art["body"], "pull_quote": None,
        "reading_time_min": reading_time(full_text), "word_count": len(re.findall(r"\w+", full_text)),
        "locale": "hu-HU", "hero_image": image, "inline_images": inline_images, "sources": sources, "quote": quote,
        "authorship": {"mode": "ai_generated", "byline": "Kollektíva szerkesztőség", "model": ai.label.split(":", 2)[-1],
                       "prompt_version": "section-v2", "reviewed_by": None, "reviewed_at": None},
        "hot_score": story[0].get("hot_score", 0),
        "title_options": title_options, "clickbait_from": clickbait_from, "image_options": image_options,
        "story": [{k: s.get(k) for k in ("title", "link", "summary", "source", "related", "hot_score")} for s in story],
        "category_meta": {"source_links": [s["link"] for s in story if not s.get("related")]},
        "date": d.isoformat(), "date_label": f"{HU_MONTHS[d.month - 1]} {d.day}.",
        "url": f"/{section['id']}/{slug}/",
        "seo": {"meta_title": art["title"][:60], "meta_description": art["lead"][:160],
                "canonical_url": f"{SITE_URL}/{section['id']}/{slug}/", "og_image": image["url"] if image else None,
                "noindex": status != "published", "schema_type": "NewsArticle"},
        "monetization": {"ads_enabled": True, "brand_safety": "safe", "sponsored": False,
                         "sponsor_name": None, "affiliate_links": False},
        "related_ids": [p["id"] for p in past or [] if p.get("id")],
        "see_also": [{"title": p["title"], "url": p["url"], "date": p.get("date")} for p in past or [] if p.get("url")],
        "thread_id": next((p["thread_id"] for p in past or [] if p.get("score", 0) >= 3.5 and p.get("thread_id")), None),
        "dedupe_hash": hashlib.sha256(f"{section['id']}|{story[0]['link']}".encode()).hexdigest(),
        "pipeline_run_id": os.getenv("GITHUB_RUN_ID"), "generator": ai.label,
        "created_at": now_iso, "updated_at": now_iso, "published_at": now_iso if status == "published" else None,
        "expires_at": None,
    }


TITLE_JUDGE = (
    "Magyar online lap címszerkesztője vagy. Címváltozatokat rangsorolsz aszerint, melyikre kattintana egy átlagos "
    "magyar olvasó a telefonján, ÉS melyik érthető elsőre. Jó cím: konkrét (ki/mi + mi történt vagy miért érdekes), "
    "rövid (max. 9 szó), van benne feszültség vagy újdonság, nem hazudik, nem homályos metafora, nem tükörfordítás. "
    "Rossz cím: általános („A … ára”), érthetetlen kép, túl hosszú, nem derül ki belőle a téma. Csak JSON-t adsz vissza."
)


def proofread_titles(ai: "AIClient", titles: list) -> list:
    """Helyesírás-javítás a címjavaslatokon (ékezet, ragozás, egybe-/különírás, nyelvtan) – a tartalom és a stílus marad."""
    titles = [t for t in titles if t]
    if not titles or os.getenv("TITLE_PROOFREAD", "true").lower() not in ("1", "true", "yes"):
        return titles
    prompt = ("Javítsd ki a helyesírási, ékezet-, ragozási, egybe-/különírási és nyelvtani hibákat az alábbi magyar "
              "újságcímekben (a magyar helyesírás szabályai szerint). A tartalmon, a szórenden és a stíluson NE változtass; "
              "ha egy cím hibátlan, add vissza változatlanul.\n\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
              + '\n\nJSON: {"titles": ["…ugyanannyi cím, ugyanebben a sorrendben…"]}')
    try:
        raw = ai.complete_json("Magyar korrektor vagy egy online lapnál. Csak JSON-t adsz vissza.", prompt, 500, light=True)
    except Exception as e:  # noqa: BLE001
        log.warning("Cím-korrektúra kimaradt: %s", e)
        return titles
    fixed = [_clean_title(str(t).strip()) for t in raw.get("titles") or []]
    if len(fixed) != len(titles):
        return titles
    out = []
    for old, new in zip(titles, fixed):  # csak apró javítást fogadunk el (ne írja át a címet)
        out.append(new if new and abs(len(new) - len(old)) <= max(6, len(old) // 5) else old)
    return out


def _rank_titles(ai: "AIClient", titles: list, lead: str) -> list:
    """Címlektor: a legjobb (legérthetőbb és legkattintósabb) cím kerül előre; ha mind gyenge, egy jobbat is írhat."""
    if len(titles) < 2 or os.getenv("TITLE_JUDGE", "true").lower() not in ("1", "true", "yes"):
        return titles
    prompt = (f"Bevezető: {lead[:400]}\n\nCímek:\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
              + "\n\nJSON: {\"order\": [sorszámok a legjobbtól], \"better\": \"ha mindegyik gyenge (homályos, "
                "általános vagy túl hosszú), egy jobb cím ugyanerre a hírre, különben null\"}")
    try:
        raw = ai.complete_json(TITLE_JUDGE, prompt, 200, light=True)
    except (AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Címlektor kimaradt: %s", e)
        return titles
    order = [int(i) - 1 for i in raw.get("order") or [] if str(i).isdigit() and 0 < int(i) <= len(titles)]
    ranked = [titles[i] for i in dict.fromkeys(order)] + [t for i, t in enumerate(titles) if i not in order]
    better = _clean_title(str(raw.get("better") or "").strip()) if raw.get("better") else ""
    if better.strip(" \"'").lower() in ("null", "none", "nincs", "-", ""):  # a modell néha szövegként írja: "null"
        better = ""
    if better and better not in ranked and len(better.split()) <= 11:
        ranked = [better] + ranked[:len(titles) - 1]
    return ranked


SENT_LOG = BASE_DIR / "data" / "review" / "sent_log.json"


def _sent_log(hours: float = 36) -> list:
    """A Telegramra már elküldött témák naplója (cím, kulcsszavak, forráslinkek, képjelöltek). Akkor is véd az
    újraírás ellen, ha a függő listából a cikk kiesett (elvetés, lejárat, félbeszakadt mentés)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return [e for e in read_json(SENT_LOG, {"items": []}).get("items", []) if (e.get("t") or "") >= cutoff]


def _log_sent(art: dict) -> None:
    items = _sent_log(72)
    items.append({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "title": art.get("title", ""), "cat": art.get("category"),
                  "kw": sorted(_keywords(art.get("title", "") + " " + " ".join(s.get("title", "") for s in art.get("sources", []))))[:40],
                  "links": art.get("category_meta", {}).get("source_links", []),
                  "imgs": [im.get("url") for im in art.get("image_options") or [] if im.get("url") and not im.get("generated")][:12]})
    write_json_atomic(SENT_LOG, {"items": items[-300:]})


def _same_images(art: dict, log_items: list) -> Optional[str]:
    mine = {im.get("url") for im in art.get("image_options") or [] if im.get("url") and not im.get("generated")}
    for e in log_items:
        if len(mine & set(e.get("imgs") or [])) >= 2:
            return e.get("title")
    return None


def run_sections(ai: AIClient, d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool) -> int:
    """Nincs napi cikkszám-korlát: minden futás (napközben kétóránként) összegyűjti az összes rovat friss
    témáit, forróság szerint rangsorolja, és a legjobb MAX_ARTICLES_PER_RUN (alap: 2) új témáról ír – így a
    cikkek elosztva, a nap folyamán jelennek meg. Ami már megjelent, azt nem írja meg újra."""
    if not ai.enabled:
        log.warning("Rovatcikkekhez AI kell – kimarad.")
        return 0
    review = None
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        import telegram_review as review  # szerkesztői ellenőrzés Telegramon
        review.poll(output_dir, ai, tz)  # előbb a beérkezett gombnyomások (ezek módosíthatják az articles.json-t)
        if not review.enabled(output_dir):
            review = None
    path = output_dir / "articles.json"
    data = read_json(path, {"articles": []})
    articles = data.get("articles", [])
    pending = review.load_pending(output_dir) if review else []
    used_links = {l for a in articles + pending for l in a.get("category_meta", {}).get("source_links", [])}
    if review:
        used_links |= review.rejected_links(output_dir)
    sent = _sent_log()
    for e in sent:
        used_links.update(e.get("links") or [])
    articles_all = articles + [p for p in pending if not p.get("live")]
    now = datetime.now(tz)
    # Ugyanarról az ügyről FOLLOWUP_MIN_H órán belül nem írunk újra; utána egy új fejlemény már „folytatás” lehet
    # (a korábbi cikkek előzményként mennek, és a cikkek egy ügyfolyamba kapcsolódnak).
    followup_h = float(os.getenv("FOLLOWUP_MIN_H", "10"))
    recent_titles = [a.get("title", "") for a in articles_all
                     if (a.get("created_at") or "") >= (now - timedelta(hours=followup_h)).isoformat() and a.get("title")]
    recent_kw = [_keywords(a.get("title", "") + " " + " ".join(s.get("title", "") for s in a.get("sources", [])))
                 for a in articles_all if (a.get("created_at") or "") >= (now - timedelta(hours=followup_h)).isoformat()]
    # a már elküldött (de a függő listából esetleg kiesett) témák is számítanak
    # elvetett témát (forráslinkje a tiltólistán) a szokásos idő után más forrásból újra megírhat – kivéve a bulvárt (36 óra)
    rej = review.rejected_links(output_dir) if review else set()
    fcut = (datetime.now(timezone.utc) - timedelta(hours=followup_h)).isoformat()
    rej_free = lambda e: bool(set(e.get("links") or []) & rej) and e.get("cat") != "bulvar" and (e.get("t") or "") < fcut
    recent_sent = [e for e in sent if (e.get("t") or "") >= fcut
                   or (set(e.get("links") or []) & rej and e.get("cat") == "bulvar")]
    recent_titles += [e["title"] for e in recent_sent if e.get("title")]
    recent_kw += [set(e.get("kw") or []) for e in recent_sent]
    wanted = [x.strip() for x in os.getenv("SECTION_IDS", ",".join(ACTIVE_SECTIONS)).split(",") if x.strip() in ACTIVE_SECTIONS]
    max_run = int(os.getenv("MAX_ARTICLES_PER_RUN", "2"))
    # Napi keret (ingyenes AI-kvóta + Cloudflare-buildek): a napi cikkszám nem lépheti túl a DAILY_ARTICLE_LIMIT-et,
    # és a keret egyenletesen oszlik el a nap futásai között (ne fogyjon el délelőtt).
    daily_limit = int(os.getenv("DAILY_ARTICLE_LIMIT", "12"))
    made_today = sum(1 for a in articles_all if a.get("date") == d.isoformat() and a.get("category") in SECTIONS)
    runs_left = int(os.getenv("SLOTS_LEFT") or max(1, (22 - now.hour) // 2 + 1))  # hátralévő körök / kétórás futások ma
    max_run = max(0, min(max_run, daily_limit - made_today, -(-(daily_limit - made_today) // runs_left)))
    if max_run == 0:
        log.info("A mai cikkkeret (%d) elfogyott – ebben a futásban nincs új cikk.", daily_limit)
        return 0
    min_score = float(os.getenv("MIN_HOT_SCORE", "5"))
    pause = int(os.getenv("AI_PAUSE_SECONDS", "8"))
    recent_imgs = used_images(articles + pending)
    cands = []
    space_today = sum(1 for a in articles_all if a.get("date") == d.isoformat() and SPACE_STORY.search(a.get("title", "")))
    for sid in wanted:
        today = sum(1 for a in articles_all if a.get("category") == sid and a.get("date") == d.isoformat())
        for group in pick_story(SECTIONS[sid], used_links, now, ai.cfg.http_timeout):
            kw = group[0]["kw"]
            # ugyanarról a témáról nem zárjuk ki automatikusan: a lenti AI-ellenőrzés dönt – új fejleményről
            # (pl. folyamatban lévő botrány új nyilatkozata, döntése) írunk, ugyanannak a hírnek az újramondásáról nem
            group[0]["near_dup"] = any(len(kw & rk) >= 4 for rk in recent_kw)
            # nincs „minden rovatba kell egy” kényszer (gyenge töltelékcikk); jó témából annyi jöhet, amennyi van (bulvár max. 6)
            cap = int(os.getenv(f"DAILY_MAX_{sid.upper()}", "6" if sid == "bulvar" else "99"))
            if today >= cap:
                continue
            head = group[0]["title"] + " " + (group[0].get("summary") or "")[:200]
            if SPACE_IMG_STORY.search(head):
                continue  # „új képek”-hír, a képek nélkül értelmetlen
            if SPACE_STORY.search(group[0]["title"]) and space_today >= int(os.getenv("DAILY_MAX_SPACE", "1")):
                continue  # űrkutatásból naponta legfeljebb 1 (túl volt tolva)
            cands.append((group[0]["hot_score"], sid, group))
    cands.sort(key=lambda x: -x[0])
    made, per_section, made_public = 0, {}, 0
    for score, sid, group in cands:
        if made >= max_run:
            break
        if score < min_score or per_section.get(sid):
            continue
        if same_story_as_recent(ai, group, recent_titles):
            continue  # más rovatban / más címmel már megírtuk ugyanezt az ügyet
        art = build_section_article(ai, SECTIONS[sid], d, tz, group, recent_imgs, related_past(articles, group))
        if not art:
            continue
        dup = _same_images(art, [e for e in sent if not rej_free(e)])
        if dup and same_story_as_recent(ai, [{"title": art["title"], "summary": art.get("lead", "")}], [dup]):
            # ugyanazok a képtalálatok ÉS az AI szerint nincs új fejlemény → ugyanaz a hír még egyszer
            log.info("Kimarad (ugyanaz a hír, mint: %s): %s", dup, art["title"])
            used_links.update(art["category_meta"]["source_links"])
            continue
        if review and not dry_run:
            _log_sent(art)
            sent.append({"title": art["title"], "imgs": [im.get("url") for im in art.get("image_options") or [] if not im.get("generated")]})
        used_links.update(art["category_meta"]["source_links"])
        recent_kw.append(group[0]["kw"])
        recent_titles.append(art["title"])
        if art.get("hero_image"):
            recent_imgs |= img_keys(art["hero_image"])
        if review:
            # REVIEW_MODE=hybrid (alap): jóváhagyásra vár, de AUTO_PUBLISH_MIN perc után magától kikerül;
            # post: azonnal kikerül, utólagos ellenőrzéssel; pre: csak jóváhagyás után.
            if review.MODE == "post":
                art["status"] = "published"
                art["live"] = True
                articles.insert(0, {k: v for k, v in art.items()
                                    if k not in ("title_options", "image_options", "story", "live", "legal")})
                made_public += 1
            else:
                art["status"] = "pending"
            pending.append(art)
            if not dry_run and review.MODE != "post":
                # küldés + azonnali mentés zárral (a párhuzamos Telegram-figyelő ne írja felül), aztán gombnyomások
                review.add_pending(art)
                review.poll(output_dir, ai, tz)
            else:
                review.send_article(output_dir, art)
        else:
            for k in ("title_options", "image_options", "story"):
                art.pop(k, None)
            articles.insert(0, art)
        made += 1
        per_section[sid] = 1
        log.info("✔ [%s] \"%s\" (pont: %.1f, %s forrás, kép: %s)", sid, art["title"], score, len(art["sources"]),
                 (art["hero_image"] or {}).get("kind", "nincs"))
        time.sleep(pause)  # ingyenes AI-keret: ne fussunk bele a percenkénti limitbe
    if review and not dry_run and review.MODE == "post":
        review.save_pending(output_dir, pending)
    if review and not dry_run and os.getenv("CURRENT_SLOT"):
        send_pitches(review, cands, used_links, recent_kw)
    articles.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    if (made_public or (made and not review)) and not dry_run:
        write_json_atomic(path, {"schema_version": 1, "updated_at": datetime.now(tz).isoformat(timespec="seconds"),
                                 "articles": articles[:int(os.getenv("ARTICLES_ARCHIVE_LIMIT", "600"))]})
    log.info("Rovatcikkek: %d új (%d jelölt)", made, len(cands))
    return made


# ---------------------------------------------------------------------------
# Fő futtató
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kollektíva napi tartalomgenerátor")
    p.add_argument("--date", help="Cél dátum ÉÉÉÉ-HH-NN (alapból a mai nap a site időzónájában)")
    p.add_argument("--provider", choices=["auto", "anthropic", "gemini", "openai", "mock"], help="AI_PROVIDER felülírása")
    p.add_argument("--only", choices=["horoscope", "retro", "sections"], help="Csak az egyik modul futtatása")
    p.add_argument("--dry-run", action="store_true", help="Nem ír fájlt, csak a kimenetet mutatja")
    p.add_argument("--if-due", action="store_true",
                   help="Gyakori (5 perces) futáshoz: rovatcikkek csak SECTIONS_EVERY_MIN percenként (6–22 óra között), "
                        "horoszkóp/retro csak ha a mai még nincs meg")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """Belépési pont cronhoz / GitHub Actionshöz. 0 = siker (fallbackkel is), 1 = írási hiba."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    load_dotenv(BASE_DIR / ".env")
    if args.provider:
        os.environ["AI_PROVIDER"] = args.provider
    cfg = Config.from_env()
    tz = ZoneInfo(cfg.timezone)
    target = date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    log.info("Cél dátum: %s | kimenet: %s", target, cfg.output_dir)

    ai = AIClient(cfg)
    exit_code = 0
    wrote = False

    # --if-due: a robot 5 percenként fut (Telegram), de új rovatcikk csak kb. kétóránként készül
    runs_path = BASE_DIR / "data" / "robot.json"
    runs = read_json(runs_path, {})
    now = datetime.now(tz)
    sections_due = True
    if args.if_due:
        every = int(os.getenv("SECTIONS_EVERY_MIN", "110"))
        last = runs.get("last_sections")
        try:
            since = (now - datetime.fromisoformat(last)).total_seconds() / 60 if last else 1e9
        except ValueError:
            since = 1e9
        sections_due = since >= every and 6 <= now.hour <= 22
        # Jóváhagyási körök (REVIEW_SLOTS, pl. „07:00,11:30,16:00,20:00”): a rovatcikkek a körök előtt kb. 25 perccel
        # készülnek, egyszerre jönnek Telegramra – nem kell egész nap figyelni. Körök között csak a nagyon forró
        # (sok lap által hozott) hír jön azonnal (BREAKING_SCORE).
        slots = [x.strip() for x in os.getenv("REVIEW_SLOTS", "").split(",") if re.match(r"^\d{1,2}:\d{2}$", x.strip())]
        if slots:
            slot_id = ""
            for x in slots:
                t = now.replace(hour=int(x.split(":")[0]), minute=int(x.split(":")[1]), second=0, microsecond=0)
                if t - timedelta(minutes=25) <= now < t + timedelta(minutes=75):
                    slot_id = f"{now.date().isoformat()} {x}"
            left = sum(1 for x in slots if now.replace(hour=int(x.split(":")[0]), minute=int(x.split(":")[1]))
                       + timedelta(minutes=75) > now)
            os.environ["SLOTS_LEFT"] = str(max(1, left))
            if slot_id and runs.get("last_slot") != slot_id:
                sections_due = True
                os.environ["MAX_ARTICLES_PER_RUN"] = os.getenv("ARTICLES_PER_SLOT", "4")
                runs["last_slot"] = slot_id
                write_json_atomic(runs_path, {**read_json(runs_path, {}), "last_slot": slot_id})
                log.info("Jóváhagyási kör: %s", slot_id)
            elif sections_due:  # körök között: csak rendkívüli hír
                os.environ["MIN_HOT_SCORE"] = os.getenv("BREAKING_SCORE", "16")
                os.environ["MAX_ARTICLES_PER_RUN"] = "1"
                slot_id = ""
            os.environ["CURRENT_SLOT"] = slot_id if sections_due else ""

    force = os.getenv("FORCE_REGENERATE", "false").lower() in ("1", "true", "yes")
    # Ami egyszer kikerült, az nem változik: a mai horoszkóp/retro cikk csak akkor készül, ha még nincs
    # (vagy ha csak AI nélküli tartalék-tartalom van). FORCE_REGENERATE=true felülírja.
    existing_h = read_json(cfg.output_dir / "horoscope.json", {})
    skip_h = (not force and existing_h.get("date") == target.isoformat()
              and (str(existing_h.get("source", "")).startswith("ai:") or not sections_due))
    if skip_h:
        log.info("A mai horoszkóp már kint van – nem generálom újra.")
    if args.only in (None, "horoscope") and not skip_h:
        try:
            horoscope = build_horoscope(ai, target, tz)
            if args.dry_run:
                print(json.dumps(horoscope, ensure_ascii=False, indent=2)[:3000])
            else:
                write_json_atomic(cfg.output_dir / "horoscope.json", horoscope)
                wrote = True
                log.info("✔ horoscope.json mentve (%s)", horoscope["source"])
        except OSError as e:
            log.exception("horoscope.json írása sikertelen: %s", e)
            exit_code = 1

    existing_r = read_json(cfg.output_dir / "retro_articles.json", {"articles": []}).get("articles", [])
    skip_r = not force and any(a.get("date") == target.isoformat() and a.get("status") == "published"
                               and (str(a.get("generator", "")).startswith("ai:") or not sections_due)
                               for a in existing_r)
    if skip_r:
        log.info("A mai retro cikk már kint van – nem generálom újra.")
    if args.only in (None, "retro") and not skip_r:
        try:
            article = build_retro_article(ai, target, tz, load_events(cfg.events_file))
            if article:
                path = cfg.output_dir / "retro_articles.json"
                archive = update_retro_archive(path, article, cfg.retro_archive_limit)
                if args.dry_run:
                    print(json.dumps(article, ensure_ascii=False, indent=2)[:3000])
                else:
                    write_json_atomic(path, archive)
                    wrote = True
                    log.info("✔ retro_articles.json mentve: \"%s\" (%s perc, %s, %s)",
                             article["title"], article["reading_time_min"], article["generator"], article["status"])
        except OSError as e:
            log.exception("retro_articles.json írása sikertelen: %s", e)
            exit_code = 1

    if args.only in (None, "sections") and sections_due:
        try:
            run_sections(ai, target, tz, cfg.output_dir, args.dry_run)
            wrote = True
        except OSError as e:
            log.exception("articles.json írása sikertelen: %s", e)
            exit_code = 1
        if args.if_due and not args.dry_run:
            write_json_atomic(runs_path, {**read_json(runs_path, {}), "last_sections": now.isoformat(timespec="seconds")})
        if os.getenv("CURRENT_SLOT") and not args.dry_run:
            try:  # összefoglaló a kör elején: mennyi vár, és egy gomb, ami mindet a chat aljára hozza
                import telegram_review as tr
                chat = tr.load_state().get("chat_id")
                waiting = [a for a in tr.load_pending() if not a.get("live")]
                if chat and waiting:
                    tr.tg("sendMessage", {"chat_id": chat, "text": f"🗂 Jóváhagyási kör ({os.getenv('CURRENT_SLOT')[-5:]}): "
                                          f"{len(waiting)} cikk vár rád. Magától egyik sem kerül ki.",
                                          })
            except Exception as e:  # noqa: BLE001
                log.warning("Kör-összefoglaló kimaradt: %s", e)
    elif args.if_due:
        log.info("Új rovatcikk most nem esedékes.")

    # Napi egy saját (off-topic) cikk a témalistából – Telegramra megy, csendes időszakban kerül ki
    if args.only in (None, "sections"):
        try:
            import offtopic
            offtopic.run(ai, target, tz, cfg.output_dir, args.dry_run)
            import polls
            polls.run(ai, target, tz, cfg.output_dir, args.dry_run)
            import quiz
            quiz.run(ai, target, tz, cfg.output_dir, args.dry_run)
            import videos
            videos.run(ai, target, tz, cfg.output_dir, args.dry_run)
        except Exception as e:  # noqa: BLE001 – a saját cikk hibája ne állítsa meg a robotot
            log.exception("Off-topic cikk kimaradt: %s", e)

    if not args.dry_run and (wrote or not args.if_due):
        try:
            build_static_site(cfg.output_dir, tz)
        except (OSError, KeyError, TypeError, ValueError) as e:
            log.exception("Statikus oldalak generálása sikertelen: %s", e)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
