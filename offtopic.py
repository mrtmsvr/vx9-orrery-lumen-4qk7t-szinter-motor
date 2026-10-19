#!/usr/bin/env python3
"""
Kollektíva – saját („off-topic”) magazincikkek
=============================================

A hírek (on-topic) mellett naponta egy saját, időtálló, mélyebb cikk készül egy előre összeállított témalistából
(data/offtopic_topics.json): életmód, univerzum, tech, kultúra, pénz. A cikk gerincét TUDOMÁNYOS TANULMÁNYOK
adják (Europe PMC: orvos- és élettudomány, OpenAlex: minden más – a legtöbbet idézett áttekintések/cikkek
összefoglalói), a Wikipédia csak háttér. A források a cikk alatt is megjelennek.
Ha egy saját cikket elvetsz Telegramon, aznap új témából új készül (max. OFFTOPIC_REROLLS, alap 2).

Időzítés: a cikk Telegramra megy (🗓 SAJÁT). Ha nem nyúlsz hozzá, CSENDES időszakban kerül ki: legkorábban
a generálás után OFFTOPIC_MIN_DELAY_H órával (alap 2), 9 és 21 óra között, amikor az elmúlt OFFTOPIC_QUIET_MIN
percben (alap 90) nem került ki hírcikk – legkésőbb OFFTOPIC_DEADLINE (alap 21:30) órakor. „✅ Kirakom” = azonnal.

Futás: a robot (kollektiva_content.py --if-due) naponta egyszer hívja, 7 és 20 óra között.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import calendar_hu as cal
import kollektiva_content as kc

log = logging.getLogger("kollektiva.offtopic")
TOPICS_FILE = kc.BASE_DIR / "data" / "offtopic_topics.json"
RUNS_FILE = kc.BASE_DIR / "data" / "robot.json"

OFFTOPIC_SYSTEM = (
    "Egy prémium magyar online magazin (Kollektíva) vezető szerzője vagy. Időtálló, alapos, mégis könnyen olvasható "
    "magazincikket írsz egy megadott témáról, elsősorban tudományos kutatások alapján: az idegen nyelvű, száraz "
    "tanulmányokat érthetővé és izgalmassá teszed a magyar olvasónak. SZIGORÚ SZABÁLY: a tényeket a megadott "
    "tanulmány-összefoglalókból, Wikipédia-kivonatokból és vitathatatlan, közismert tudásból veszed; nem találsz ki "
    "számot, tanulmányt, idézetet, nevet vagy dátumot, és nem állítasz többet, mint amit a tanulmány kimond. "
    "Ha valamiben a tudomány bizonytalan, azt kimondod. Egészségügyi témánál nem adsz személyre szabott tanácsot, "
    "és a végén egy mondatban jelzed, hogy a cikk nem helyettesíti az orvosi/szakértői véleményt. "
    "STÍLUS: természetes, élvezetes, újságírói magyar nyelv; változatos mondathossz; konkrét példák, számok, "
    "érdekességek; nincs töltelék, nincs ismétlés, nincsenek gépies fordulatok („fontos megjegyezni”, "
    "„összességében”, „nem csupán… hanem”, „ez rávilágít”). Nem a Wikipédiát foglalod össze szócikk-szerűen: "
    "az olvasó kérdéseire felelsz, saját szerkezettel. Csak érvényes JSON-t adsz vissza."
)


# ---------------------------------------------------------------------------
# Wikipédia
# ---------------------------------------------------------------------------

def wiki_search(query: str, lang: str, timeout: int) -> Optional[str]:
    url = (f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json&list=search&srlimit=1"
           f"&srsearch={urllib.parse.quote(query)}")
    data = kc.http_get_json(url, timeout) or {}
    hits = (data.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


def wiki_extract(title: str, lang: str, timeout: int, chars: int = 5000) -> Optional[dict]:
    """A szócikk sima szövege (bevezető + fejezetek) – ha a pontos cím nincs meg, kereséssel."""
    for t in (title, wiki_search(title, lang, timeout)):
        if not t:
            continue
        url = (f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json&prop=extracts|info&inprop=url"
               f"&explaintext=1&exsectionformat=plain&redirects=1&titles={urllib.parse.quote(t)}")
        data = kc.http_get_json(url, timeout) or {}
        for page in ((data.get("query") or {}).get("pages") or {}).values():
            text = re.sub(r"\n{3,}", "\n\n", page.get("extract") or "").strip()
            if len(text) > 400:
                return {"title": page.get("title", t), "url": page.get("fullurl") or
                        f"https://{lang}.wikipedia.org/wiki/{kc._q(page.get('title', t))}",
                        "lang": lang, "text": text[:chars]}
    return None


EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX_URL = "https://api.openalex.org/works"


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t or "")).strip()


def epmc_papers(q: str, timeout: int, n: int = 5) -> list:
    """Europe PMC: a témában legtöbbet idézett áttekintések / metaanalízisek (2012-től), összefoglalóval."""
    query = (f'TITLE:({q}) AND (PUB_TYPE:"review" OR PUB_TYPE:"systematic-review" OR PUB_TYPE:"meta-analysis") '
             "AND HAS_ABSTRACT:y AND LANG:eng AND PUB_YEAR:[2012 TO 2030]")
    data = kc.http_get_json(f"{EPMC_URL}?query={urllib.parse.quote(query)}&format=json&resultType=core"
                            f"&pageSize={n + 3}&sort={urllib.parse.quote('CITED desc')}", timeout) or {}
    out = []
    for r in ((data.get("resultList") or {}).get("result") or []):
        ab = _clean(r.get("abstractText"))
        if len(ab) < 400:
            continue
        url = f"https://doi.org/{r['doi']}" if r.get("doi") else f"https://europepmc.org/article/MED/{r.get('pmid')}"
        out.append({"title": _clean(r.get("title")).rstrip("."), "url": url, "lang": "en", "kind": "paper",
                    "journal": _clean((r.get("journalInfo") or {}).get("journal", {}).get("title")) or "Europe PMC",
                    "year": r.get("pubYear"), "cited": r.get("citedByCount") or 0, "text": ab[:2200]})
    return out[:n]


def openalex_papers(q: str, timeout: int, n: int = 5) -> list:
    """OpenAlex: a címben a keresett kifejezést tartalmazó, legtöbbet idézett tanulmányok (2010-től), összefoglalóval."""
    flt = f"title.search:{q},has_abstract:true,from_publication_date:2010-01-01,type:article|review"
    data = kc.http_get_json(f"{OPENALEX_URL}?filter={urllib.parse.quote(flt)}&sort=cited_by_count:desc&per-page={n + 3}"
                            "&select=title,publication_year,doi,id,cited_by_count,primary_location,abstract_inverted_index"
                            "&mailto=szerkesztoseg@xn--kollektva-m5a.hu", timeout) or {}
    out = []
    for r in data.get("results") or []:
        inv = r.get("abstract_inverted_index") or {}
        pos = sorted((i, w) for w, idx in inv.items() for i in idx)
        ab = _clean(" ".join(w for _, w in pos))
        if len(ab) < 400:
            continue
        src = ((r.get("primary_location") or {}).get("source") or {}).get("display_name") or "OpenAlex"
        out.append({"title": _clean(r.get("title")).rstrip("."), "url": r.get("doi") or r.get("id"), "lang": "en",
                    "kind": "paper", "journal": src, "year": r.get("publication_year"),
                    "cited": r.get("cited_by_count") or 0, "text": ab[:2200]})
    return out[:n]


def science_sources(topic: dict, timeout: int) -> list:
    sci = topic.get("science") or {}
    if not sci.get("q") or os.getenv("OFFTOPIC_SCIENCE", "true").lower() not in ("1", "true", "yes"):
        return []
    try:
        papers = (epmc_papers if sci.get("db") == "epmc" else openalex_papers)(sci["q"], timeout)
    except Exception as e:  # noqa: BLE001 – a tudományos forrás hiánya nem állíthatja meg a cikket
        log.warning("Tudományos források nem jöttek (%s): %s", topic.get("id"), e)
        return []
    log.info("Off-topic: %d tanulmány (%s)", len(papers), sci.get("db"))
    return papers


def gather(topic: dict, timeout: int) -> list:
    """Források egy témához: előbb a tudományos tanulmányok (ha van legalább 2, a Wikipédiából csak 1 magyar
    szócikk megy mellé háttérnek), különben a magyar, és ha kevés, az angol szócikkek. ScienceDaily-témánál
    a kiinduló kutatási hír is forrás."""
    papers = ([{"kind": "news", **topic["sd"]}] if topic.get("sd") else []) + science_sources(topic, timeout)
    if len(papers) >= 2:
        bg = next((ex for ex in (wiki_extract(t, "hu", timeout, 2500) for t in topic.get("wiki_hu", [])[:1]) if ex), None)
        return papers + ([bg] if bg else [])
    out = list(papers)
    for t in topic.get("wiki_hu", [])[:3]:
        ex = wiki_extract(t, "hu", timeout)
        if ex and ex["url"] not in {o["url"] for o in out}:
            out.append(ex)
    if sum(len(o["text"]) for o in out) < 4000:
        for t in topic.get("wiki_en", [])[:2]:
            ex = wiki_extract(t, "en", timeout, 6000)
            if ex and ex["url"] not in {o["url"] for o in out}:
                out.append(ex)
    return out


# ---------------------------------------------------------------------------
# Cikk
# ---------------------------------------------------------------------------

def offtopic_prompt(topic: dict, section: dict, sources: list, d: date) -> str:
    src = "\n\n".join(
        (f"[{i + 1}] TANULMÁNY – {s['title']} ({s['journal']}, {s['year']}; {s['cited']} hivatkozás) – összefoglaló:\n{s['text']}"
         if s.get("kind") == "paper" else
         f"[{i + 1}] ÚJ KUTATÁSI HÍR ({s.get('publisher', 'ScienceDaily')}) – a cikk kiindulópontja – {s['title']}\n{s['text']}"
         if s.get("kind") == "news" else f"[{i + 1}] Wikipédia ({s['lang']}) – {s['title']}\n{s['text']}")
        for i, s in enumerate(sources))
    sci = any(s.get("kind") == "paper" for s in sources)
    sci_rules = ("""
TUDOMÁNYOS ALAP: a cikk gerincét a fenti tanulmányok adják. Mondd el közérthetően, mit vizsgáltak és mit találtak
(pl. „egy 2019-es, több tucat kutatást összesítő áttekintés szerint…”, a folyóirat neve megemlíthető). Jelezd, ha
az eredmény bizonytalan, csak összefüggés, vagy állatkísérlet. Ne sorold fel a tanulmányokat egymás után: a
témát magyarázd, a kutatások a bizonyítékok. Számot csak a forrásból vegyél.
""" if sci else "")
    return f"""Rovat: {section['name']} ({section['focus']}). Dátum: {kc.hu_date(d)}.
A rovat hangja: {section.get('voice', 'természetes, újságírói')}.
Téma: {topic['topic']}
Szög / amire az olvasó kíváncsi: {topic.get('angle', '')}

Háttéranyag (csak ebből és közismert tudásból dolgozz; angol anyagot magyarul, saját szavaiddal használd):
{src}
{sci_rules}
Írj egy eredeti, időtálló magyar magazincikket (nem hír, hanem „olvasnivaló”):
- "title": RÖVID (max. 8 szó), kíváncsiságot keltő, de igaz cím (kérdés, meglepő tény vagy szám is lehet)
- "title_options": 2 további, eltérő stílusú címváltozat, tömbként
- "lead": 2 mondatos bevezető, ami behúzza az olvasót
- "key_points": 4–5 rövid, egymondatos pont a lényegről („Röviden” doboz)
- "body": bekezdések tömbje, {"1100–1500 szó (6–8 perc olvasás)" if topic.get("longform") else "700–1100 szó (4–6 perc olvasás)"}. 3–5 tematikus blokk, mindegyik első bekezdése
  „## ” kezdetű alcímsorral kezdődjön (pl. „## Miért pont a koffein?” – csak maga az alcím, címke nélkül). Legyen benne: miért érdekes/fontos, hogyan működik, konkrét számok és
  példák, gyakori tévhitek, és hogy mit jelent ez a hétköznapokban. Ahol illik, egy bekezdés lehet felsorolás
  („- ” kezdetű sorok). Ne ismételd a leadet, ne legyen „kerekítő” zárómondat.
- "tags": 3–5 rövid címke
- "image_query": 1–4 szavas ANGOL keresőkifejezés a Wikimedia Commonshoz (konkrét, fotózható tárgy/hely/jelenség)
- "image_query_alt": 2–4 további angol keresőkifejezés
- "image_generic": 1–2 szavas ANGOL, egyszerű hangulatkép-téma
- "inline_images": 1–2 szövegközi kép, ha a cikk konkrét, fotózható dolgot mutat be (tárgy, hely, jelenség, eszköz,
  személy), ami nem a főkép témája: {{"after": bekezdés sorszáma (0-tól), "query": pontos angol név a Wikimedia
  Commonshoz, "caption": rövid magyar képaláírás}}. Ha nincs ilyen, üres tömb.

Kizárólag ezt a JSON-t add vissza:
{{"title": "...", "title_options": ["...", "..."], "lead": "...", "key_points": ["..."], "body": ["..."], "tags": ["..."], "image_query": "...", "image_query_alt": ["..."], "image_generic": "...", "inline_images": []}}"""


def occasion_tag(topic: dict) -> str:
    """Ünnepi anyag címkéje (pl. „Advent”, „Mikulás”) – a cikk tetején kattintható, összegyűjti a témát."""
    occ = topic.get("occasion") or ""
    if not occ:
        return ""
    try:
        o = next(o for o in cal.occasions(int(occ.rsplit("-", 1)[1])) if o["id"] == occ)
    except (StopIteration, ValueError, IndexError):
        return ""
    name = o["name"].split(",")[0]
    return "Advent" if o["key"].startswith("advent") else name


def build_article(ai: "kc.AIClient", topic: dict, d: date, tz: ZoneInfo, avoid_images: Optional[set] = None,
                  sources: Optional[list] = None) -> Optional[dict]:
    section = kc.SECTIONS.get(topic.get("section")) or kc.SECTIONS["eletmod"]
    if section.get("legacy"):  # az Univerzum beolvadt a Tech & Tudományba
        section = kc.SECTIONS["tech"]
    sources = sources or gather(topic, ai.cfg.http_timeout)
    if not sources:
        log.warning("Off-topic: nincs forrásanyag ehhez: %s", topic.get("topic"))
        return None
    try:
        raw = ai.complete_json(OFFTOPIC_SYSTEM, offtopic_prompt(topic, section, sources, d), 8000 if topic.get("longform") else 6000)
        kc.validate_retro(raw)
        raw = kc.editorial_polish(ai, raw)
        raw = kc.critical_review(ai, raw, sources)
        art = kc.validate_retro(raw)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.error("Off-topic cikkírás sikertelen (%s): %s", topic.get("id"), e)
        return None
    words = len(re.findall(r"\w+", " ".join(art["body"])))
    if words < 450:
        log.warning("Off-topic: túl rövid lett (%d szó) – kimarad.", words)
        return None
    generic = raw.get("image_generic")
    image_options = kc.find_images([raw.get("image_query"), *(raw.get("image_query_alt") or []), *topic.get("images", [])][:6],
                                   (generic if isinstance(generic, list) else [generic])[:2], ai.cfg.http_timeout,
                                   avoid_images, limit=kc.IMAGE_OPTIONS)
    image_options = kc.drop_used(image_options, avoid_images)
    image_options = kc.vision_rank(ai, art["title"], art["lead"], image_options)
    if not image_options:  # nincs illő kép -> saját grafika
        image_options = kc.auto_illustration(ai, art, "sajat")
    image = image_options[0] if image_options else None
    inline_images = kc.find_inline_images(raw, len(art["body"]), ai.cfg.http_timeout,
                                          (avoid_images or set()) | {im["url"] for im in image_options})
    title_options = [art["title"]] + [str(t).strip() for t in (raw.get("title_options") or [])
                                      if str(t).strip() and str(t).strip() != art["title"]]
    now_iso = datetime.now(tz).isoformat(timespec="seconds")
    slug = kc.slugify(f"{d.isoformat()}-{art['title']}")
    srcs = kc.normalize_sources([
        {"url": s["url"], "title": s["title"], "publisher": f"{s['journal']}, {s['year']}"}
        if s.get("kind") == "paper" else
        {"url": s["url"], "title": s["title"], "publisher": s.get("publisher", "ScienceDaily")}
        if s.get("kind") == "news" else
        {"url": s["url"], "title": s["title"], "publisher": f"Wikipédia ({s['lang']})", "license": "CC BY-SA 4.0"}
        for s in sources], now_iso)
    full_text = " ".join([art["lead"], *art["body"]])
    sid = section["id"]
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kc.SITE_URL}/{sid}/{slug}")),
        "slug": slug, "status": "pending", "category": sid, "subcategory": "olvasnivalo",
        "tags": ([occasion_tag(topic)] if occasion_tag(topic) else []) + art["tags"][:5], "title": art["title"], "subtitle": None, "lead": art["lead"],
        "key_points": [str(k).strip() for k in (raw.get("key_points") or []) if str(k).strip()][:5],
        "content": kc.to_markdown(art), "content_format": "markdown", "body": art["body"], "pull_quote": None,
        "reading_time_min": kc.reading_time(full_text), "word_count": len(re.findall(r"\w+", full_text)),
        "locale": "hu-HU", "hero_image": image, "inline_images": inline_images, "sources": srcs,
        "authorship": {"mode": "ai_generated", "byline": "Kollektíva szerkesztőség", "model": ai.label.split(":", 2)[-1],
                       "prompt_version": "offtopic-v2-science", "reviewed_by": None, "reviewed_at": None},
        "hot_score": 0, "offtopic": True, "offtopic_topic": topic,
        "science_based": any(s.get("kind") in ("paper", "news") for s in sources),
        "title_options": title_options[:3], "image_options": image_options,
        "category_meta": {"source_links": [s["url"] for s in sources], "offtopic_id": topic.get("id")},
        "date": d.isoformat(), "date_label": f"{kc.HU_MONTHS[d.month - 1]} {d.day}.",
        "url": f"/{sid}/{slug}/",
        "seo": {"meta_title": art["title"][:60], "meta_description": art["lead"][:160],
                "canonical_url": f"{kc.SITE_URL}/{sid}/{slug}/", "og_image": image["url"] if image else None,
                "noindex": False, "schema_type": "Article"},
        "monetization": {"ads_enabled": True, "brand_safety": "safe", "sponsored": False,
                         "sponsor_name": None, "affiliate_links": False},
        "related_ids": [], "dedupe_hash": hashlib.sha256(f"offtopic|{topic.get('id')}".encode()).hexdigest(),
        "pipeline_run_id": os.getenv("GITHUB_RUN_ID"), "generator": ai.label,
        "created_at": now_iso, "updated_at": now_iso, "published_at": None, "expires_at": None,
    }


# ---------------------------------------------------------------------------
# Időzítés
# ---------------------------------------------------------------------------

def make_schedule(now: datetime) -> dict:
    earliest = now + timedelta(hours=float(os.getenv("OFFTOPIC_MIN_DELAY_H", "2")))
    hh, mm = (int(x) for x in os.getenv("OFFTOPIC_DEADLINE", "21:30").split(":"))
    deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if deadline <= earliest:
        deadline = earliest + timedelta(hours=1)
    return {"earliest": earliest.isoformat(timespec="seconds"), "deadline": deadline.isoformat(timespec="seconds")}


def is_due(art: dict, articles: list, now: datetime) -> bool:
    """Kirakható-e most az időzített saját cikk: legkorábbi időpont után, 9–21 óra között, csendes időszakban
    (az elmúlt OFFTOPIC_QUIET_MIN percben nem került ki hírcikk) – vagy ha elérte a határidőt."""
    sch = art.get("schedule") or {}
    try:
        earliest, deadline = datetime.fromisoformat(sch["earliest"]), datetime.fromisoformat(sch["deadline"])
    except (KeyError, TypeError, ValueError):
        return True
    if now >= deadline:
        return True
    if now < earliest or not 9 <= now.hour <= 21:
        return False
    quiet = timedelta(minutes=int(os.getenv("OFFTOPIC_QUIET_MIN", "90")))
    for a in articles:
        if a.get("offtopic") or a.get("category") not in kc.SECTIONS:
            continue
        try:
            if now - datetime.fromisoformat(a.get("published_at") or a.get("created_at")) < quiet:
                return False
        except (TypeError, ValueError):
            continue
    return True


# ---------------------------------------------------------------------------
# Napi futás
# ---------------------------------------------------------------------------

TOPIC_GEN_SYSTEM = ("Egy magyar online magazin főszerkesztője vagy. Időtálló, kattintásra érdemes „olvasnivaló” "
                    "témákat tervezel. Csak JSON-t adsz vissza.")


def refill_topics(ai: "kc.AIClient", topics: list, n: int = 12) -> list:
    """Ha kevés téma maradt, az AI újakat tervez (rovatonként vegyesen, ajánlókkal), és a listához fűzi őket."""
    if not ai or not ai.enabled:
        return []
    have = "; ".join(t.get("topic", "") for t in topics[-60:] if not t.get("occasion"))
    prompt = (f"Eddigi témáink (ezeket NE ismételd): {have}\n\nTervezz {n} új, időtálló témát a következő rovatokba vegyesen: "
              "eletmod (egészség, mozgás, alvás, pszichológia, kapcsolatok – NE megint táplálék-kiegészítő vagy diéta), "
              "tech (hétköznapi technológia, tudomány, fizika – űrkutatás legfeljebb 1), penzvilag "
              "(személyes pénzügyek, gazdaság), kultura (köztük 3–4 film-, sorozat- vagy könyvajánló, ill. „könyv röviden”). "
              "Legyen VÁLTOZATOS a forma is: magyarázó, tévhit-romboló, történet („hogyan lett…”), gyakorlati útmutató, "
              "ajánló; magyar vonatkozás (történelem, találmány, hely, személy) ahol lehet; két hasonló téma ne legyen. "
              "Elemek: {\"id\": \"rovid-kotojeles-azonosito\", \"section\": \"rovat\", \"topic\": \"magyar cím-ötlet\", "
              "\"angle\": \"mire kíváncsi az olvasó\", \"wiki_hu\": [\"magyar Wikipédia-szócikk címe\"], "
              "\"wiki_en\": [\"angol szócikk címe\"], \"images\": [\"angol képkereső kifejezés\"], "
              "\"science\": {\"db\": \"epmc\" (orvosi/élettudomány) vagy \"openalex\" (minden más), \"q\": \"rövid angol keresőkifejezés\"} "
              "– ajánlóknál a science legyen null}. JSON: {\"topics\": [...]}")
    try:
        raw = ai.complete_json(TOPIC_GEN_SYSTEM, prompt, 5000)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Új témák tervezése sikertelen: %s", e)
        return []
    ids = {t.get("id") for t in topics}
    new = []
    for t in raw.get("topics") or []:
        if not isinstance(t, dict) or not t.get("topic") or t.get("section") not in kc.SECTIONS:
            continue
        t["id"] = kc.slugify(str(t.get("id") or t["topic"]))[:40]
        if t["id"] in ids:
            continue
        if not isinstance(t.get("science"), dict):
            t.pop("science", None)
        t["auto"] = True
        ids.add(t["id"])
        new.append(t)
    if new:
        _write_topics(topics + new)
        log.info("Off-topic: %d új témát terveztem.", len(new))
    return new


SEASON_SYSTEM = ("Egy magyar online magazin főszerkesztője vagy. Az év jeles napjaihoz, ünnepköreihez tervezel "
                 "olvasnivalót, amire az emberek ilyenkor tényleg rákeresnek. Csak JSON-t adsz vissza.")


def _write_topics(all_t: list) -> None:
    lines = ['{"topics": ['] + [json.dumps(x, ensure_ascii=False) + (',' if i < len(all_t) - 1 else '')
                                 for i, x in enumerate(all_t)] + [']}']
    TOPICS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def plan_seasonal(ai: Optional["kc.AIClient"], d: date, topics: list) -> list:
    """Az év körforgása: ha egy jeles nap belép az előkészítési ablakba (nagy ünnep: 3 hét, kisebb: 10 nap), az AI
    tematikus témákat tervez hozzá (ajánló, ajándékötlet, eredettörténet, „X dolog, amit nem tudtál”, gyakorlati
    tipp), a megfelelő napokra időzítve. Jeles naponként egyszer fut."""
    if not ai or not ai.enabled:
        return []
    have = {t.get("occasion") for t in topics if t.get("occasion")}
    new = []
    for o in cal.upcoming(d, 25):
        if o["id"] in have or o["days"] > cal.lead_days(o):
            continue
        n = 4 if o["major"] else 2
        prompt = (f"Mai dátum: {d.isoformat()}. {cal.where_are_we(d)}\n\nJeles nap: {o['name']} ({o['date'].isoformat()}).\n"
                  f"Tervezz hozzá {n} cikktémát, változatosan ezekből: ajánló (film, könyv, program), ajándék- vagy "
                  "receptötletek, eredettörténet / hagyomány, „X dolog, amit nem tudtál róla”, gyakorlati tippek "
                  "(„így kerüld el…”, „így készülj…”). Csak olyat, ami tényekre épülhet (Wikipédia, tudomány), kitalált "
                  "termék vagy ár nélkül. Rovat: kultura (hagyomány, eredet, ajánló, program), penzvilag (ajándék, vásárlás, "
                  "spórolás), eletmod (CSAK egészség, étkezés, sport, alvás, lelki egészség – ajándék, divat NEM ide), tech "
                  "(tudomány, csillagászat). "
                  'Elemek: {"id": "rovid-kotojeles-azonosito", "section": "rovat", "topic": "magyar cím-ötlet", '
                  '"angle": "mire kíváncsi az olvasó", "when": "elotte" (készülődés, ötletek – a napok előtte) vagy '
                  '"napjan" (eredet, hagyomány, köszöntő – aznap), "wiki_hu": ["magyar Wikipédia-szócikk"], '
                  '"wiki_en": ["angol szócikk"], "images": ["angol képkereső kifejezés"], '
                  '"science": {"db": "epmc" vagy "openalex", "q": "rövid angol keresés"} vagy null}. JSON: {"topics": [...]}')
        try:
            raw = ai.complete_json(SEASON_SYSTEM, prompt, 3000)
        except (kc.AIError, ValueError, TypeError, KeyError) as e:
            log.warning("Ünnepi témák tervezése sikertelen (%s): %s", o["id"], e)
            continue
        ids = {t.get("id") for t in topics + new}
        for t in raw.get("topics") or []:
            if not isinstance(t, dict) or not t.get("topic") or t.get("section") not in kc.SECTIONS:
                continue
            t["id"] = kc.slugify(f"{o['key']}-{t.get('id') or t['topic']}")[:48] + f"-{o['date'].year}"
            if t["id"] in ids:
                continue
            on_day = str(t.pop("when", "")).startswith("nap")
            # nagy ünnep (karácsony, húsvét…): a készülődős anyag hetekkel előtte is jó; kisebb, egynapos jeles
            # napnál (pl. október 6.) csak előző nap vagy aznap van értelme – akkor keresnek rá
            early = cal.lead_days(o) if o["major"] else 1
            t.update({"occasion": o["id"], "auto": True,
                      "publish_from": (o["date"] if on_day else o["date"] - timedelta(days=early)).isoformat(),
                      "publish_by": (o["date"] + timedelta(days=1 if on_day else 0)).isoformat()})
            if not isinstance(t.get("science"), dict):
                t.pop("science", None)
            ids.add(t["id"])
            new.append(t)
        if not any(t.get("occasion") == o["id"] for t in new):  # ne próbálja minden futásnál újra
            new.append({"id": f"{o['id']}-ures", "occasion": o["id"], "topic": "", "section": "", "skip": True})
    if new:
        _write_topics(topics + new)
        log.info("Év körforgása: %d ünnepi téma (%s).", len([t for t in new if not t.get("skip")]),
                 ", ".join(sorted({t["occasion"] for t in new})))
    return new


def next_seasonal(done: list, ai: Optional["kc.AIClient"], d: date) -> Optional[dict]:
    """A ma esedékes ünnepi téma (ha van): amelyiknek az ablaka ma nyitva, a leghamarabb lejáró előre."""
    topics = kc.read_json(TOPICS_FILE, {"topics": []}).get("topics", [])
    topics += plan_seasonal(ai, d, topics)
    today = d.isoformat()
    cands = [t for t in topics if t.get("occasion") and not t.get("skip") and t.get("id") not in set(done)
             and t.get("publish_from", "") <= today <= t.get("publish_by", "")]
    return sorted(cands, key=lambda t: t["publish_by"])[0] if cands else None


def next_topic(done: list, ai: Optional["kc.AIClient"] = None) -> Optional[dict]:
    topics = kc.read_json(TOPICS_FILE, {"topics": []}).get("topics", [])
    fresh = [t for t in topics if t.get("id") not in set(done) and not t.get("occasion")]
    if len(fresh) < 5:  # fogyóban a lista → az AI újakat tervez
        fresh += refill_topics(ai, topics)
    if not fresh:
        return None
    # rovatok váltakozzanak: amelyikből legrégebben volt, az jön
    last_sec = {}
    for i, tid in enumerate(done):
        t = next((x for x in topics if x.get("id") == tid), None)
        if t:
            last_sec[t.get("section")] = i
    fresh.sort(key=lambda t: last_sec.get(t.get("section"), -1))
    return fresh[0]


HOOK_SYSTEM = ("Egy magyar online magazin főszerkesztője vagy. A heti hírekből olyan időtálló háttércikk-témát választasz, "
               "ami megmagyarázza a hírek mögötti fogalmat, intézményt, történetet vagy tudományt. Csak JSON-t adsz vissza.")


def hook_topic(ai: "kc.AIClient", articles: list, done: list, d: date) -> Optional[dict]:
    """A hét híreihez kötött háttéranyag (pl. euró-bevezetés hírére: „Mi kell az euróhoz?”) – ezt keresik az olvasók."""
    if not ai or not ai.enabled:
        return None
    since = (d - timedelta(days=4)).isoformat()
    heads = [a.get("title", "") for a in articles if (a.get("date") or "") >= since and not a.get("offtopic")][:40]
    if len(heads) < 5:
        return None
    topics = kc.read_json(TOPICS_FILE, {"topics": []}).get("topics", [])
    have = "; ".join(t.get("topic", "") for t in topics if t.get("id") in set(done))[-1500:]
    prompt = ("A hét címei nálunk:\n" + "\n".join("- " + h for h in heads)
              + f"\n\nKorábbi saját témáink (NE ismételd): {have}\n\nVálassz EGY témát, ami a fenti hírek közül egy fontoshoz "
              "kapcsolódó, időtálló háttér-magyarázat (fogalom, intézmény, történelmi előzmény, tudományos háttér) – NEM a hír "
              "újramondása, és nem pártpolitikai állásfoglalás. Rovat: kozelet, penzvilag, tech, eletmod vagy kultura. "
              'JSON: {"id": "rovid-kotojeles-azonosito", "section": "rovat", "topic": "magyar cím-ötlet", "angle": "mire kíváncsi az olvasó", '
              '"wiki_hu": ["magyar Wikipédia-szócikk"], "wiki_en": ["angol szócikk"], "images": ["angol képkereső kifejezés"], '
              '"science": {"db": "epmc" vagy "openalex", "q": "rövid angol keresés"} vagy null}')
    try:
        t = ai.complete_json(HOOK_SYSTEM, prompt, 1200)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Hírhez kötött téma kimaradt: %s", e)
        return None
    if not isinstance(t, dict) or not t.get("topic") or t.get("section") not in kc.SECTIONS:
        return None
    t["id"] = "hir-" + kc.slugify(str(t.get("id") or t["topic"]))[:40]
    if t["id"] in set(done):
        return None
    if not isinstance(t.get("science"), dict):
        t.pop("science", None)
    t["hook"] = True
    _write_topics(topics + [t])
    return t


SD_FEEDS = ["https://www.sciencedaily.com/rss/top/health.xml",
            "https://www.sciencedaily.com/rss/health_medicine/fitness.xml",
            "https://www.sciencedaily.com/rss/health_medicine/nutrition.xml",
            "https://www.sciencedaily.com/rss/mind_brain.xml"]
SD_SYSTEM = ("Egy magyar online magazin főszerkesztője vagy. Friss kutatási hírekből választod ki azt, amelyikből a legtöbb "
             "magyar olvasó kattintana egy alapos, magyarázó cikkre. Csak JSON-t adsz vissza.")


def science_topic(ai: Optional["kc.AIClient"], done: list, d: date) -> Optional[dict]:
    """Heti 3 (SCIENCE_DAYS, alap hétfő/szerda/péntek) hosszabb saját cikk egy friss ScienceDaily-kutatásból: az AI a
    legkattinthatóbbat választja (egészség, fogyás, alvás, edzés, étel, öregedés, agy – nem sejtszintű/állatos
    szakhír), a cikk gerincét a témában talált további tanulmányok adják. Nem forró hír: időzítve, csendes sávban megy ki."""
    if not ai or not ai.enabled:
        return None
    days = {int(x) for x in os.getenv("SCIENCE_DAYS", "0,2,4").split(",") if x.strip().isdigit()}
    if d.weekday() not in days:
        return None
    now = datetime.now().astimezone()
    items, seen = [], set()
    for u in SD_FEEDS:
        for it in kc.fetch_feed(u, ai.cfg.http_timeout):
            tid = "sd-" + hashlib.sha1(it["link"].encode()).hexdigest()[:10]
            if it["link"] in seen or tid in set(done):
                continue
            if it.get("published") and (now - it["published"]).days > 14:
                continue
            seen.add(it["link"])
            items.append((tid, it))
    items = items[:30]
    if not items:
        return None
    lst = "\n".join(f"{i + 1}. {it['title']} – {(it.get('summary') or '')[:220]}" for i, (_, it) in enumerate(items))
    prompt = (f"Friss kutatási hírek:\n{lst}\n\nVálaszd ki azt az EGYET, amelyik a legtöbb átlagos magyar olvasót érdekli "
              "(gyakorlati haszon: egészség, testsúly, alvás, edzés, étkezés, öregedés, agy/lélek, gyereknevelés; meglepő, "
              "hétköznapi tanulsággal). Kerüld a szűk szakmai, sejtszintű, állatkísérletes vagy gyógyszercéges híreket. "
              'JSON: {"n": sorszám, "topic": "magyar cím-ötlet", "angle": "mire kíváncsi az olvasó, mit tanul belőle", '
              '"wiki_hu": ["magyar Wikipédia-szócikk"], "wiki_en": ["angol szócikk"], "images": ["angol képkereső kifejezés"], '
              '"science": {"db": "epmc", "q": "rövid angol keresés a témához (további tanulmányokhoz)"}}')
    try:
        t = ai.complete_json(SD_SYSTEM, prompt, 1000, light=True)
        n = int(t.get("n", 0)) - 1
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.warning("ScienceDaily-téma kimaradt: %s", e)
        return None
    if not 0 <= n < len(items) or not t.get("topic"):
        return None
    tid, it = items[n]
    topic = {"id": tid, "section": "eletmod", "topic": str(t["topic"])[:120], "angle": str(t.get("angle") or "")[:300],
             "wiki_hu": [str(x) for x in t.get("wiki_hu") or []][:2], "wiki_en": [str(x) for x in t.get("wiki_en") or []][:2],
             "images": [str(x) for x in t.get("images") or []][:3], "longform": True,
             "sd": {"title": it["title"], "url": it["link"], "text": (it.get("summary") or "")[:1500], "publisher": "ScienceDaily"}}
    if isinstance(t.get("science"), dict) and t["science"].get("q"):
        topic["science"] = {"db": "epmc", "q": str(t["science"]["q"])[:80]}
    topics = kc.read_json(TOPICS_FILE, {"topics": []}).get("topics", [])
    _write_topics(topics + [topic])
    log.info("ScienceDaily-téma: %s (%s)", topic["topic"], it["title"][:80])
    return topic


def allow_reroll(d: date) -> bool:
    """Elvetett saját cikk után aznap új készülhet (max. OFFTOPIC_REROLLS alkalommal)."""
    runs = kc.read_json(RUNS_FILE, {})
    used = runs.get("offtopic_rerolls", {}).get(d.isoformat(), 0)
    if used >= int(os.getenv("OFFTOPIC_REROLLS", "2")):
        return False
    runs.pop("last_offtopic_date", None)
    runs["offtopic_rerolls"] = {d.isoformat(): used + 1}
    kc.write_json_atomic(RUNS_FILE, runs)
    return True


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False) -> int:
    if os.getenv("OFFTOPIC_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    runs = kc.read_json(RUNS_FILE, {})
    if not 7 <= now.hour <= 20:
        return 0
    done = runs.get("offtopic_done", [])
    slot = "last_offtopic_date"
    if runs.get("last_offtopic_date") == d.isoformat():
        # a napi időtálló anyag már megvolt → ha van esedékes ünnepi téma, az jön (naponta egy)
        if runs.get("last_seasonal_date") == d.isoformat():
            return 0
        topic, slot = next_seasonal(done, ai, d), "last_seasonal_date"
        if not topic:
            return 0
    else:
        topic = science_topic(ai, done, d)  # hétfő/szerda/péntek: hosszabb, kutatásalapú életmódcikk
        if not topic and d.toordinal() % 2 == 0 and os.getenv("OFFTOPIC_HOOK", "true").lower() in ("1", "true", "yes"):
            topic = hook_topic(ai, kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", []), done, d)
        topic = topic or next_topic(done, ai)
    if not topic:
        log.info("Off-topic: elfogyott a témalista.")
        return 0
    articles = kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", [])
    avoid = kc.used_images(articles)
    art = build_article(ai, topic, d, tz, avoid)
    # a témát akkor is lezárjuk, ha nem sikerült (ne próbálkozzon vele minden futásnál)
    if not dry_run:
        kc.write_json_atomic(RUNS_FILE, {**kc.read_json(RUNS_FILE, {}), slot: d.isoformat(),
                                         "offtopic_done": done + [topic["id"]]})
    if not art:
        return 0
    log.info("✔ [off-topic/%s] \"%s\" (%d szó, kép: %s)", art["category"], art["title"], art["word_count"],
             (art["hero_image"] or {}).get("kind", "nincs"))
    if dry_run:
        print(json.dumps({k: art[k] for k in ("title", "title_options", "lead", "key_points", "body")},
                         ensure_ascii=False, indent=2)[:4000])
        return 1
    art["schedule"] = make_schedule(now)
    review = None
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        import telegram_review as review
        if not review.enabled(output_dir):
            review = None
    if review:
        review.add_pending(art)
    else:  # Telegram nélkül azonnal kikerül
        art.update({"status": "published", "published_at": art["created_at"]})
        for k in ("title_options", "image_options", "offtopic_topic", "schedule", "legal"):
            art.pop(k, None)
        path = output_dir / "articles.json"
        data = kc.read_json(path, {"articles": []})
        kc.write_json_atomic(path, {**data, "articles": [art] + data.get("articles", [])})
    return 1


if __name__ == "__main__":  # kézi próba: python offtopic.py [--dry-run] [--topic ID]
    import argparse
    import sys
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--topic")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    kc.load_dotenv(kc.BASE_DIR / ".env")
    cfg = kc.Config.from_env()
    tz = ZoneInfo(cfg.timezone)
    ai = kc.AIClient(cfg)
    if args.topic:
        t = next((x for x in kc.read_json(TOPICS_FILE, {"topics": []})["topics"] if x["id"] == args.topic), None)
        a = build_article(ai, t, datetime.now(tz).date(), tz) if t else None
        print(json.dumps({k: a[k] for k in ("title", "lead", "body", "sources")}, ensure_ascii=False, indent=2) if a else "nincs")
        sys.exit(0)
    sys.exit(0 if run(ai, datetime.now(tz).date(), tz, cfg.output_dir, args.dry_run) >= 0 else 1)
