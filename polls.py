#!/usr/bin/env python3
"""
Kollektíva – napi szavazás
==========================

Naponta egy szavazás a nap legvitatottabb (már kint lévő) közéleti/világ/pénz/tech/életmód témájához: az AI 3
véleménykérdést javasol Telegramon, és csak a te ✅ választásod után kerül ki (🔄 másik téma, 🗑 ma nincs).
A kérdést az AI írja (pártsemlegesen, nem sugalmazó módon), a szavazatokat a Cloudflare D1 adatbázis tárolja
(functions/api/poll.js). A kérdések listája: public/data/polls.json (legfrissebb elöl, max. 30).
Futás: a robot (kollektiva_content.py) hívja; naponta egyszer, 10 és 20 óra között.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.polls")
POLL_SECTIONS = ("kozelet", "vilag", "penzvilag", "tech", "eletmod")

POLL_SYSTEM = (
    "Egy magyar online magazin (Kollektíva) szerkesztője vagy. Egy cikkhez olvasói szavazást írsz. SZABÁLYOK: a kérdés "
    "rövid, érthető, semleges – nem sugalmaz választ, nem sértő, nem pártpolitikai hovatartozásra kérdez rá, nem "
    "személyeskedik; a válaszlehetőségek kiegyensúlyozottak, egymást kizárók, és lefedik a fő álláspontokat "
    "(lehet köztük „Nem tudom / nincs véleményem”). Bűnügyben nem kérdezhetsz rá valaki bűnösségére. "
    "MÓDSZERTAN: egy kérdés egyetlen dologra kérdezzen (ne legyen kettős kérdés), konkrét döntésre vagy helyzetre; "
    "csak olyan témát válassz, amiben az emberek tényleg megoszlanak; a pro és kontra válaszok száma és erőssége "
    "legyen azonos, egy köztes válasz belefér; ne legyen értékítéletet sugalló szó a kérdésben; a „Nem tudom” mindig "
    "az utolsó. Magázva vagy személytelenül kérdezz (pl. „Ön szerint…”, „Hogyan értékeli…”), ne tegezve. "
    "Csak érvényes JSON-t adsz vissza."
)


# Tegező alakok -> magázó (a modell néha a szabály ellenére tegez). A gyakori kérdésformákat cseréljük;
# ha ezután is tegező jelet találunk, a kérdést újraíratjuk (új szavazásnál), vagy kimarad.
FORMAL_SUBS = [
    (r"\bSzerinted\b", "Ön szerint"), (r"\bszerinted\b", "ön szerint"),
    (r"\bTe mit\b", "Ön mit"), (r"\bte mit\b", "ön mit"), (r"\bNeked\b", "Önnek"), (r"\bneked\b", "önnek"),
    (r"\bTámogatod\b", "Támogatja"), (r"\btámogatod\b", "támogatja"), (r"\bGondolod\b", "Gondolja"),
    (r"\bgondolod\b", "gondolja"), (r"\bTartod\b", "Tartja"), (r"\btartod\b", "tartja"),
    (r"\bÉrtesz egyet\b", "Egyetért"), (r"\bértesz egyet\b", "egyetért"), (r"\bEgyetértesz\b", "Egyetért"),
    (r"\begyetértesz\b", "egyetért"), (r"\bÉrtékeled\b", "Értékeli"), (r"\bértékeled\b", "értékeli"),
    (r"\bLátod\b", "Látja"), (r"\blátod\b", "látja"), (r"\bVárod\b", "Várja"), (r"\bvárod\b", "várja"),
    (r"\bfizetnél\b", "fizetne"), (r"\bvennél\b", "venne"), (r"\bszavaznál\b", "szavazna"),
]
INFORMAL = re.compile(r"\b(szerinted|neked|veled|tőled|nálad|te\s|gondolod|tartod|támogatod|szeretnéd|"
                      r"\w+od\?|\w+ed\?|\w+öd\?)", re.I)


def formal(text: str) -> str:
    for pat, rep in FORMAL_SUBS:
        text = re.sub(pat, rep, text)
    return text


DRAFT_FILE = kc.BASE_DIR / "data" / "review" / "poll_draft.json"
# tapasztalatra / ismeretre kérdező, „semmitmondó” kérdések – ezek nem vélemény-szavazások
BAD_Q = re.compile(r"(?i)\bhallott|milyen gyakran|\bismeri\b|tudott (már|róla)|olvasott|találkozott|"
                   r"értesült|követi (a|az)\b|mennyire (ismeri|tájékozott)")


def _fix_old(polls: list) -> bool:
    fixed = False
    for p in polls:  # a kint lévő szavazások tegező szövegét is javítjuk (magázás)
        q2, o2 = formal(p.get("question", "")), [formal(o) for o in p.get("options", [])]
        if q2 != p.get("question") or o2 != p.get("options"):
            p["question"], p["options"], fixed = q2, o2, True
    return fixed


def make_draft(ai: "kc.AIClient", d: date, output_dir: Path, exclude: set = frozenset()) -> Optional[dict]:
    """3 szavazás-javaslat a nap legvitatottabb témájához – Telegramon választasz, csak jóváhagyással kerül ki."""
    data = kc.read_json(output_dir / "polls.json", {"polls": []})
    polls = data.get("polls", [])
    articles = kc.read_json(output_dir / "articles.json", {"articles": []}).get("articles", [])
    used = ({p.get("article_url") for p in polls} | {r.get("article_url") for r in data.get("removed", [])}
            | set(exclude))
    since = (d - timedelta(days=1)).isoformat()
    cands = [a for a in articles if a.get("status") == "published" and a.get("category") in POLL_SECTIONS
             and (a.get("date") or "") >= since and a.get("url") not in used and not a.get("offtopic")]
    cands = sorted(cands, key=lambda a: a.get("hot_score") or 0, reverse=True)[:10]
    # csak ha van igazán forró téma (szerkesztői kérés, 11.07.): a gyenge napokon nincs szavazás
    cands = [a for a in cands if float(a.get("hot_score") or 0) >= float(os.getenv("POLL_MIN_HOT", "10"))]
    if not cands:
        return None
    listing = "\n".join(f"{i + 1}. {a['title']} – {a.get('lead', '')[:200]}" for i, a in enumerate(cands))
    prompt = (f"A mai cikkeink:\n{listing}\n\nVálaszd ki azt az EGY cikket, amelynek témájában a magyar társadalom "
              "valóban megosztott, és amiről mindenkinek lehet véleménye (közpolitikai döntés, javaslat, értékválasztás, "
              "ár/adó/szabály, fontossági sorrend). Írj hozzá 3 KÜLÖNBÖZŐ szavazási kérdést. A kérdés VÉLEMÉNYT kérjen "
              "(pl. „Ön támogatja, hogy…?”, „Mit tart fontosabbnak…?”, „Jó döntés-e…?”, „Mennyit lenne hajlandó…?”) – "
              "TILOS tapasztalatra vagy ismeretre kérdezni („hallott-e”, „milyen gyakran”, „ismeri-e”), és tilos "
              "olyat kérdezni, amire szinte mindenki ugyanazt felelné. Bűnügynél, tragédiánál ne válassz cikket. "
              'JSON: {"article": a cikk sorszáma, "polls": [{"question": "max. 12 szó", "options": ["2–4 rövid válasz, '
              'max. 6 szó"]}, …3 db]} vagy ha egyik cikk sem alkalmas: {"skip": true}')
    try:
        raw = ai.complete_json(POLL_SYSTEM, prompt, 1200)
    except (kc.AIError, ValueError, TypeError, KeyError) as e:
        log.warning("Szavazás-javaslat kimaradt: %s", e)
        return None
    if raw.get("skip"):
        return None
    try:
        art = cands[int(raw.get("article")) - 1]
    except (TypeError, ValueError, IndexError):
        art = cands[0]
    opts = []
    for pl in raw.get("polls") or []:
        if not isinstance(pl, dict):
            continue
        q = formal(str(pl.get("question") or "").strip()[:140])
        o = [formal(str(x).strip()[:60]) for x in (pl.get("options") or []) if str(x).strip()][:4]
        if q and len(o) >= 2 and not INFORMAL.search(q) and not BAD_Q.search(q):
            opts.append({"question": q, "options": o})
    if not opts:
        log.info("Szavazás-javaslat: nem lett elfogadható kérdés (%s).", art["title"])
        return None
    return {"id": hashlib.sha1(f"{d.isoformat()}|{art['url']}|{len(used)}".encode()).hexdigest()[:10],
            "date": d.isoformat(), "status": "pending", "article_url": art["url"], "article_title": art["title"],
            "category": art.get("category"), "choices": opts[:3]}


def send_draft(dr: dict) -> None:
    import telegram_review as tr
    chat = tr.load_state().get("chat_id") if os.getenv("TELEGRAM_BOT_TOKEN") else None
    if not chat:
        return
    txt = (f"🗳 Mai szavazás – válassz (cikk: {dr['article_title']}):\n\n"
           + "\n\n".join(f"{i + 1}) {c['question']}\n   " + " / ".join(c["options"]) for i, c in enumerate(dr["choices"])))
    btns = [{"text": f"✅ {i + 1}", "callback_data": f"pok|{dr['id']}|{i}"} for i in range(len(dr["choices"]))]
    r = tr.tg("sendMessage", {"chat_id": chat, "text": txt[:4000], "reply_markup": {"inline_keyboard": [
        btns, [{"text": "🔄 Másik téma", "callback_data": f"pre|{dr['id']}"},
               {"text": "🗑 Ma nincs szavazás", "callback_data": f"pno|{dr['id']}"}]]}})
    dr["msg_id"] = tr._mid(r)


def approve(dr: dict, i: int, output_dir: Path, tz: ZoneInfo) -> dict:
    now = datetime.now(tz)
    path = output_dir / "polls.json"
    data = kc.read_json(path, {"polls": []})
    c = dr["choices"][i]
    poll = {"id": hashlib.sha1(f"{dr['date']}|{dr['article_url']}".encode()).hexdigest()[:12], "date": dr["date"],
            "question": c["question"], "options": c["options"], "article_url": dr["article_url"],
            "article_title": dr["article_title"], "category": dr.get("category"),
            "created_at": now.isoformat(timespec="seconds"),
            "closes_at": (now + timedelta(days=int(os.getenv("POLL_DAYS", "7")))).isoformat(timespec="seconds")}
    kc.write_json_atomic(path, {**data, "updated_at": now.isoformat(timespec="seconds"),
                                "polls": [poll] + data.get("polls", [])[:29]})
    dr["status"] = "approved"
    kc.write_json_atomic(DRAFT_FILE, dr)
    return poll


def run(ai: "kc.AIClient", d: date, tz: ZoneInfo, output_dir: Path, dry_run: bool = False) -> int:
    """Naponta egy szavazás-javaslat (3 kérdés) Telegramra, 10 és 20 óra között. Csak a te jóváhagyásoddal kerül ki."""
    if os.getenv("POLLS_ENABLED", "true").lower() not in ("1", "true", "yes") or not ai.enabled:
        return 0
    now = datetime.now(tz)
    path = output_dir / "polls.json"
    data = kc.read_json(path, {"polls": []})
    polls = data.get("polls", [])
    if _fix_old(polls) and not dry_run:
        log.info("Szavazás: tegező szöveg javítva magázóra.")
        kc.write_json_atomic(path, {**data, "polls": polls})
    dr = kc.read_json(DRAFT_FILE, {})
    gap = int(os.getenv("POLL_EVERY_DAYS", "2"))  # legfeljebb ennyi naponta egy szavazás
    recent = (d - timedelta(days=gap - 1)).isoformat()
    if any((p.get("date") or "") >= recent for p in polls) or (dr.get("date") or "") >= recent or not 10 <= now.hour <= 20:
        return 0
    draft = make_draft(ai, d, output_dir)
    if not draft:
        return 0
    log.info("✔ Szavazás-javaslat: %s", [c["question"] for c in draft["choices"]])
    if dry_run:
        return 1
    send_draft(draft)
    kc.write_json_atomic(DRAFT_FILE, draft)
    return 1
