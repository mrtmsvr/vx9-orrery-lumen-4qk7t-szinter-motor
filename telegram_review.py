#!/usr/bin/env python3
"""
Kollektíva – emberi jóváhagyás Telegramon
=========================================

Módok (REVIEW_MODE környezeti változó):
  hybrid (alap): a cikk jóváhagyásra vár; „✅ Kirakom” után azonnal kikerül. Hír magától nem kerül ki
                 (AUTO_PUBLISH_NEWS=true esetén AUTO_PUBLISH_MIN perc után igen); a saját időzített anyag
                 csendes időszakban kimehet. Kint lévő cikknél utólag is lehet címet/képet cserélni, törölni.
  post:          a hír azonnal kikerül, Telegramon csak utólagos ellenőrzés.
  pre:           csak jóváhagyás után jelenik meg (nincs automatikus kirakás).

Egy cikkhez ezt kapod:
  1. fejléc: rovat, forróság, címjavaslatok, lead, „Röviden” pontok, források
  2. a teljes szöveg
  3. a képjelöltek számozva (max. 10)
  4. vezérlőüzenet gombokkal:  Cím 1–3 · Kép 1–N / Nincs kép · 🔄 Új képek · 🆕 Új címek ·
     ✅ Kirakom · 🔁 Újraírás · 🗑 Elvetem
Új képek: a gomb eldobja a mostani jelölteket és újakat keres; válaszban (/k hadihajó) megadhatod, mit keressen.
Saját cím: a ✏️ gomb után írd be (vagy válaszolj: /c az új cím).

Futtatás: `python telegram_review.py --loop 600` (GitHub Actions) – kb. 10 percig figyeli a gombnyomásokat,
és azonnal feldolgozza őket (a változás a mentés után 1–2 perccel él az oldalon). Az első üzenet, amit a botnak írsz, összeköti a botot veled (chat ID).

Állapot: data/review/pending.json (függő cikkek), data/review/state.json (offset, chat ID, elvetett linkek).
A token csak a TELEGRAM_BOT_TOKEN környezeti változóból jön, sehova nem íródik ki.
"""
from __future__ import annotations

import copy
import hashlib
import html
import re
import subprocess
import threading
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.telegram")
REVIEW_DIR = kc.BASE_DIR / "data" / "review"
PENDING_FILE = REVIEW_DIR / "pending.json"
STATE_FILE = REVIEW_DIR / "state.json"
PENDING_MAX_AGE_H = int(os.getenv("PENDING_MAX_AGE_H", "48"))
PUBLISH_FLAG = kc.BASE_DIR / ".published_flag"
MODE = os.getenv("REVIEW_MODE", "hybrid").strip().lower()
AUTO_PUBLISH_MIN = int(os.getenv("AUTO_PUBLISH_MIN", "30"))
# hír csak a te jóváhagyásoddal kerül ki (AUTO_PUBLISH_NEWS=true visszakapcsolja a „30 perc után magától” működést);
# a saját (időzített) anyag továbbra is csendes időszakban, a határidejéig magától kimehet
AUTO_NEWS = os.getenv("AUTO_PUBLISH_NEWS", "false").lower() in ("1", "true", "yes")
E = lambda t: html.escape(str(t or ""), quote=False)  # noqa: E731


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def tg(method: str, payload: dict, timeout: int = 30) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {}
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning("Telegram %s: HTTP %s %s", method, e.code, e.read()[:300].decode("utf-8", "ignore"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("Telegram %s: %s", method, getattr(e, "reason", type(e).__name__))
    return {}


# ---------------------------------------------------------------------------
# Azonnali fogadás (webhook): a Telegram a Cloudflare-végpontra (/api/tg) küld, az D1-sorba teszi,
# visszajelez és elindítja a robotot; a robot innen veszi ki. Ha nincs TG_WEBHOOK_SECRET, marad a régi lekérdezés.
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = f"{kc.SITE_URL}/api/tg"
ALLOWED_UPDATES = ["message", "callback_query"]


def _queue_get(timeout: int = 15) -> Optional[dict]:
    req = urllib.request.Request(WEBHOOK_URL, headers={"X-Queue-Secret": WEBHOOK_SECRET, "User-Agent": "KollektivaBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning("Telegram-sor: HTTP %s", e.code)
        return {"ok": False, "status": e.code}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("Telegram-sor: %s", getattr(e, "reason", type(e).__name__))
        return None


def _webhook_on(st: dict) -> bool:
    """Bekapcsolja a webhookot, ha a Cloudflare-végpont működik; ha elromlott, visszaáll a régi lekérdezésre."""
    if not WEBHOOK_SECRET:
        if st.get("webhook"):
            tg("deleteWebhook", {"drop_pending_updates": False})
            st.pop("webhook", None)
        return False
    if st.get("webhook") == WEBHOOK_URL:
        return True
    probe = _queue_get()
    if not probe or not probe.get("ok"):
        log.warning("A /api/tg végpont még nem működik (Cloudflare-titkok?) – marad a régi lekérdezés.")
        return False
    r = tg("setWebhook", {"url": WEBHOOK_URL, "secret_token": WEBHOOK_SECRET, "allowed_updates": ALLOWED_UPDATES,
                          "max_connections": 5})
    if r.get("ok"):
        st["webhook"] = WEBHOOK_URL
        st["_webhook_buffer"] = probe.get("result") or []
        log.info("✔ Telegram webhook bekapcsolva: %s", WEBHOOK_URL)
        return True
    return False


def _updates(st: dict, wait: int) -> dict:
    """Új Telegram-frissítések: webhookos módban a Cloudflare-sorból (3 mp-enként néz rá), egyébként getUpdates."""
    if _webhook_on(st):
        buf = st.pop("_webhook_buffer", [])
        if buf:
            return {"ok": True, "result": buf}
        end = time.time() + max(0, wait)
        while True:
            r = _queue_get()
            if r and r.get("ok"):
                if r.get("result"):
                    return r
            elif r and r.get("status") in (403, 503):  # a végpont elromlott -> vissza a régi módra
                tg("deleteWebhook", {"drop_pending_updates": False})
                st.pop("webhook", None)
                break
            if time.time() >= end:
                return {"ok": True, "result": []}
            time.sleep(3)
    return tg("getUpdates", {"offset": st.get("offset", 0), "timeout": wait, "allowed_updates": ALLOWED_UPDATES},
              timeout=wait + 15)


def _fresh_titles(ai, a: dict, n: int = 2) -> list:
    """Visszahívott cikkhez (kép vagy javítás miatt), vagy a 🆕 Új címek gombra: a mostani cím + n új címjavaslat."""
    out = [a.get("title", "")]
    try:
        ai = ai or kc.AIClient(kc.Config.from_env())
        raw = ai.complete_json("Hírszerkesztő vagy egy magyar online magazinnál. Csak JSON-t adsz vissza.",
                               f"Cím: {a.get('title', '')}\nBevezető: {a.get('lead', '')}\n\nÍrj {n} új, ütős, de igaz címet, ami "
                               "más hangsúllyal vagy szöggel szól; a fontos kulcsszavak (személy neve, helyszín, a lényeg) maradhatnak benne, "
                               "ismert személy nevét ne írd körül "
                               "(max. 9 szó, pont nélkül a végén, olcsó clickbait-sablonok nélkül). "
                               'JSON: {"titles": ["...", "..."]}', 300, light=True)
        for t in raw.get("titles") or []:
            t = kc._clean_title(str(t).strip())
            if t and t not in out:
                out.append(t)
    except Exception as e:  # noqa: BLE001
        log.warning("Címjavaslat kimaradt: %s", str(e)[:150])
    out = out[:n + 1]
    if len(out) > 1:  # az újakat helyesírás-ellenőrzi (a mostani cím marad)
        out = [out[0]] + kc.proofread_titles(ai or kc.AIClient(kc.Config.from_env()), out[1:])
    return out


def _download_file(file_id: str) -> Optional[bytes]:
    """A chatbe küldött kép/matrica letöltése (Telegram getFile)."""
    try:
        path = (tg("getFile", {"file_id": file_id}).get("result") or {}).get("file_path")
        if not path:
            return None
        url = f"https://api.telegram.org/file/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{path}"
        with urllib.request.urlopen(url, timeout=40) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        log.warning("Fájlletöltés sikertelen: %s", str(e)[:120])
        return None


def _resend_waiting(out_dir: Path, st: dict, pending: list) -> bool:
    """/f vagy a 📋 gomb: minden még ki nem rakott cikk újra a chat aljára (a régi üzenetei törlődnek), minden gombbal."""
    waiting = [a for a in pending if not a.get("live")]
    if not waiting:
        tg("sendMessage", {"chat_id": st["chat_id"], "text": "Nincs váró cikk."})
        return False
    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"📋 {len(waiting)} váró cikk:"})
    for a in waiting:
        keep = {k: a.get("review", {}).get(k) for k in ("title", "image", "custom_title") if a.get("review", {}).get(k) is not None}
        _drop_old_messages(st["chat_id"], a)
        send_article(out_dir, a)
        if keep:  # a korábbi cím-/képválasztásod megmarad
            a["review"].update(keep)
            _refresh_control(st["chat_id"], a)
    return True


def _mid(resp: dict) -> Optional[int]:
    r = resp.get("result")
    if isinstance(r, list):
        return r[0].get("message_id") if r else None
    return (r or {}).get("message_id")


# ---------------------------------------------------------------------------
# Állapot
# ---------------------------------------------------------------------------

def load_state() -> dict:
    return kc.read_json(STATE_FILE, {"offset": 0, "chat_id": None, "rejected_links": []})


def save_state(st: dict) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    st["rejected_links"] = st.get("rejected_links", [])[-800:]
    kc.write_json_atomic(STATE_FILE, st)


# A Telegram-figyelő és a vele párhuzamosan futó tartalomgyártás (--content) ugyanazokat a fájlokat írja:
# a függő lista/állapot módosítása csak ezzel a zárral történhet.
LOCK = threading.RLock()
LOOP_THREAD: Optional[int] = None  # a figyelő szál; ha fut, csak ő kérdezi le a Telegramot


def add_pending(art: dict) -> None:
    """Új cikk Telegramra + a függő listába, zárral (a párhuzamos gombnyomás-feldolgozás ne írja felül)."""
    with LOCK:
        send_article(None, art)
        cur = load_pending()
        cur.append(art)
        save_pending(None, cur)


_PITCH_TAKEN: set = set()


def _write_pitch(out_dir: Path, pit: dict, tz: ZoneInfo, chat: int) -> None:
    try:
        live_arts = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
        new = kc.build_from_pitch(kc.AIClient(kc.Config.from_env()), pit, tz, live_arts)
    except Exception as e:  # noqa: BLE001
        log.warning("Témajavaslatból cikk sikertelen: %s", e)
        new = None
    if new:
        add_pending(new)
        with LOCK:
            rq = kc.read_json(PITCH_RETRY_FILE, {})
            if rq.pop(pit.get("id"), None) is not None:
                kc.write_json_atomic(PITCH_RETRY_FILE, rq)
        return
    if kc.ai_quota_out():  # elfogyott a napi ingyenes AI-keret: nem dobjuk el, később magától megírja
        with LOCK:
            rq = kc.read_json(PITCH_RETRY_FILE, {})
            first = pit.get("id") not in rq
            r = rq.setdefault(pit.get("id"), {"tries": 0, "since": time.time()})
            r["tries"] += 1
            r["next"] = time.time() + PITCH_RETRY_MIN * 60
            if time.time() - r["since"] < 24 * 3600:
                kc.write_json_atomic(PITCH_RETRY_FILE, rq)
                _PITCH_TAKEN.discard(pit.get("id"))
                if first:
                    tg("sendMessage", {"chat_id": chat, "text": f"⏳ Elfogyott a mai ingyenes AI-keret – később magától megírom: "
                                                               f"{pit.get('title', '')}"})
                return
    with LOCK:  # végleges hiba (vagy 24 óra után is keret-hiány): kikerül az újrapróbálásból
        rq = kc.read_json(PITCH_RETRY_FILE, {})
        if rq.pop(pit.get("id"), None) is not None:
            kc.write_json_atomic(PITCH_RETRY_FILE, rq)
    tg("sendMessage", {"chat_id": chat, "text": f"Ebből most nem sikerült cikket írni: {pit.get('title', '')}"})


PITCH_RETRY_FILE = REVIEW_DIR / "pitch_retry.json"
PITCH_RETRY_MIN = int(os.getenv("PITCH_RETRY_MIN", "45"))


def _retry_pitches(out_dir: Path, tz: ZoneInfo) -> None:
    """A keret miatt elmaradt ✍️ témák újrapróbálása (egyszerre egy, hogy ne égesse el rögtön az új keretet)."""
    with LOCK:
        rq = kc.read_json(PITCH_RETRY_FILE, {})
        due = [k for k, v in rq.items() if v.get("next", 0) <= time.time() and k not in _PITCH_TAKEN]
        if not due:
            return
        pit = kc.read_json(kc.PITCH_FILE, {"items": {}}).get("items", {}).get(due[0])
        chat = load_state().get("chat_id")
        if not pit or not chat:
            rq.pop(due[0], None)
            kc.write_json_atomic(PITCH_RETRY_FILE, rq)
            return
        rq[due[0]]["next"] = time.time() + PITCH_RETRY_MIN * 60
        kc.write_json_atomic(PITCH_RETRY_FILE, rq)
        _PITCH_TAKEN.add(due[0])
    threading.Thread(target=_write_pitch, args=(out_dir, pit, tz, chat), daemon=False).start()


def _daily_model_check(tz: ZoneInfo) -> None:
    """Naponta egyszer (8 óra után): élnek-e még a beállított AI-modellek, és elég volt-e tegnap a keret."""
    now = datetime.now(tz)
    st = load_state()
    if now.hour < 8 or st.get("model_check") == now.date().isoformat() or not st.get("chat_id"):
        return
    probs = kc.model_health()
    with LOCK:
        st = load_state()
        st["model_check"] = now.date().isoformat()
        save_state(st)
    if probs:
        tg("sendMessage", {"chat_id": st["chat_id"], "text": "🩺 Napi rendszerellenőrzés:\n\n" + "\n\n".join(f"• {p}" for p in probs)})


def load_pending(_out_dir: Optional[Path] = None) -> list:
    return kc.read_json(PENDING_FILE, {"articles": []}).get("articles", [])


def save_pending(_out_dir: Optional[Path], items: list) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    kc.write_json_atomic(PENDING_FILE, {"articles": items})


def enabled(_out_dir: Optional[Path] = None) -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(load_state().get("chat_id"))


def rejected_links(_out_dir: Optional[Path] = None) -> set:
    return set(load_state().get("rejected_links", []))


# ---------------------------------------------------------------------------
# Küldés
# ---------------------------------------------------------------------------

def _chunks(text: str, size: int = 3800) -> list:
    out, cur = [], ""
    for p in text.split("\n\n"):
        if len(cur) + len(p) + 2 > size and cur:
            out.append(cur)
            cur = ""
        cur = (cur + "\n\n" + p).strip()
    if cur:
        out.append(cur)
    return out


def _chosen_title(art: dict) -> str:
    rv = art.get("review", {})
    if rv.get("custom_title"):
        return rv["custom_title"]
    opts = art.get("title_options") or [art["title"]]
    return opts[min(rv.get("title", 0), len(opts) - 1)]


def _control(art: dict) -> tuple:
    rv = art.get("review", {})
    sid = art["id"][:8]
    if rv.get("compact"):  # lezárt cikk: egyetlen sor; a ✏️ gomb (vagy /e) mindent újra előhoz
        return (f"{rv['compact']} · <b>{E(_chosen_title(art))}</b>\n{kc.SITE_URL}{art.get('url', '')}",
                {"inline_keyboard": [[{"text": "✏️ Módosítás (cím, kép, törlés…)", "callback_data": f"{sid}|ed"}]]})
    opts = art.get("title_options") or [art["title"]]
    imgs = art.get("image_options") or []
    ti, ii = rv.get("title", 0), rv.get("image", 0 if imgs else -1)
    mark = lambda ok: "✓ " if ok else ""  # noqa: E731
    cb_from = art.get("clickbait_from", len(opts))
    tb = [{"text": f"{mark(not rv.get('custom_title') and ti == i)}{'🔥 ' if i >= cb_from else 'Cím '}{i + 1}",
           "callback_data": f"{sid}|t|{i}"} for i in range(len(opts))]
    rows = [tb[k:k + 3] for k in range(0, len(tb), 3)]
    btns = [{"text": f"{mark(ii == i)}Kép {i + 1}", "callback_data": f"{sid}|i|{i}"} for i in range(len(imgs))]
    btns.append({"text": f"{mark(ii == -1)}Nincs kép", "callback_data": f"{sid}|i|-1"})
    rows += [btns[k:k + 4] for k in range(0, len(btns), 4)]
    rows.append([{"text": "🔄 Új képek", "callback_data": f"{sid}|img"},
                 {"text": "🎨 Grafika", "callback_data": f"{sid}|gen"}])
    rows.append([{"text": "🆕 Új címek", "callback_data": f"{sid}|nt"},
                 {"text": "📄 Teljes szöveg", "callback_data": f"{sid}|txt"}])
    if (art.get("legal") or {}).get("issues"):
        rows.append([{"text": "⚖️ Jogi javítás (a jelzések alapján)", "callback_data": f"{sid}|lfix"}])
    live = art.get("live")
    rows.append([{"text": "✅ Rendben" if live else "✅ Kirakom", "callback_data": f"{sid}|ok"},
                 {"text": "🗑 Törlés" if live else "🗑 Elvetem", "callback_data": f"{sid}|no"}])
    img_txt = f"{ii + 1}. kép" if ii >= 0 and imgs else "nincs kép"
    auto = f" (magától: {AUTO_PUBLISH_MIN} perc)" if MODE == "hybrid" and AUTO_NEWS else " (magától nem kerül ki)"
    if art.get("hold") or art.get("category") == "bulvar":
        auto = " (magától nem kerül ki)"
    elif art.get("schedule") and MODE == "hybrid":
        auto = f" (magától: legkésőbb {str(art['schedule'].get('deadline', ''))[11:16]})"
    if art.get("scheduled_at") and not live:
        auto = f" (időzítve {art['scheduled_at'][11:16]} – ✅ = azonnal)"
    state = f"🟢 <b>Kint van</b> – {kc.SITE_URL}{art.get('url', '')}" if live else "⏳ <b>Vár</b>" + auto
    text = (f"{state} · {E(kc.SECTIONS.get(art['category'], {}).get('name', art['category']))}\n"
            f"<b>{E(_chosen_title(art))}</b> · {img_txt}")
    return text, {"inline_keyboard": rows}


DEPLOY_NOW = kc.BASE_DIR / "data" / "deploy_now"


def _deploy_now() -> None:
    """Levétel/törlés: a változás azonnal kerüljön ki (a napi build-keret ezt nem tartja vissza)."""
    try:
        DEPLOY_NOW.write_text("1")
    except OSError:
        pass


def _compact(chat: int, art: dict, label: str) -> None:
    """Kirakás / döntés után a cikk üzenetei (képek, Röviden, címek) eltűnnek, csak egy rövid sor marad."""
    rv = art.setdefault("review", {})
    for mid in (rv.get("msg_ids") or []) + [rv.get("title_prompt_id")]:
        if mid:
            tg("deleteMessage", {"chat_id": chat, "message_id": mid})
    rv["msg_ids"], rv["title_prompt_id"], rv["compact"] = [], None, label
    _refresh_control(chat, art)


def _expand(out_dir: Path, art: dict) -> None:
    """Lezárt (kint lévő) cikk teljes vezérlője újra: a mostani cím és kép az első helyen."""
    chat = load_state().get("chat_id")
    _drop_old_messages(chat, art)
    cur = _chosen_title(art)
    art["title_options"] = [cur] + [t for t in (art.get("title_options") or []) if t != cur][:2]
    art["clickbait_from"] = len(art["title_options"])
    hero = art.get("hero_image")
    imgs = [im for im in (art.get("image_options") or []) if not hero or im.get("url") != hero.get("url")]
    art["image_options"] = ([hero] if hero else []) + imgs[:5]
    art["review"] = {}
    if art.get("live"):
        art.setdefault("legal", {})  # kint lévő cikknél ne fusson újra a jogi automata (ne írja át csendben a szöveget)
    send_article(out_dir, art)


def _drop_old_messages(chat: int, art: dict) -> None:
    """Egy cikk korábbi Telegram-üzeneteinek törlése (újraküldés előtt), hogy ne látsszon kétszer ugyanaz a cikk."""
    rv = art.get("review") or {}
    for mid in (rv.get("msg_ids") or []) + [rv.get("control_id"), rv.get("title_prompt_id")]:
        if mid:
            tg("deleteMessage", {"chat_id": chat, "message_id": mid})  # 48 óránál régebbit a Telegram nem enged – nem baj


def send_article(_out_dir: Optional[Path], art: dict) -> None:
    chat = load_state().get("chat_id")
    if not chat:
        return
    sec = kc.SECTIONS.get(art["category"], {}).get("name", art["category"])
    titles = art.get("title_options") or [art["title"]]
    legal_txt = ""
    try:  # jogi ellenőr: kockázatjelzés a cikk alá; magas kockázatnál nem kerül ki magától
        import legal
        if "legal" not in art:
            legal.auto(kc.AIClient(kc.Config.from_env()), art)
        legal_txt = legal.summary(art.get("legal") or {})
    except Exception as e:  # noqa: BLE001
        log.warning("Jogi ellenőrzés kimaradt: %s", e)
    if not art.get("image_options") and not art.get("live") and not isinstance(art.get("video"), dict):
        try:  # nincs illő kép → ne „nincs kép”-pel jöjjön: generált grafika (ha a cikkírásnál nem sikerült)
            gen = kc.auto_illustration(kc.AIClient(kc.Config.from_env()), art, art.get("category", "x"))
            if gen:
                art["image_options"] = gen
        except Exception as e:  # noqa: BLE001
            log.warning("Automatikus grafika küldéskor kimaradt: %s", e)
    kind = "🟢 KINT VAN" if art.get("live") else ("🗓 SAJÁT (időzített)" if art.get("offtopic") else "🆕 ÚJ")
    if isinstance(art.get("video"), dict) and not art.get("live"):
        _m = art["video"].get("minutes")
        kind = f"🎬 VIDEÓBÓL ({art['video'].get('channel') or 'YouTube'}{f', {_m} perces videó' if _m else ''})"
    resent = art.pop("resent", None)
    if resent == "v":
        kind = "♻️ VISSZAVÉVE JAVÍTÁSRA (ugyanaz a cikk, nem új)"
    elif resent == "rw":
        kind = "🔁 ÚJRAÍRT VÁLTOZAT (az előzőt váltja, a régi üzenetek törölve)"
    head = (f"{kind} · <b>{E(sec)}</b> · forróság {art.get('hot_score', 0)} · {len(art.get('sources', []))} forrás · "
            f"{art.get('reading_time_min', 1)} perc"
            + (f" · +{len(art['inline_images'])} kép a szövegben" if art.get("inline_images") else "")
            + "\n\n<b>Címjavaslatok</b>\n"
            + "\n".join(f"{'🔥 ' if i >= art.get('clickbait_from', len(titles)) else ''}{i + 1}) {E(t)}"
                        for i, t in enumerate(titles))
            + f"\n\n<i>{E(art.get('lead'))}</i>")
    if art.get("key_points"):
        head += "\n\n<b>Röviden</b>\n" + "\n".join("• " + E(k) for k in art["key_points"])
    head += "\n\n<b>Források:</b> " + ", ".join(
        f'<a href="{html.escape(s["url"])}">{E(s.get("publisher") or "forrás")}</a>' for s in art.get("sources", []))
    extra = []
    if legal_txt and len(head) + len(E(legal_txt)) < 3990:
        head += "\n\n" + E(legal_txt)
    elif legal_txt:
        extra.append(E(legal_txt)[:3900])
    ids = [_mid(tg("sendMessage", {"chat_id": chat, "text": head[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": True}))]
    ids += [_mid(tg("sendMessage", {"chat_id": chat, "text": x, "parse_mode": "HTML"})) for x in extra]
    # a teljes szöveget nem küldjük el (elég a „Röviden”); kérésre: 📄 Teljes szöveg gomb
    imgs = art.get("image_options") or []
    ids += _send_images(chat, imgs)
    art["review"] = {"title": 0, "image": 0 if imgs else -1, "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    text, kb = _control(art)
    ctl = _mid(tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "reply_markup": kb}))
    art["review"].update({"msg_ids": [i for i in ids if i], "control_id": ctl})


def _send_photo_file(chat: int, path: str, caption: str) -> dict:
    """Helyi kép feltöltése (multipart) – a generált illusztráció még nincs kint az oldalon."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not os.path.exists(path):
        return {}
    bnd = uuid.uuid4().hex
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
            f"--{bnd}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
            f"--{bnd}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"kep.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n").encode() + open(path, "rb").read() + f"\r\n--{bnd}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("Telegram képfeltöltés: %s", e)
        return {}


def _send_images(chat: int, imgs: list, note: str = "") -> list:
    """Képjelöltek albumban (max. 10); ha a Telegram valamelyiket nem tudja letölteni, egyenként."""
    imgs = imgs[:10]
    if any(im.get("local") for im in imgs):  # generált képek: egyenként, feltöltéssel
        out = []
        for i, im in enumerate(imgs):
            capt = (f"{i + 1}. kép{note if i == 0 else ''} – {im.get('credit', '')}")[:200]
            r = _send_photo_file(chat, im["local"], capt) if im.get("local") else tg("sendPhoto", {"chat_id": chat, "photo": im["url"], "caption": capt})
            out.append(_mid(r) if r.get("ok") else None)
        return out
    cap = lambda i, im: (f"{i + 1}. kép{note if i == 0 else ''} – {im.get('credit', '')}")[:200]  # noqa: E731
    if len(imgs) >= 2:
        media = [{"type": "photo", "media": im["url"], "caption": cap(i, im)} for i, im in enumerate(imgs)]
        resp = tg("sendMediaGroup", {"chat_id": chat, "media": media}, timeout=90)
        if resp.get("ok"):
            return [m.get("message_id") for m in resp.get("result", [])]
        out = []
        for i, im in enumerate(imgs):
            r = tg("sendPhoto", {"chat_id": chat, "photo": im["url"], "caption": cap(i, im)})
            out.append(_mid(r) if r.get("ok") else _mid(tg("sendMessage", {"chat_id": chat, "text": f"{i + 1}. kép: {im['url']}"})))
        return out
    if imgs:
        return [_mid(tg("sendPhoto", {"chat_id": chat, "photo": imgs[0]["url"], "caption": cap(0, imgs[0])}))]
    return [_mid(tg("sendMessage", {"chat_id": chat, "text": "Ehhez a cikkhez nem találtam illő, szabad licencű képet."}))]


IMG_SYSTEM = ("Képszerkesztő vagy egy magyar hírmagazinnál. Egy cikkhez keresőkifejezéseket adsz a Wikimedia Commons "
              "és az Openverse szabad licencű fotóihoz. Csak érvényes JSON-t adsz vissza.")


def _new_images(art: dict, ai, hint: str = "") -> list:
    """🔄 Új képek: az AI a cikkből (és a szerkesztő kéréséből) új keresőszavakat ad, ezekre keresünk új jelölteket.
    A már mutatott képek nem jönnek újra."""
    if ai is None:
        ai = kc.AIClient(kc.Config.from_env())
    seen = set(art.get("images_seen") or []) | {im["url"] for im in art.get("image_options") or []}
    used = art.get("image_queries") or []
    specific, generic = [], []
    if ai.enabled:
        prompt = (f"Cikk címe: {_chosen_title(art)}\nBevezető: {art.get('lead', '')}\nLényeg: "
                  + " | ".join(art.get("key_points") or []) + "\nSzöveg eleje: " + " ".join(art.get("body") or [])[:1500]
                  + (f"\n\nA SZERKESZTŐ KÉRÉSE (ez a legfontosabb): {hint}" if hint else "")
                  + ("\n\nEzekkel már kerestünk, ne ismételd: " + "; ".join(used) if used else "")
                  + "\n\nAdj új keresőkifejezéseket. JSON: {\"specific\": [\"4–6 konkrét angol név: a cikkben szereplő "
                    "személyek, helyszínek, intézmények, járművek, tárgyak (pl. 'USS Gerald R. Ford', 'Budapest Keleti "
                    "railway station')\"], \"generic\": [\"2–3 egyszerű, fotózható angol hangulat-téma, pl. 'coffee cup', "
                    "'courtroom', 'hospital corridor'\"]}. Parliament / Országház csak ha a cikk tényleg arról szól.")
        try:
            raw = ai.complete_json(IMG_SYSTEM, prompt, 500, light=True)
            specific = [str(q).strip()[:60] for q in raw.get("specific") or [] if str(q).strip()][:6]
            generic = [str(q).strip()[:40] for q in raw.get("generic") or [] if str(q).strip()][:3]
        except (kc.AIError, ValueError, TypeError, KeyError) as e:
            log.warning("Képkulcsszavak (AI) kimaradtak: %s", e)
    if hint and not specific:
        specific = [hint[:60]]
    found = kc.find_images(specific, generic, kc.Config.from_env().http_timeout, seen, limit=8)
    try:  # más cikk főképe ne jöjjön újra jelöltnek
        cfg_ = kc.Config.from_env()
        others = [a for a in kc.read_json(cfg_.output_dir / "articles.json", {"articles": []}).get("articles", [])
                  if a.get("id") != art.get("id")]
        found = kc.drop_used(found, kc.used_images(others))
    except Exception as e:  # noqa: BLE001
        log.warning("Használt képek szűrése kimaradt: %s", e)
    found = kc.vision_rank(ai, _chosen_title(art), art.get("lead", ""), found)
    art["image_queries"] = (used + specific + generic)[-30:]
    return found


def _refresh_images(chat: int, art: dict, ai, hint: str = "") -> str:
    """Lecseréli a képjelölteket újakra (kint lévő cikknél a mostani kép marad az 1., amíg nem választasz)."""
    new = _new_images(art, ai, hint)
    if not new:
        tg("sendMessage", {"chat_id": chat, "reply_to_message_id": art["review"].get("control_id"),
                           "text": "Nem találtam új, illő képet. Írd meg válaszban, mit keressek (pl. /k hadihajó)."})
        return "Nincs új kép"
    art["images_seen"] = list({*(art.get("images_seen") or []), *(im["url"] for im in art.get("image_options") or [])})[-60:]
    keep = [art["hero_image"]] if art.get("live") and art.get("hero_image") else []
    art["image_options"] = keep + new
    art["review"]["image"] = 0
    ids = _send_images(chat, art["image_options"], " (a mostani)" if keep else "")
    art["review"]["msg_ids"] = (art["review"].get("msg_ids") or []) + [i for i in ids if i]
    # a vezérlő újra a képek alá kerül, hogy ne kelljen felgörgetni
    tg("deleteMessage", {"chat_id": chat, "message_id": art["review"].get("control_id")})
    text, kb = _control(art)
    art["review"]["control_id"] = _mid(tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                          "reply_markup": kb}))
    return f"{len(new)} új kép"


def _refresh_control(chat: int, art: dict) -> None:
    text, kb = _control(art)
    tg("editMessageText", {"chat_id": chat, "message_id": art["review"].get("control_id"), "text": text,
                           "parse_mode": "HTML", "reply_markup": kb})


# ---------------------------------------------------------------------------
# Kirakás
# ---------------------------------------------------------------------------

def publish(art: dict, tz: ZoneInfo) -> dict:
    rv = art.pop("review", {})
    title = _chosen_title({**art, "review": rv})
    imgs = art.get("image_options") or []
    ii = rv.get("image", 0 if imgs else -1)
    art["hero_image"] = kc.localize_image(imgs[ii] if 0 <= ii < len(imgs) else None)
    cat = art["category"]
    if title != art["title"]:
        art["title"] = title
        art["slug"] = kc.slugify(f"{art['date']}-{title}")
        art["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kc.SITE_URL}/{cat}/{art['slug']}"))
    art["url"] = f"/{cat}/{art['slug']}/"
    art["seo"].update({"meta_title": art["title"][:60], "canonical_url": f"{kc.SITE_URL}/{cat}/{art['slug']}/",
                       "og_image": (art["hero_image"] or {}).get("url"), "noindex": False})
    now_iso = datetime.now(tz).isoformat(timespec="seconds")
    art.update({"status": "published", "published_at": now_iso, "created_at": now_iso, "updated_at": now_iso})
    art["authorship"]["reviewed_by"] = "szerkesztő (Telegram)"
    art["authorship"]["reviewed_at"] = now_iso
    for k in ("title_options", "image_options", "story", "offtopic_topic", "schedule", "legal"):
        art.pop(k, None)
    return art


def _apply_live(out_dir: Path, art: dict, tz: ZoneInfo, remove: bool = False, content: bool = False) -> None:
    """A már kint lévő cikk módosítása az articles.json-ban (cím, kép, újraírt szöveg vagy törlés).
    Az URL (slug) nem változik, hogy a megosztott linkek ne törjenek el."""
    path = out_dir / "articles.json"
    data = kc.read_json(path, {"articles": []})
    items = data.get("articles", [])
    idx = next((i for i, a in enumerate(items) if a.get("id") == art["id"]), None)
    if idx is None:
        return
    if remove:
        items.pop(idx)
        _deploy_now()
    else:
        cur = items[idx]
        rv = art.get("review", {})
        imgs = art.get("image_options") or []
        ii = rv.get("image", 0 if imgs else -1)
        cur["title"] = _chosen_title(art)
        cur["hero_image"] = kc.localize_image(imgs[ii] if 0 <= ii < len(imgs) else None)
        if content:
            for k in ("lead", "key_points", "body", "content", "tags", "word_count", "reading_time_min", "sources"):
                if k in art:
                    cur[k] = art[k]
        cur["seo"].update({"meta_title": cur["title"][:60], "meta_description": cur.get("lead", "")[:160],
                           "og_image": (cur["hero_image"] or {}).get("url")})
        cur["updated_at"] = datetime.now(tz).isoformat(timespec="seconds")
    kc.write_json_atomic(path, {**data, "updated_at": datetime.now(tz).isoformat(timespec="seconds"), "articles": items})


def _add_published(out_dir: Path, art: dict, tz: ZoneInfo) -> None:
    path = out_dir / "articles.json"
    articles = kc.read_json(path, {"articles": []}).get("articles", [])
    articles.insert(0, art)
    articles.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    kc.write_json_atomic(path, {"schema_version": 1, "updated_at": datetime.now(tz).isoformat(timespec="seconds"),
                                "articles": articles[:int(os.getenv("ARTICLES_ARCHIVE_LIMIT", "600"))]})


def _go_live(out_dir: Path, art: dict, tz: ZoneInfo, auto: bool = False) -> dict:
    """A függő cikk kikerül az oldalra; a függő listában „kint van” állapotban marad, hogy utólag is
    lehessen címet/képet cserélni, újraíratni vagy törölni."""
    final = publish(copy.deepcopy(art), tz)
    if auto:
        final["authorship"]["reviewed_by"] = None
    _add_published(out_dir, final, tz)
    for k in ("id", "slug", "url", "seo", "status", "title", "hero_image", "published_at", "created_at", "updated_at"):
        art[k] = final[k]
    art["live"] = True
    return final


STAGGER_MIN = int(os.getenv("PUBLISH_GAP_MIN", "20"))


def _stagger_time(out_dir: Path, pending: list, tz: ZoneInfo) -> Optional[datetime]:
    """Ha az előző cikk STAGGER_MIN percen belül került ki (vagy van már időzített), a következő időpont; különben None."""
    if STAGGER_MIN <= 0:
        return None
    now = datetime.now(tz)
    times = []
    for a in kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])[:30]:
        try:
            t = datetime.fromisoformat(a.get("published_at") or "")
        except ValueError:
            continue
        times.append(t if t.tzinfo else t.replace(tzinfo=tz))
    for a in pending:
        if a.get("scheduled_at") and not a.get("live"):
            times.append(datetime.fromisoformat(a["scheduled_at"]))
    nxt = max(times, default=now - timedelta(days=1)) + timedelta(minutes=STAGGER_MIN)
    return nxt if nxt > now + timedelta(minutes=1) else None


def _age_min(art: dict) -> float:
    ts = art.get("review", {}).get("sent_at") or art.get("created_at")
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)  # a régi bejegyzések UTC-ben (GitHub Actions)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


# ---------------------------------------------------------------------------
# Beérkezett válaszok feldolgozása
# ---------------------------------------------------------------------------

def _make_graphic(chat: int, out_dir: Path, art: dict, ai, tz: ZoneInfo, hint: str = "") -> int:
    """Grafika a cikkhez (🎨 gomb vagy /g [kérés]); visszaadja, hány élő módosítás történt (0/1)."""
    lead = art.get("lead", "") + (f"\nA szerkesztő kérése a grafikához: {hint}" if hint else "")
    try:
        gen = kc.generate_illustration(ai or kc.AIClient(kc.Config.from_env()), _chosen_title(art), lead, art["id"])
    except Exception as e:  # noqa: BLE001 – a grafika hibája ne állítsa le a robotot
        kc.LAST_GEN_ERROR, gen = str(e)[:300], []
    if not gen:
        why = kc.LAST_GEN_ERROR or ("hiányzik a CF_ACCOUNT_ID vagy a CF_AI_TOKEN" if not (os.getenv("CF_ACCOUNT_ID")
                                     and os.getenv("CF_AI_TOKEN")) else "a képleíró AI-hívás nem sikerült")
        tg("sendMessage", {"chat_id": chat, "text": f"A grafika most nem készült el. Ok: {why}"})
        return 0
    art["image_options"] = gen + [im for im in art.get("image_options") or [] if not im.get("generated")]
    art["review"]["image"] = 0
    ids = _send_images(chat, gen, " (generált grafika)")
    art["review"]["msg_ids"] = (art["review"].get("msg_ids") or []) + [i for i in ids if i]
    tg("deleteMessage", {"chat_id": chat, "message_id": art["review"].get("control_id")})
    text_, kb_ = _control(art)
    art["review"]["control_id"] = _mid(tg("sendMessage", {"chat_id": chat, "text": text_,
                                                          "parse_mode": "HTML", "reply_markup": kb_}))
    if art.get("live"):
        _apply_live(out_dir, art, tz)
        return 1
    return 0


HELP = ("Parancsok (rövid / hosszú):\n"
        "/k /kep <mit> – új képek (válaszként a cikkre)\n"
        "/g /grafika [mit] – generált grafika (válaszként)\n"
        "/j /jogi – jogi ellenőrzés újra (válaszként)\n"
        "/c /cim <cím> – saját cím (válaszként)\n"
        "/u /ujrairas – újraírás (válaszként)\n"
        "/e <link|cím> – kint lévő cikk minden gombja újra (kint marad)\n"
        "/v /vissza <link|cím> – levétel javításra\n"
        "/t /torles <link|cím> – leszedés\n"
        "/kn – kép nélküli cikkek képválasztásra\n"
        "/csatornak · /csatorna <YouTube-link> – videófigyelés\n"
        "/l /lista · /f – a váró cikkek újra, minden gombbal\n"
        "/szavazas (állás, létszám) · /keret · /stat (olvasók)\n"
        "Instagram-előnézetre válaszként küldött kép/matrica = új háttér\n"
        "Link vagy „téma: …” → cikk róla")


def _stats_text(days: int = 7) -> str:
    """Saját olvasószámláló (/api/olvas, D1): napi olvasók és oldalmegtekintések, top cikkek, honnan jöttek."""
    try:
        req = urllib.request.Request(f"{kc.SITE_URL}/api/olvas?days={days}", headers={
            "User-Agent": "Mozilla/5.0 (KollektivaBot)", "X-Queue-Secret": os.getenv("TG_WEBHOOK_SECRET", "")})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"A statisztika most nem kérhető le: {e}"
    if not r.get("ok"):
        return "A statisztika most nem kérhető le."
    ds = r.get("days") or []
    lines = [f"📊 Olvasók (saját mérés, robotok nélkül) – utolsó {days} nap", ""]
    lines += [f"{d['day'][5:]}: 👥 {d['visitors']} olvasó · 📄 {d['views']} oldal" for d in ds]
    lines.append(f"Összesen: {sum(d['visitors'] for d in ds)} olvasó-nap · {sum(d['views'] for d in ds)} oldal")
    if r.get("top"):
        lines += ["", "🔝 Legolvasottabb:"] + [f"{x['v']} · {x['path']}" for x in r["top"][:8]]
    if r.get("sources"):
        lines += ["", "↪️ Honnan jöttek:"] + [f"{x['src']}: {x['v']}" for x in r["sources"]]
    return "\n".join(lines)


def _poll_counts(p: dict) -> str:
    """A szavazás állása az oldal API-jából (D1): létszám + opciónként szavazat és százalék."""
    try:
        req = urllib.request.Request(f"{kc.SITE_URL}/api/poll?id={p['id']}", headers={"User-Agent": "Mozilla/5.0 (KollektivaBot)"})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"(az állás most nem kérhető le: {e})"
    t = r.get("total") or 0
    counts = r.get("counts") or []
    rows = [f"  {o}: {counts[i] if i < len(counts) else 0} ({round(100 * (counts[i] if i < len(counts) else 0) / t) if t else 0}%)"
            for i, o in enumerate(p.get("options", []))]
    return f"👥 {t} szavazat\n" + "\n".join(rows)


def poll(out_dir: Path, ai=None, tz: Optional[ZoneInfo] = None, wait: int = 0) -> int:
    """Feldolgozza a Telegram-frissítéseket (wait > 0: ennyi mp-ig vár az új gombnyomásra).
    Visszatér: hány változás történt az oldalon (kirakás, csere, törlés).
    A várakozás zár nélkül megy (a párhuzamos tartalomgyártás közben menthet), a feldolgozás zárral."""
    if LOOP_THREAD and threading.get_ident() != LOOP_THREAD:
        return 0  # a figyelő szál folyamatosan kérdez – a tartalomgyártó szál ne vegye el előle a frissítéseket
    st0 = load_state()
    hook_before = st0.get("webhook")
    resp = _updates(st0, wait)
    with LOCK:
        st = load_state()
        for k in ("webhook", "offset"):
            if k in st0:
                st[k] = st0[k]
            else:
                st.pop(k, None)
        return _process(out_dir, ai, tz or ZoneInfo("Europe/Budapest"), resp, st, hook_before)


def _process(out_dir: Path, ai, tz: ZoneInfo, resp: dict, st: dict, hook_before) -> int:
    pending = load_pending()
    published, changed = 0, st.get("webhook") != hook_before
    for u in resp.get("result", []):
        st["offset"] = u["update_id"] + 1
        changed = True
        try:
            n_pub, n_ch = _handle(out_dir, ai, tz, u, st, pending)
        except Exception as e:  # noqa: BLE001 – egy hibás gombnyomás ne vesszen el csendben, és ne vigye el a többit
            log.exception("Telegram-frissítés feldolgozása sikertelen: %s", e)
            n_pub, n_ch = 0, False
            if st.get("chat_id"):
                tg("sendMessage", {"chat_id": st["chat_id"],
                                   "text": f"⚠️ Ezt most nem tudtam végrehajtani (hiba: {str(e)[:150]}). Próbáld újra."})
        published += n_pub
        changed = changed or n_ch
    return _finish(out_dir, tz, st, pending, published, changed)


def _handle(out_dir: Path, ai, tz: ZoneInfo, u: dict, st: dict, pending: list) -> tuple:
    published, changed = 0, False
    for _once in (1,):
        if "message" in u:
            m = u["message"]
            chat = m.get("chat", {})
            if not st.get("chat_id"):
                if chat.get("type") == "private":
                    st["chat_id"] = chat["id"]
                    tg("sendMessage", {"chat_id": chat["id"], "text": "✅ Összekötve.\n\n" + HELP})
                continue
            if chat.get("id") != st["chat_id"]:
                continue
            text = (m.get("text") or "").strip()
            rep = (m.get("reply_to_message") or {}).get("message_id")
            fid = ((m.get("photo") or [{}])[-1].get("file_id") or (m.get("sticker") or {}).get("file_id")
                   or ((m.get("document") or {}).get("file_id") if str((m.get("document") or {}).get("mime_type", "")).startswith("image/") else None))
            if fid and rep:  # kép vagy matrica válaszként az Instagram-előnézetre: ez lesz a kártya háttere
                import instagram
                pid = instagram.post_for_message(rep)
                if pid:
                    data = _download_file(fid)
                    res = instagram.set_background(pid, data) if data else "A képet nem tudtam letölteni."
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"], "text": res})
                    continue
            if rep and text and not text.startswith("/"):
                art = next((a for a in pending if rep in (a.get("review", {}).get("msg_ids", []) +
                                                          [a.get("review", {}).get("control_id"),
                                                           a.get("review", {}).get("title_prompt_id")])), None)
                is_title = art and rep == art["review"].get("title_prompt_id")
                if art and not is_title:
                    # nem egyértelmű (sima válasz, nem /k vagy /c parancs): rákérdezünk két gombbal
                    st.setdefault("choices", {})[str(m["message_id"])] = {"aid": art["id"], "text": text[:140]}
                    st["choices"] = dict(list(st["choices"].items())[-20:])
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": f"Mi legyen ezzel: „{text[:140]}”?",
                                       "reply_markup": {"inline_keyboard": [[
                                           {"text": "✏️ Legyen ez a cím", "callback_data": f"ch|t|{m['message_id']}"},
                                           {"text": "🖼 Képet keressek erre", "callback_data": f"ch|i|{m['message_id']}"}]]}})
                elif art:
                    art["review"]["custom_title"] = text[:140]
                    _refresh_control(st["chat_id"], art)
                    if art.get("live"):
                        _apply_live(out_dir, art, tz)
                        published += 1
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"Cím beállítva: {text[:140]}",
                                       "reply_to_message_id": m["message_id"]})
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "Ez a cikk már nincs függőben."})
            elif re.match(r"(?i)/(u|ujrairas|újraírás)\b", text):
                art = rep and next((a for a in pending if rep in (a.get("review", {}).get("msg_ids", []) +
                                                                  [a.get("review", {}).get("control_id")])), None)
                if not art:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": "Az újraíráshoz válaszolj a cikk valamelyik üzenetére ezzel: /u"})
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "🔁 Újraírás…", "reply_to_message_id": m["message_id"]})
                    new = _rewrite(art, ai, tz)
                    if new and art.get("live"):  # az élő cikk helyben cserélődik, az URL marad
                        for k in ("id", "slug", "url", "seo", "created_at", "published_at", "status"):
                            new[k] = art[k]
                        new["live"] = True
                        new["review"] = {"title": 0, "image": 0 if new.get("image_options") else -1}
                        _apply_live(out_dir, new, tz, content=True)
                        published += 1
                    if new:
                        pending[pending.index(art)] = new
                        _drop_old_messages(st["chat_id"], art)
                        new["resent"] = "rw"
                        send_article(out_dir, new)
                        changed = True
                    else:
                        tg("sendMessage", {"chat_id": st["chat_id"], "text": "Az újraírás most nem sikerült, próbáld később."})
            elif re.match(r"(?i)/(e|elo|elő|elohiv|előhív)\b", text):
                # kint lévő cikk teljes vezérlője újra (cím, kép, grafika, újraírás, törlés) – az oldalon kint marad
                qtxt = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
                live_arts = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
                hits = [a for a in live_arts if qtxt and (qtxt in (a.get("url") or "").lower() or qtxt in a.get("title", "").lower())]
                if len(hits) != 1:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": (
                        "Használat: /e <link vagy címrészlet> – a kint lévő cikk minden gombja újra (kint marad)" if not hits else
                        f"{len(hits)} cikk illik rá, pontosíts:\n" + "\n".join("• " + a["title"] for a in hits[:8]))})
                else:
                    h = hits[0]
                    item = next((p for p in pending if p.get("id") == h["id"]), None)
                    if not item:
                        item = {**h, "live": True, "title_options": [h["title"]], "clickbait_from": 1,
                                "image_options": [h["hero_image"]] if h.get("hero_image") else [],
                                "created_at": datetime.now(tz).isoformat(timespec="seconds")}
                        pending.append(item)
                    _expand(out_dir, item)
                    changed = True
            elif re.match(r"(?i)/(v|vissza)\b", text):
                # kint lévő cikk levétele + visszaküldése ide javításra (nem kerül ki magától, csak ✅ Kirakom után)
                qtxt = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
                live_arts = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
                hits = [a for a in live_arts if qtxt and (qtxt in (a.get("url") or "").lower() or qtxt in a.get("title", "").lower())]
                if len(hits) != 1:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": (
                        "Használat: /v <link vagy címrészlet> – leveszi az oldalról, és ide küldi javításra" if not hits else
                        f"{len(hits)} cikk illik rá, pontosíts:\n" + "\n".join("• " + a["title"] for a in hits[:8]))})
                else:
                    h = hits[0]
                    _apply_live(out_dir, h, tz, remove=True)
                    published += 1
                    for p in [p for p in pending if p.get("id") == h["id"]]:
                        _drop_old_messages(st["chat_id"], p)
                    pending[:] = [p for p in pending if p.get("id") != h["id"]]
                    back = {k: v for k, v in h.items() if k != "live"}
                    back["resent"] = "v"
                    back.update({"status": "needs_review", "hold": True, "title_options": _fresh_titles(ai, h), "clickbait_from": 3,
                                 "image_options": [h["hero_image"]] if h.get("hero_image") else [],
                                 "created_at": datetime.now(tz).isoformat(timespec="seconds")})
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"⏸ Levettem az oldalról (1–2 perc): {h['title']}\n"
                                       "Javítsd lent (cím, kép, /u újraírás), majd ✅ Kirakom – addig nem kerül ki magától."})
                    send_article(out_dir, back)
                    pending.append(back)
                    changed = True
            elif re.match(r"(?i)/(k|kep|kép|c|cim|cím)\b", text):
                # válaszként egy cikkre: /k <mit keressek> = új képek, /c <új cím> = saját cím
                cmd, _, arg = text.partition(" ")
                arg = arg.strip()
                art = rep and next((a for a in pending if rep in (a.get("review", {}).get("msg_ids", []) +
                                                                  [a.get("review", {}).get("control_id"),
                                                                   a.get("review", {}).get("title_prompt_id")])), None)
                if not art:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": "Válaszolj a cikk valamelyik üzenetére: /k <mit keressek> (új képek) vagy /c <új cím>."})
                elif cmd.lower() in ("/k", "/kep", "/kép"):
                    note = _refresh_images(st["chat_id"], art, ai, arg[:120])
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"🔄 {note}.", "reply_to_message_id": m["message_id"]})
                    changed = True
                elif not arg:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": "Írd a parancs után az új címet, pl.: /c Megszavazták a nyugdíjemelést"})
                else:
                    art["review"]["custom_title"] = arg[:140]
                    _refresh_control(st["chat_id"], art)
                    if art.get("live"):
                        _apply_live(out_dir, art, tz)
                        published += 1
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"✏️ Cím beállítva: {arg[:140]}",
                                       "reply_to_message_id": m["message_id"]})
                    changed = True
            elif re.match(r"(?i)/(j|jogi)\b", text):
                # válaszként egy cikkre: jogi ellenőrzés újra (pl. javítás után)
                art = rep and next((a for a in pending if rep in (a.get("review", {}).get("msg_ids", []) +
                                                                  [a.get("review", {}).get("control_id")])), None)
                if not art:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": "Válaszolj a cikk valamelyik üzenetére: /j (jogi ellenőrzés)."})
                else:
                    import legal
                    res = legal.review(ai or kc.AIClient(kc.Config.from_env()), art)
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": legal.summary(res) or "A jogi ellenőrzés ki van kapcsolva."})
                    _refresh_control(st["chat_id"], art)
                    changed = True
            elif re.match(r"(?i)/(g|grafika)\b", text):
                # válaszként egy cikkre: /g = generált grafika, /g <mit> = a megadott elképzelés szerint
                arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
                art = rep and next((a for a in pending if rep in (a.get("review", {}).get("msg_ids", []) +
                                                                  [a.get("review", {}).get("control_id"),
                                                                   a.get("review", {}).get("title_prompt_id")])), None)
                if not art:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": "Válaszolj a cikk valamelyik üzenetére: /g (grafika) vagy /g <mit rajzoljak>."})
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": "🎨 Grafikát készítek (kb. 30 mp)…"})
                    published += _make_graphic(st["chat_id"], out_dir, art, ai, tz, arg[:200])
                    changed = True
            elif re.match(r"(?i)/(kn|kepnelkul|képnélkül)\b", text):
                # a kint lévő, kép nélküli cikkek (utolsó 7 nap, max. 8) ide jönnek képválasztásra; kint maradnak,
                # a kiválasztott kép 1–2 percen belül él
                live_arts = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
                since = (datetime.now(tz) - timedelta(days=7)).date().isoformat()
                ids_p = {p.get("id") for p in pending}
                todo = [a for a in live_arts if not a.get("hero_image") and (a.get("date") or "") >= since
                        and a.get("id") not in ids_p][:8]
                tg("sendMessage", {"chat_id": st["chat_id"], "text": f"🖼 {len(todo)} kép nélküli cikk jön, képjelöltekkel." if todo
                                   else "Nincs kép nélküli cikk az elmúlt 7 napból."})
                for a in todo:
                    item = {**a, "live": True, "title_options": _fresh_titles(ai, a), "clickbait_from": 3, "image_options": []}
                    send_article(out_dir, item)
                    pending.append(item)
                    _refresh_images(st["chat_id"], item, ai)
                    changed = True
            elif re.match(r"(?i)/(csatornak|csatornák)\b", text):
                import videos
                lst = videos.load_sources()
                tg("sendMessage", {"chat_id": st["chat_id"], "text": "🎬 Figyelt csatornák:\n" + "\n".join(
                    f"• {x.get('name')}" + (f" (@{x['handle']})" if x.get("handle") else "") for x in lst)
                    + "\nÚj: /csatorna <YouTube-link vagy @név> · Törlés: /csatorna- <név>"})
            elif re.match(r"(?i)/csatorna-", text):
                import videos
                name = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
                lst = videos.load_sources()
                left = [x for x in lst if name not in (str(x.get("name", "")) + " " + str(x.get("handle", ""))).lower()] if name else lst
                videos.save_sources(left, [x.get("name", "") for x in lst if x not in left])
                changed = True
                tg("sendMessage", {"chat_id": st["chat_id"], "text": f"🗑 {len(lst) - len(left)} csatorna törölve."})
            elif re.match(r"(?i)/csatorna\b", text):
                import videos
                arg = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
                src = videos.parse_source(arg)
                mm = re.search(r"\b(\d{1,3})\s*(?:perc|p|min)\b", arg)
                if src and mm:  # pl. „/csatorna @valaki 3 perc” – ennél rövidebb videóról nem ír
                    src["min_minutes"] = int(mm.group(1))
                if src and videos.resolve_channel(src):
                    lst = videos.load_sources()
                    if not any(x.get("channel_id") == src["channel_id"] for x in lst):
                        lst.append(src)
                        videos.save_sources(lst)
                        changed = True
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"✅ Figyelem: {src['name']} – az új videóiból cikk jön jóváhagyásra."})
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": (
                        "Nem tudtam kiolvasni a csatornát (a YouTube néha nem engedi a szervernek). Próbáld a csatorna "
                        "youtube.com/channel/UC… linkjével, vagy küldd el az egyik videója linkjét: /csatorna <videólink>."
                        if src else "Használat: /csatorna https://www.youtube.com/@csatornanev (vagy @csatornanev)")})
            elif re.match(r"(?i)/(keret|limit)\b", text):
                tg("sendMessage", {"chat_id": st["chat_id"], "text": kc.usage_report()})
            elif re.match(r"(?i)/(t|torles|törlés)\b", text):
                # bármikor (a 48 órás szerkesztési idő után is) leszedhető egy kint lévő cikk: link vagy címrészlet alapján
                qtxt = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) > 1 else ""
                live_arts = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
                hits = [a for a in live_arts if qtxt and (qtxt in (a.get("url") or "").lower() or qtxt in a.get("title", "").lower())]
                if len(hits) == 1:
                    h = hits[0]
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"Biztosan leszedjem?\n{h['title']}\n{kc.SITE_URL}{h.get('url', '')}",
                                       "reply_markup": {"inline_keyboard": [[{"text": "🗑 Igen, leszedem", "callback_data": f"del|{h['id'][:12]}"},
                                                                            {"text": "Mégse", "callback_data": "del|x"}]]}})
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": (
                        "Használat: /torles <link vagy címrészlet>" if not hits else
                        f"{len(hits)} cikk illik rá, pontosíts:\n" + "\n".join("• " + a["title"] for a in hits[:8]))})
            elif re.match(r"(?i)/(stat|statisztika)\b", text):
                mm = re.search(r"\d+", text)
                tg("sendMessage", {"chat_id": st["chat_id"], "text": _stats_text(int(mm.group()) if mm else 7),
                                   "disable_web_page_preview": True})
            elif text.startswith("/szavazas") or text.startswith("/szavazás"):
                allp = kc.read_json(out_dir / "polls.json", {"polls": []}).get("polls", [])
                pl = [x for x in allp if (x.get("closes_at") or "") > datetime.now(tz).isoformat()]
                if not pl:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "Nincs nyitott szavazás."})
                old = [x for x in allp if x not in pl][:10]
                if old:  # lezárult szavazások eredménye egy üzenetben (csak neked; az oldalon 200 alatt nem látszik a létszám)
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "🗳 Lezárult szavazások:\n\n" + "\n\n".join(
                        f"{x.get('date', '')} · {x['question']}\n{_poll_counts(x)}" for x in old)})
                for x in pl[:5]:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"🗳 {x['question']}\n({x.get('article_title', '')})\n{_poll_counts(x)}",
                                       "reply_markup": {"inline_keyboard": [[{"text": "🗑 Szavazás leszedése",
                                                                             "callback_data": f"pdel|{x['id']}"}]]}})
            elif re.match(r"(?i)/(l|lista)\b", text):
                lines = [f"{'🟢' if a.get('live') else '⏳'} {kc.SECTIONS.get(a['category'], {}).get('name', '')}: "
                         f"{_chosen_title(a)}" for a in pending]
                waiting = [a for a in pending if not a.get("live")]
                tg("sendMessage", {"chat_id": st["chat_id"], "text": "\n".join(lines) or "Nincs függő cikk.",
                                   **({"reply_markup": {"inline_keyboard": [[{"text": f"📋 Váró cikkek ({len(waiting)})",
                                                                             "callback_data": "fall|x"}]]}} if waiting else {})})
            elif re.match(r"(?i)/(f|fuggo|függő|fuggok|függők)\b", text):
                changed = _resend_waiting(out_dir, st, pending) or changed
            elif text.startswith("/"):
                tg("sendMessage", {"chat_id": st["chat_id"], "text": HELP})
            elif text and not rep and not re.search(r"https?://|^(téma|tema|cikk)\s*:", text, re.I):
                tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                   "text": "Cikket linkből vagy témából tudok írni: küldj egy linket, vagy írd így: „téma: MNB kamatdöntés”."})
            elif text and not rep:
                # link vagy téma → cikk erről (jóváhagyásra jön, mint a többi)
                text = re.sub(r"(?i)^(téma|tema|cikk)\s*:\s*", "", text).strip()
                tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                   "text": "✍️ Megírom erről a cikket, 1–2 perc…"})
                try:
                    live_arts = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
                    new = kc.build_on_demand(ai or kc.AIClient(kc.Config.from_env()), text, tz, live_arts)
                except Exception as e:  # noqa: BLE001
                    log.warning("Kérésre írt cikk sikertelen: %s", e)
                    new = None
                if new:
                    pending.append(new)
                    send_article(out_dir, new)
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": m["message_id"],
                                       "text": "Ebből most nem sikerült cikket írni (nem találtam elég forrást). Próbáld linkkel."})
        elif "callback_query" in u:
            q = u["callback_query"]
            if (q.get("message") or {}).get("chat", {}).get("id") != st.get("chat_id"):
                continue
            parts = (q.get("data") or "").split("|")
            if parts[0] == "igno":  # Instagram-poszt letiltása (instagram.py)
                import instagram
                ok = instagram.cancel(parts[1])
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Nem megy ki." if ok else "Ez már kiment vagy nem várakozik."})
                continue
            if parts[0] == "fall":  # a váró cikkek újra a chat aljára, minden gombbal
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Előhozom a váró cikkeket…"})
                changed = _resend_waiting(out_dir, st, pending) or changed
                continue
            if parts[0] == "pw":  # téma-javaslatból cikk (a kör végi „További forró témák” listából)
                pit = kc.read_json(kc.PITCH_FILE, {"items": {}}).get("items", {}).get(parts[1])
                if not pit:
                    tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Ez a téma már nincs meg."})
                    continue
                if parts[1] in _PITCH_TAKEN:
                    tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Ezt már írom / megírtam."})
                    continue
                _PITCH_TAKEN.add(parts[1])
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Megírom, 1–2 perc…"})
                tg("sendMessage", {"chat_id": st["chat_id"], "text": f"✍️ Írom: {pit.get('title', '')} (1–2 perc)"})
                # külön szálon írja, hogy közben a többi gombnyomásra is azonnal reagáljon
                threading.Thread(target=_write_pitch, args=(out_dir, pit, tz, st["chat_id"]), daemon=False).start()
                continue
            if parts[0] == "ignow":  # Instagram-poszt azonnal
                import instagram
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Posztolom…"})
                res = instagram.post_now(parts[1])
                if res:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": res, "disable_web_page_preview": True})
                continue
            if parts[0] == "igdel":  # kint lévő Instagram-poszt leszedése
                import instagram
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Leszedem…"})
                res = instagram.delete_post(parts[1])
                if res != "Leszedve.":
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": res, "disable_web_page_preview": True})
                continue
            if parts[0] == "qdel":
                qpath = out_dir / "quizzes.json"
                qdata = kc.read_json(qpath, {"quizzes": []})
                left = [x for x in qdata.get("quizzes", []) if x.get("id") != parts[1]]
                if len(left) != len(qdata.get("quizzes", [])):
                    kc.write_json_atomic(qpath, {**qdata, "quizzes": left})
                    published += 1
                tg("editMessageText", {"chat_id": st["chat_id"], "message_id": q["message"]["message_id"],
                                       "text": "🗑 A kvíz lekerült az oldalról (1–2 perc)."})
                tg("answerCallbackQuery", {"callback_query_id": q["id"]})
                continue
            if parts[0] in ("pok", "pre", "pno"):  # napi szavazás-javaslat: választás / másik téma / ma nincs
                import polls
                dr = kc.read_json(polls.DRAFT_FILE, {})
                mid_ = q["message"]["message_id"]
                if dr.get("id") != parts[1] or dr.get("status") != "pending":
                    tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Ez a javaslat már nem aktuális."})
                    continue
                if parts[0] == "pok":
                    pl = polls.approve(dr, int(parts[2]), out_dir, tz)
                    published += 1
                    _deploy_now()
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": mid_,
                                           "text": f"🗳 Kint (1–2 perc): {pl['question']}\n" + " / ".join(pl["options"]),
                                           "reply_markup": {"inline_keyboard": [[{"text": "🗑 Szavazás leszedése",
                                                                                 "callback_data": f"pdel|{pl['id']}"}]]}})
                    tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Kirakva"})
                elif parts[0] == "pno":
                    dr["status"] = "skipped"
                    kc.write_json_atomic(polls.DRAFT_FILE, dr)
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": mid_, "text": "🗳 Ma nincs szavazás."})
                    tg("answerCallbackQuery", {"callback_query_id": q["id"]})
                else:
                    tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Másik témát keresek…"})
                    new_dr = polls.make_draft(ai or kc.AIClient(kc.Config.from_env()), date.fromisoformat(dr["date"]),
                                              out_dir, {dr.get("article_url")} | set(dr.get("tried") or []))
                    tg("deleteMessage", {"chat_id": st["chat_id"], "message_id": mid_})
                    if new_dr:
                        new_dr["tried"] = (dr.get("tried") or []) + [dr.get("article_url")]
                        polls.send_draft(new_dr)
                        kc.write_json_atomic(polls.DRAFT_FILE, new_dr)
                    else:
                        dr["status"] = "skipped"
                        kc.write_json_atomic(polls.DRAFT_FILE, dr)
                        tg("sendMessage", {"chat_id": st["chat_id"], "text": "🗳 Nem találtam másik alkalmas témát – ma nincs szavazás."})
                continue
            if parts[0] == "pdel":
                ppath = out_dir / "polls.json"
                pdata = kc.read_json(ppath, {"polls": []})
                left = [x for x in pdata.get("polls", []) if x.get("id") != parts[1]]
                if len(left) != len(pdata.get("polls", [])):
                    gone = [{"id": x.get("id"), "article_url": x.get("article_url"), "question": x.get("question")}
                            for x in pdata.get("polls", []) if x.get("id") == parts[1]]
                    kc.write_json_atomic(ppath, {**pdata, "polls": left, "removed": (pdata.get("removed") or [])[-99:] + gone})
                    published += 1
                tg("editMessageText", {"chat_id": st["chat_id"], "message_id": q["message"]["message_id"],
                                       "text": "🗑 A szavazás lekerült az oldalról (1–2 perc)."})
                tg("answerCallbackQuery", {"callback_query_id": q["id"]})
                continue
            if parts[0] == "ch" and len(parts) == 3:
                c = st.get("choices", {}).pop(parts[2], None)
                art = c and next((a for a in pending if a["id"] == c["aid"]), None)
                msg_id = q["message"]["message_id"]
                if not art:
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": msg_id, "text": "Ez a cikk már nincs függőben."})
                elif parts[1] == "t":
                    art["review"]["custom_title"] = c["text"]
                    _refresh_control(st["chat_id"], art)
                    if art.get("live"):
                        _apply_live(out_dir, art, tz)
                        published += 1
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": msg_id, "text": f"✏️ Cím beállítva: {c['text']}"})
                else:
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": msg_id, "text": f"🖼 Képet keresek: {c['text']}…"})
                    tg("answerCallbackQuery", {"callback_query_id": q["id"]})
                    note = _refresh_images(st["chat_id"], art, ai, c["text"][:120])
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": f"🔄 {note}."})
                    continue
                tg("answerCallbackQuery", {"callback_query_id": q["id"]})
                continue
            if parts[0] == "del":
                msg_id = q["message"]["message_id"]
                if parts[1] == "x":
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": msg_id, "text": "Rendben, marad."})
                else:
                    live_arts = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
                    h = next((a for a in live_arts if a.get("id", "").startswith(parts[1])), None)
                    if h:
                        _apply_live(out_dir, h, tz, remove=True)
                        st.setdefault("rejected_links", []).extend(h.get("category_meta", {}).get("source_links", []))
                        pending[:] = [p for p in pending if p.get("id") != h["id"]]
                        published += 1
                    tg("editMessageText", {"chat_id": st["chat_id"], "message_id": msg_id,
                                           "text": f"🗑 Leszedve (1–2 perc): {h['title']}" if h else "Ez a cikk már nincs kint."})
                tg("answerCallbackQuery", {"callback_query_id": q["id"]})
                continue
            art = next((a for a in pending if a["id"].startswith(parts[0])), None)
            if not art:
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Ez a cikk már nincs függőben."})
                continue
            act, note = parts[1] if len(parts) > 1 else "", ""
            if act == "t":
                art["review"]["title"] = int(parts[2])
                art["review"].pop("custom_title", None)
                _refresh_control(st["chat_id"], art)
                note = f"Cím {int(parts[2]) + 1}"
                if art.get("live"):
                    _apply_live(out_dir, art, tz)
                    published += 1
            elif act == "i":
                art["review"]["image"] = int(parts[2])
                _refresh_control(st["chat_id"], art)
                note = "Nincs kép" if int(parts[2]) < 0 else f"Kép {int(parts[2]) + 1}"
                if art.get("live"):
                    _apply_live(out_dir, art, tz)
                    published += 1
            elif act == "gen":
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Grafikát készítek (kb. 30 mp)…"})
                q = None
                published += _make_graphic(st["chat_id"], out_dir, art, ai, tz)
            elif act == "lfix":
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Javítom a jogi jelzések alapján…"})
                q = None
                import legal
                aic = ai or kc.AIClient(kc.Config.from_env())
                done = legal.fix(aic, art)
                if not done:
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": art["review"].get("control_id"),
                                       "text": "A jogi javítás most nem sikerült – próbáld újra, vagy /c és /u."})
                else:
                    res = legal.review(aic, art)
                    if art.get("live"):
                        _apply_live(out_dir, art, tz, content=True)
                        published += 1
                    changed = True
                    tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": art["review"].get("control_id"),
                                       "text": "⚖️ Javítva:\n" + "\n".join("• " + c for c in done.get("changes") or ["a kifogásolt részek"])
                                       + "\n\n" + (legal.summary(res) or "") + "\n\n📄 Teljes szöveg gombbal megnézheted."})
                    tg("deleteMessage", {"chat_id": st["chat_id"], "message_id": art["review"].get("control_id")})
                    text_, kb_ = _control(art)
                    art["review"]["control_id"] = _mid(tg("sendMessage", {"chat_id": st["chat_id"], "text": text_,
                                                                          "parse_mode": "HTML", "reply_markup": kb_}))
            elif act == "txt":
                note = "Teljes szöveg alább"
                for part in _chunks("\n\n".join(art.get("body", []))):
                    mid = _mid(tg("sendMessage", {"chat_id": st["chat_id"], "text": part}))
                    if mid:
                        art["review"].setdefault("msg_ids", []).append(mid)
            elif act == "img":
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Új képeket keresek…"})
                q = None
                _refresh_images(st["chat_id"], art, ai)
            elif act == "nt":  # új címjavaslatok (saját cím: /c válaszként)
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Új címeket írok…"})
                q = None
                cur = _chosen_title(art)
                new_t = _fresh_titles(ai, {**art, "title": cur}, 3)
                if len(new_t) > 1:
                    art["title_options"], art["clickbait_from"] = new_t, len(new_t)
                    art["review"]["title"] = 0
                    art["review"].pop("custom_title", None)
                    changed = True
                    mid = _mid(tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": art["review"].get("control_id"),
                                                  "text": "🆕 Új címek (az 1. a mostani):\n" + "\n".join(
                                                      f"{i + 1}) {t}" for i, t in enumerate(new_t))}))
                    if mid:
                        art["review"].setdefault("msg_ids", []).append(mid)
                    _refresh_control(st["chat_id"], art)
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "Most nem sikerült új címet írni, próbáld újra (vagy /c <cím>)."})
            elif act == "ct":
                note = "Írd be a címet"
                art["review"]["title_prompt_id"] = _mid(tg("sendMessage", {
                    "chat_id": st["chat_id"], "text": f"✏️ Írd be az új címet ehhez: {_chosen_title(art)}",
                    "reply_markup": {"force_reply": True, "input_field_placeholder": "Új cím"}}))
            elif act == "ok" and art.get("live"):
                # nem zárjuk le: PENDING_MAX_AGE_H óráig (alap: 48) még cserélhető a cím/kép, vagy törölhető
                note = "Rendben ✅"
                _compact(st["chat_id"], art, "✅ Kint")
            elif act == "ok":
                urgent = float(art.get("hot_score") or 0) >= float(os.getenv("PUBLISH_NOW_SCORE", "10"))
                when = None if art.get("scheduled_at") or urgent else _stagger_time(out_dir, pending, tz)  # rendkívüli hír: azonnal
                if when:  # röviddel az előző után: időzítve, hogy ne egyszerre kerüljön ki minden
                    art["scheduled_at"] = when.isoformat(timespec="seconds")
                    note = f"Időzítve: {when:%H:%M}"
                    _compact(st["chat_id"], art, f"🕒 Kint lesz {when:%H:%M} (✏️ → ✅ = azonnal)")
                else:
                    art.pop("scheduled_at", None)
                    _go_live(out_dir, art, tz)
                    published += 1
                    note = "Kirakva ✅ (1–2 perc múlva látszik)"
                    _compact(st["chat_id"], art, "✅ Kint")
            elif act == "ed":  # lezárt cikk újra előhozása (cím, kép, újraírás, törlés)
                note = "Előhozom…"
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": note})
                q = None
                _expand(out_dir, art)
            elif act == "no":
                pending.remove(art)
                st.setdefault("rejected_links", []).extend(art.get("category_meta", {}).get("source_links", []))
                if art.get("live"):
                    _apply_live(out_dir, art, tz, remove=True)
                    published += 1
                note = "Törölve" if art.get("live") else "Elvetve"
                if art.get("offtopic") and not art.get("live"):
                    import offtopic  # elvetett saját cikk helyett aznap új téma jön
                    if art.get("date") == datetime.now(tz).date().isoformat() and offtopic.allow_reroll(datetime.now(tz).date()):
                        note += " – új saját cikket írok (másik téma)"
                rv_ = art.get("review", {})
                for mid in (rv_.get("msg_ids") or []) + [rv_.get("title_prompt_id"), rv_.get("control_id"),
                                                          q["message"]["message_id"]]:
                    if mid:  # elvetett / törölt cikk: minden üzenete eltűnik a chatből, ne foglalja a helyet
                        tg("deleteMessage", {"chat_id": st["chat_id"], "message_id": mid})
            elif act == "rw":
                note = "Újraírás…"
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": note})
                q = None
                new = _rewrite(art, ai, tz)
                if new and art.get("live"):  # az élő cikk helyben cserélődik, az URL marad
                    for k in ("id", "slug", "url", "seo", "created_at", "published_at", "status"):
                        new[k] = art[k]
                    new["live"] = True
                    new["review"] = {"title": 0, "image": 0 if new.get("image_options") else -1}
                    _apply_live(out_dir, new, tz, content=True)
                    published += 1
                if new:
                    pending[pending.index(art)] = new
                    _drop_old_messages(st["chat_id"], art)
                    new["resent"] = "rw"
                    send_article(out_dir, new)
                else:
                    tg("sendMessage", {"chat_id": st["chat_id"], "text": "Az újraírás most nem sikerült, próbáld később."})
            if q:
                tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": note})
    return published, changed


def _finish(out_dir: Path, tz: ZoneInfo, st: dict, pending: list, published: int, changed: bool) -> int:
    # ingyenes keretek: figyelmeztetés csak USAGE_ALERTS=1 esetén (alapból ki; állapot: /keret)
    alert, today_s = kc.usage_alert(), datetime.now(tz).date().isoformat()
    if os.getenv("USAGE_ALERTS") == "1" and alert and st.get("usage_warned") != today_s and st.get("chat_id"):
        tg("sendMessage", {"chat_id": st["chat_id"], "text": alert})
        st["usage_warned"] = today_s
        changed = True
    if published:  # a te gombnyomásod / parancsod: soron kívül kikerül (nem a napi keretből)
        _deploy_now()
    # automatikus kirakás, ha AUTO_PUBLISH_MIN percen belül nem jött döntés
    # időzített (jóváhagyott, de röviddel az előző után jött) cikkek kirakása, ha eljött az idejük
    for a in [a for a in pending if a.get("scheduled_at") and not a.get("live")]:
        if a["scheduled_at"] <= datetime.now(tz).isoformat(timespec="seconds"):
            a.pop("scheduled_at", None)
            _go_live(out_dir, a, tz)
            published += 1
            changed = True
            a.setdefault("review", {})["compact"] = "✅ Kint"
            _refresh_control(st["chat_id"], a)
    if MODE == "hybrid":
        import offtopic
        now_local = datetime.now(tz)
        live_articles = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
        for a in [a for a in pending if not a.get("live") and not a.get("hold") and a.get("category") != "bulvar"]:
            if a.get("schedule"):
                if not offtopic.is_due(a, live_articles, now_local):
                    continue
                msg = "🗓 Csendesebb időszak van, ezért kiraktam a saját anyagot."
            elif AUTO_NEWS and _age_min(a) >= AUTO_PUBLISH_MIN:
                msg = f"⏱ Nem jött döntés {AUTO_PUBLISH_MIN} percen belül, ezért kiraktam."
            else:
                continue
            _go_live(out_dir, a, tz, auto=True)
            published += 1
            changed = True
            # magától kikerült (nem te hagytad jóvá): minden üzenete marad, hogy reggel is lásd; csak a vezérlő frissül
            _refresh_control(st["chat_id"], a)
            mid = _mid(tg("sendMessage", {"chat_id": st["chat_id"], "reply_to_message_id": a["review"].get("control_id"),
                                          "text": msg + " Utólag még cserélheted a címet/képet, vagy törölheted (✅ Rendben = lezárom)."}))
            if mid:
                a["review"].setdefault("msg_ids", []).append(mid)
    # lejárt függő cikkek
    limit = (datetime.now(tz) - timedelta(hours=PENDING_MAX_AGE_H)).isoformat()
    for a in [a for a in pending if (a.get("created_at") or "") < limit and (a.get("live") or MODE != "hybrid")]:
        pending.remove(a)  # az élő cikk kint marad, csak az ellenőrzés zárul le
        if not a.get("live"):
            st.setdefault("rejected_links", []).extend(a.get("category_meta", {}).get("source_links", []))
        changed = True
    if changed:
        save_state(st)
        save_pending(out_dir, pending)
    if published:
        PUBLISH_FLAG.write_text("1")
    return published


def _rewrite(art: dict, ai, tz: ZoneInfo) -> Optional[dict]:
    if ai is None:
        ai = kc.AIClient(kc.Config.from_env())
    if ai.enabled and art.get("offtopic_topic"):  # saját anyag: ugyanarról a témáról új változat
        import offtopic
        new = offtopic.build_article(ai, art["offtopic_topic"], date.fromisoformat(art["date"]), tz,
                                     {im["url"] for im in art.get("image_options") or []})
        if new and art.get("schedule"):
            new["schedule"] = art["schedule"]
        return new
    # a visszavett (/v) kint lévő cikknél nincs eltárolt „story”: a forráslistából rakjuk össze
    src = art.get("story") or [{"title": s.get("title") or "", "link": s["url"], "summary": "", "source": s.get("publisher") or "",
                                "published": art.get("date"), "categories": []} for s in art.get("sources") or [] if s.get("url")]
    if not ai.enabled or not src or art.get("category") not in kc.SECTIONS:
        return None
    story = [dict(s, kw=kc._keywords(s["title"] + " " + (s.get("summary") or "")[:200])) for s in src]
    new = kc.build_section_article(ai, kc.SECTIONS[art["category"]], date.fromisoformat(art["date"]), tz, story,
                                   {im["url"] for im in art.get("image_options") or []})
    if new:
        new["status"] = "pending"
    return new


def _save_to_git(label: str) -> None:
    """GitHub Actionsben menti és feltölti a változást (a build-keretet a scripts/commit.sh figyeli)."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        r = subprocess.run(["bash", "scripts/commit.sh", label], cwd=kc.BASE_DIR)
        if r.returncode:
            log.warning("A mentés nem sikerült (%s) – a következő körben újrapróbálom.", r.returncode)


def main(argv: Optional[list] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Kollektíva – Telegram-jóváhagyás")
    p.add_argument("--loop", type=int, default=0, help="ennyi másodpercig figyeli folyamatosan a gombnyomásokat")
    p.add_argument("--content", action="store_true",
                   help="közben, párhuzamos szálon a tartalomgyártás (kollektiva_content.py --if-due) is fut – így a "
                        "gombnyomásokra a cikkírás alatt is azonnal reagál")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    kc.load_dotenv(kc.BASE_DIR / ".env")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        log.warning("Nincs TELEGRAM_BOT_TOKEN – nincs mit tenni.")
        return 0
    cfg = kc.Config.from_env()
    tz = ZoneInfo(cfg.timezone)
    deadline = time.time() + max(0, args.loop)
    # ha az oldalsablon (kollektiva_content.py) változott, minden cikkoldal újraépül és soron kívül kikerül
    try:
        ver = hashlib.sha1((kc.BASE_DIR / "kollektiva_content.py").read_bytes()).hexdigest()[:12]
        vf = kc.BASE_DIR / "data" / "site_version"
        if not vf.exists() or vf.read_text().strip() != ver:
            kc.build_static_site(cfg.output_dir, tz)
            vf.write_text(ver)
            _deploy_now()
            _save_to_git("Oldalsablon frissítve")
            log.info("✔ Új oldalsablon: minden oldal újraépítve")
    except Exception as e:  # noqa: BLE001
        log.warning("Sablon-ellenőrzés kimaradt: %s", e)
    global LOOP_THREAD
    # „python telegram_review.py” esetén ez a modul __main__ néven fut; a többi modul (tartalom, Instagram) a
    # „telegram_review” nevet importálja – ugyanaz a példány kell (közös zár, közös figyelő-szál)
    sys.modules["telegram_review"] = sys.modules[__name__]
    worker = None
    if args.content:
        LOOP_THREAD = threading.get_ident()

        def _content() -> None:
            try:
                kc.main(["--if-due"])
            except Exception as e:  # noqa: BLE001
                log.exception("Tartalomgyártás hiba: %s", e)
        worker = threading.Thread(target=_content, name="tartalom", daemon=True)
        worker.start()
    last_ig = last_rt = 0.0
    while True:
        if time.time() - last_rt >= 300:  # 5 percenként: elmaradt ✍️ témák újrapróbálása + napi modell-ellenőrzés
            last_rt = time.time()
            try:
                _retry_pitches(cfg.output_dir, tz)
                _daily_model_check(tz)
            except Exception as e:  # noqa: BLE001
                log.warning("Újrapróbálás / modell-ellenőrzés kimaradt: %s", str(e)[:200])
        if time.time() - last_ig >= 120:  # esedékes Instagram-poszt percre pontosan (ne csak a következő futás elején)
            last_ig = time.time()
            try:
                import instagram
                if instagram.publish_pending():
                    _save_to_git("Instagram")
            except Exception as e:  # noqa: BLE001
                log.warning("Instagram-kör kimaradt: %s", str(e)[:200])
        before = (STATE_FILE.read_text() if STATE_FILE.exists() else "", PENDING_FILE.read_text() if PENDING_FILE.exists() else "")
        left = int(deadline - time.time())
        n = poll(cfg.output_dir, None, tz, wait=max(5 if worker and worker.is_alive() else 0, min(25, left)))
        if n:
            kc.build_static_site(cfg.output_dir, tz)
            log.info("✔ %d változás az oldalon", n)
        after = (STATE_FILE.read_text() if STATE_FILE.exists() else "", PENDING_FILE.read_text() if PENDING_FILE.exists() else "")
        if n or after != before or (kc.BASE_DIR / "data" / "deploy_pending").exists():
            _save_to_git("Jóváhagyás")  # a várakozó kirakást is itt küldi ki, amint lehet
        if time.time() >= deadline - 2 and not (worker and worker.is_alive()):
            break  # a figyelés addig tart, amíg a párhuzamos tartalomgyártás is be nem fejeződik
    return 0


if __name__ == "__main__":
    sys.exit(main())
