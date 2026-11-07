#!/usr/bin/env python3
"""
Kollektíva – videó- és podcastfigyelő
=====================================

Figyeli a megadott YouTube-csatornák új videóit (a csatornák ingyenes RSS-éből), a Gemini „megnézi” a videót
(hang + kevés képkocka), és részletes, idézetekkel ellátott magyar összefoglalót ad. Ebből a szokásos cikkíró
folyamat (lektor, tényellenőrzés, idézetellenőrzés, képek) cikket ír, a cikkbe beágyazzuk az eredeti videót
(hivatalos YouTube-lejátszó – ez jogszerű, és a forrásnak is jó). A cikk Telegramra megy, és CSAK jóváhagyással kerül ki.

Csatornák: data/video_sources.json (Telegramon: /csatorna <YouTube-link vagy @név>, /csatornak).
Napi keret: nincs (VIDEO_DAILY_MAX-szal korlátozható), futásonként legfeljebb 1 videó; óránként egyszer néz rá a csatornákra.
Csak a VIDEO_MIN_MINUTES-nél (alap 20 perc) hosszabb videók – csatornánként felülírható ("min_minutes"),
pl. politikusok rövid bejelentéseihez kisebb érték; a shortsok, előzetesek kimaradnak, a többiről a Gemini dönti el,
hogy van-e benne hír.
"""
from __future__ import annotations

import json
import html
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.videos")
SOURCES_FILE = kc.BASE_DIR / "data" / "video_sources.json"
STATE_FILE = kc.BASE_DIR / "data" / "video_state.json"
DEFAULT_SOURCES = [
    {"name": "Partizán", "channel_id": "UCEFpEvuosfPGlV1VyUF6QOA"},
    {"name": "Telex", "handle": "Telexponthu", "channel_id": "UCM-1sd-cXSuCsfWp8QMY_OQ"},
    {"name": "444", "handle": "negynegynegy", "channel_id": "UCGoLa-QhHmTxLEdjv_8dxrg"},
    {"name": "Szélsőközép", "handle": "szelsokozepprodukcio", "channel_id": "UCHdkcNjZq3CpIvMdWX8maMg"},
]
# ismert csatornaazonosítók (a GitHub-szerverről a YouTube néha nem adja ki a csatornaoldalt)
KNOWN_IDS = {"telexponthu": "UCM-1sd-cXSuCsfWp8QMY_OQ", "444hu": "UCGoLa-QhHmTxLEdjv_8dxrg",
             "negynegynegy": "UCGoLa-QhHmTxLEdjv_8dxrg", "szelsokozepprodukcio": "UCHdkcNjZq3CpIvMdWX8maMg"}
# a YouTube a bot-azonosítót és a süti nélküli kérést gyakran a hozzájárulási oldalra irányítja → böngésző-fejléc + hozzájárulás-süti
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.5", "Cookie": "SOCS=CAI; CONSENT=YES+1"}
SKIP = re.compile(r"#shorts|\bshorts?\b|előzetes|trailer|élő adás indul|premier|stream starts|reklám", re.I)

VIDEO_SYSTEM = (
    "Egy magyar online magazin (Kollektíva) hírszerkesztője vagy. Megnézel egy videót/podcastot, és a cikkíróknak "
    "pontos, tényszerű anyagot készítesz belőle. SZABÁLYOK: csak azt írd le, ami a videóban tényleg elhangzik vagy "
    "látszik; ne egészítsd ki saját tudással. Szó szerinti idézetet csak akkor adj, ha biztosan érthető volt; ha egy "
    "név, szám vagy mondat félrehallható/bizonytalan, azt tedd az „uncertain” listába, és a szövegben ne állítsd "
    "biztosnak. Mindig írd oda, KI mondja (név, szerep). Csak érvényes JSON-t adsz vissza."
)


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(600_000).decode("utf-8", "ignore")


def load_sources() -> list:
    data = kc.read_json(SOURCES_FILE, {"sources": DEFAULT_SOURCES})
    src = data.get("sources", DEFAULT_SOURCES)
    removed = {str(x).lower() for x in data.get("removed", [])}
    for x in src:  # régi bejegyzés azonosító nélkül → ismert azonosító
        if not x.get("channel_id") and str(x.get("handle", "")).lower() in KNOWN_IDS:
            x["channel_id"] = KNOWN_IDS[str(x["handle"]).lower()]
    have = {x.get("channel_id") for x in src}
    for d in DEFAULT_SOURCES:  # később felvett alapcsatornák (ha nem törölted őket)
        if d["channel_id"] not in have and d["name"].lower() not in removed:
            src.append(dict(d))
    return src


def save_sources(src: list, removed: Optional[list] = None) -> None:
    data = kc.read_json(SOURCES_FILE, {})
    rem = list(dict.fromkeys((data.get("removed") or []) + (removed or [])))
    kc.write_json_atomic(SOURCES_FILE, {"sources": src, "removed": rem})


def resolve_channel(src: dict) -> Optional[str]:
    """@név -> csatornaazonosító (UC…), a csatornaoldalból; az eredményt eltároljuk."""
    if src.get("channel_id"):
        return src["channel_id"]
    if src.get("video"):
        try:
            page = _get(f"https://www.youtube.com/watch?v={src['video']}")
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        m = re.search(r'"channelId":"(UC[\w-]{22})"', page)
        n = re.search(r'"ownerChannelName":"([^"]+)"', page)
        if m:
            src.update({"channel_id": m.group(1), "name": n.group(1) if n else m.group(1)})
            src.pop("video", None)
            return m.group(1)
        return None
    h = str(src.get("handle") or "").lstrip("@")
    if not h:
        return None
    if h.lower() in KNOWN_IDS:
        src["channel_id"] = KNOWN_IDS[h.lower()]
        return src["channel_id"]
    try:
        page = _get(f"https://www.youtube.com/@{urllib.parse.quote(h)}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Csatorna nem érhető el (@%s): %s", h, e)
        return None
    m = (re.search(r'"externalId":"(UC[\w-]{22})"', page) or re.search(r'"channelId":"(UC[\w-]{22})"', page)
         or re.search(r"/channel/(UC[\w-]{22})", page))
    if not m:  # tartalék: YouTube-keresés csatornára
        try:
            res = _get(f"https://www.youtube.com/results?search_query={urllib.parse.quote(h)}&sp=EgIQAg%3D%3D")
            m = re.search(r'"channelId":"(UC[\w-]{22})","title":\{"simpleText":"([^"]+)"', res)
        except (urllib.error.URLError, TimeoutError, OSError):
            m = None
    if m:
        src["channel_id"] = m.group(1)
        n = re.search(r'<meta property="og:title" content="([^"]+)"', page)
        if n and src.get("name") == h:
            src["name"] = html.unescape(n.group(1))
        return m.group(1)
    log.warning("Nem találom a csatornaazonosítót: @%s", h)
    return None


def parse_source(text: str) -> Optional[dict]:
    """Telegramos /csatorna paraméter: YouTube-link (csatorna vagy videó) vagy @név."""
    t = text.strip()
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", t)
    if m:
        return {"name": m.group(1), "channel_id": m.group(1)}
    m = re.search(r"(?:watch\?v=|youtu\.be/|/live/)([\w-]{11})", t)
    if m:  # videólink: a csatornát a videó oldaláról olvassuk ki
        return {"name": m.group(1), "video": m.group(1)}
    m = re.search(r"(?:youtube\.com/)?@([\w.\-]+)", t)
    if m:
        return {"name": m.group(1), "handle": m.group(1)}
    return None


def latest_videos(channel_id: str) -> list:
    try:
        xml = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
        root = ET.fromstring(xml)
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as e:
        log.warning("Videólista nem olvasható (%s): %s", channel_id, e)
        return []
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    out = []
    for e in root.findall("a:entry", ns):
        vid = (e.findtext("yt:videoId", "", ns) or "").strip()
        title = (e.findtext("a:title", "", ns) or "").strip()
        pub = (e.findtext("a:published", "", ns) or "").strip()
        desc = (e.findtext("m:group/m:description", "", ns) or "").strip()
        author = (e.findtext("a:author/a:name", "", ns) or "").strip()
        link = e.find("a:link", ns)
        href = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}"
        if vid and "/shorts/" not in href:
            out.append({"id": vid, "title": title, "published": pub, "description": desc, "author": author,
                        "url": f"https://www.youtube.com/watch?v={vid}"})
    return out


def video_minutes(vid: str) -> Optional[int]:
    """A videó hossza percben (a videóoldalból); ha nem derül ki, None (ilyenkor a Gemini dönt)."""
    try:
        page = _get(f"https://www.youtube.com/watch?v={vid}")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    m = re.search(r'"lengthSeconds":"(\d+)"', page)
    return int(m.group(1)) // 60 if m else None


def watch(ai: "kc.AIClient", v: dict) -> Optional[dict]:
    """A Gemini megnézi a videót (YouTube-link közvetlenül), és részletes összefoglalót ad idézetekkel."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    models = [m.strip() for m in (ai.cfg.gemini_model or "").split(",") if m.strip()] or ["gemini-2.5-flash"]
    prompt = (f"Videó címe: {v.get('title') or '(nem ismert)'}\nCsatorna: {v.get('author', '')}\nLeírás: {v.get('description', '')[:800]}\n\n"
              "Nézd meg a videót, és add vissza JSON-ben: {\"newsworthy\": true/false (van-e benne hírértékű, "
              "magyar olvasót érdeklő állítás, bejelentés, vita – reklám, zene, előzetes, általános csevegés: false), "
              "\"topic\": \"egy mondat: miről szól\", \"speakers\": [\"név – szerep\"], \"summary\": \"részletes "
              "magyar összefoglaló 500–1200 szóban, a fontos állításokkal, ki mit mondott, a legjobb szó szerinti "
              "idézetekkel „...” jelek között\", \"uncertain\": [\"bizonytalanul érthető nevek/számok/mondatok\"], \"minutes\": \"a videó teljes hossza percben, egész szám\", "
              "\"channel\": \"a csatorna / műsor neve\", \"title\": \"a videó eredeti címe\"}")
    body = {"contents": [{"parts": [{"file_data": {"file_uri": v["url"]}, "video_metadata": {"end_offset": "3600s", "fps": 0.1}},
                                    {"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": VIDEO_SYSTEM}]},
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 8000,
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
    last = None
    for model in models:
        try:
            data = kc.post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                {"x-goog-api-key": key}, body, 300, 2)
            kc.note_usage("gemini")
            text = "".join(p.get("text", "") for p in ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts", []))
            return kc.extract_json(text)
        except Exception as e:  # noqa: BLE001
            kc.note_usage("gemini", ok=False)
            log.warning("Videó feldolgozása sikertelen (%s, %s): %s", v["id"], model, str(e)[:200])
            last = e
    log.warning("Videó kimarad: %s", last)
    return None


def video_cover(v: dict) -> Optional[dict]:
    """A cikk főképe: a videó saját előnézeti képe (a YouTube-on is ezt látni), rajta egy lejátszás-gomb – mintha a
    YouTube-on rá akarnál kattintani. Nem AI-kép, és mindig a videóhoz illik."""
    try:
        import io
        import hashlib
        from PIL import Image, ImageDraw
        data = None
        for q in ("maxresdefault", "sddefault", "hqdefault"):
            try:
                req = urllib.request.Request(f"https://i.ytimg.com/vi/{v['id']}/{q}.jpg", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                if len(data) > 8000:
                    break
            except Exception:  # noqa: BLE001
                data = None
        if not data:
            return None
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.width < 1200:
            im = im.resize((1280, int(1280 * im.height / im.width)))
        W, H = im.size
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(shade)
        r = int(min(W, H) * 0.11)
        cx, cy = W // 2, H // 2
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(14, 16, 36, 190), outline=(236, 230, 216, 235), width=max(3, r // 14))
        d.polygon([(cx - r * 0.32, cy - r * 0.5), (cx - r * 0.32, cy + r * 0.5), (cx + r * 0.55, cy)], fill=(236, 230, 216, 255))
        im = Image.alpha_composite(im.convert("RGBA"), shade).convert("RGB")
        out = kc.BASE_DIR / "public" / "img" / "gen"
        out.mkdir(parents=True, exist_ok=True)
        name = f"vid-{hashlib.sha1(v['id'].encode()).hexdigest()[:10]}.jpg"
        im.save(out / name, "JPEG", quality=86, optimize=True)
        who = v.get("author") or "YouTube"
        return {"url": f"{kc.SITE_URL}/public/img/gen/{name}", "local": str(out / name), "kind": "photo",
                "alt": v.get("title", "")[:200], "credit": f"A videó előnézeti képe – {who} (YouTube)",
                "license": "a csatorna tulajdona, a beágyazott videó előnézete", "source_url": v.get("url", ""),
                "card": "video", "width": im.width, "height": im.height}
    except Exception as e:  # noqa: BLE001
        log.warning("Videó-előnézeti kép kimaradt: %s", e)
        return None


def article_from_video(ai: "kc.AIClient", v: dict, tz: ZoneInfo, articles: list, force: bool = False) -> Optional[dict]:
    """force=True: kérésre (Telegramon küldött link) – nem szűr hírértékre, rövidebb videóból is ír."""
    info = watch(ai, v)
    if info and not v.get("title"):
        v["title"] = str(info.get("title") or info.get("topic") or "Videó")[:150]
    if info and not v.get("author") and info.get("channel"):
        v["author"] = str(info["channel"])[:60]
    if not info or (not force and (not info.get("newsworthy") or len(str(info.get("summary") or "")) < 400)) \
            or len(str(info.get("summary") or "")) < 150:
        log.info("Videó: nem hírértékű vagy kevés tartalom – %s", v["title"][:80])
        return None
    unsure = [str(u) for u in info.get("uncertain") or [] if str(u).strip()][:10]
    who = v.get("author") or str(info.get("channel") or "") or "a csatorna"
    fulltext = (f"[Videó/podcast: {v.get('author', '')} – {v['title']}]\nSzereplők: {', '.join(info.get('speakers') or [])}\n"
                f"SZÓHASZNÁLAT: a forrást a csatorna / lap / műsor nevével említsd (pl. „a {who} műsorában”, „a {who} "
                f"videójában”, „– mondta a {who} interjújában”); SOHA ne így: „egy online videóban”, „egy videóban”, "
                "„egy YouTube-videóban”.\n\n"
                + str(info["summary"])
                + (("\n\nBIZONYTALANUL ÉRTHETŐ RÉSZEK (ezeket ne állítsd biztosnak, ne idézd): " + "; ".join(unsure)) if unsure else ""))
    now = datetime.now(tz)
    story = [{"title": v["title"], "link": v["url"], "summary": str(info.get("topic") or "")[:600],
              "source": v.get("author") or "YouTube", "published": now, "categories": [], "fulltext": fulltext}]
    story[0]["kw"] = kc._keywords(story[0]["title"] + " " + story[0]["summary"])
    section = kc.pick_section(ai, story, kc.SECTIONS.get("kozelet"))
    recent_imgs = kc.used_images(articles)
    art = kc.build_section_article(ai, section, now.date(), tz, story, recent_imgs, kc.related_past(articles, story))
    if not art:
        return None
    mins = v.get("minutes")
    if mins is None and str(info.get("minutes") or "").strip().isdigit():
        mins = int(str(info["minutes"]).strip())
    if not v.get("author") and info.get("channel"):
        v["author"] = str(info["channel"])[:60]
    if mins is None:
        try:
            mins = video_minutes(v["id"])
        except Exception:  # noqa: BLE001
            mins = None
    art.update({"status": "pending",
                "video": {"id": v["id"], "title": v["title"], "channel": v.get("author", ""), "minutes": mins},
                "hot_score": max(art.get("hot_score") or 0, 5)})
    cover = video_cover(v)
    if cover:  # a videó előnézete az első (kiválasztott) kép; a többi jelölt marad választhatónak
        art["image_options"] = [cover] + [im for im in art.get("image_options") or [] if im.get("url") != cover["url"]][:4]
        art["hero_image"] = cover
    return art


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False) -> int:
    if os.getenv("VIDEOS_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    st = kc.read_json(STATE_FILE, {"seen": [], "last_check": "", "done": {}})
    if st.get("last_check") and now - datetime.fromisoformat(st["last_check"]) < timedelta(minutes=int(os.getenv("VIDEO_CHECK_MIN", "15"))):
        return 0
    st["last_check"] = now.isoformat(timespec="seconds")
    today = d.isoformat()
    done_today = int((st.get("done") or {}).get(today, 0))
    seen = set(st.get("seen") or [])
    sources = load_sources()
    cands = []
    for src in sources:
        cid = resolve_channel(src)
        if not cid:
            continue
        for v in latest_videos(cid)[:5]:
            try:
                age = now - datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if v["id"] in seen:
                continue
            if age > timedelta(hours=36) or SKIP.search(v["title"]):
                seen.add(v["id"])  # régi vagy nem cikkbe való – többet nem nézzük
                continue
            mins = int(src.get("min_minutes", os.getenv("VIDEO_MIN_MINUTES", "20")))
            dur = video_minutes(v["id"])
            if dur is not None and dur < mins:
                seen.add(v["id"])  # túl rövid (rövid klip, részlet) – nem írunk belőle cikket
                continue
            v["minutes"] = dur
            cands.append(v)
    save_sources(sources)
    made = 0
    if cands and done_today < int(os.getenv("VIDEO_DAILY_MAX", "999")):
        v = sorted(cands, key=lambda x: x["published"], reverse=True)[0]
        seen.add(v["id"])
        articles = kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", [])
        art = article_from_video(ai, v, tz, articles)
        if art and not dry_run:
            import telegram_review as review
            review.add_pending(art)
            st.setdefault("done", {})[today] = done_today + 1
            log.info("✔ Videóból cikk: %s", art["title"])
            made = 1
    st["seen"] = list(seen)[-500:]
    st["done"] = {k: v for k, v in (st.get("done") or {}).items() if k >= (d - timedelta(days=7)).isoformat()}
    if not dry_run:
        kc.write_json_atomic(STATE_FILE, st)
    return made
