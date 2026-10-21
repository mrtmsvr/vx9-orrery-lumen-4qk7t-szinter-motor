#!/usr/bin/env python3
"""
Kollektíva – automatikus Instagram-posztolás (Instagram API with Instagram Login, Facebook-oldal nélkül)
=====================================================================================================

A legforróbb kirakott cikkekből (bulvár nélkül; IG_DAILY_MAX, alap 4/nap), legalább IG_GAP_H (alap 2,5) óra
különbséggel, 8 és 21:30 között – álló, 4:5-ös képkártyát készít (főkép vagy generált grafika + logó + rovat + cím),
és képaláírással (cím, lead, „link a bióban”, max. 5 hashtag – az Instagram 2025 vége óta ennyit enged) kiteszi.

Menete: 1) kártya + előnézet Telegramon „🚫 Ne menjen ki” gombbal, a kártya kikerül az oldalra
(az Instagram csak nyilvános URL-ről tölt le képet); 2) IG_DELAY_MIN (alap 45) perc múlva, ha nem tiltottad le
és a kártya már elérhető, posztol, és Telegramon jelez (a Telegram-figyelő 2 percenként nézi, nem késik).
Másik háttér: az előnézetre válaszként küldj egy képet vagy matricát – a kártya azzal újra elkészül.

Kell: IG_ACCESS_TOKEN (GitHub Secret). A token kb. 60 napig él: a robot hetente meghosszabbítja, és az új tokent
titkosítva (a TG_WEBHOOK_SECRET-ből képzett kulccsal) a data/ig_state.json-ba menti – így nem jár le. Ha a Secretet
kézzel lecseréled, onnantól az új számít. Ha a meghosszabbítás nem sikerül és közeleg a lejárat, Telegramon szól.
Kint lévő poszt leszedése: a „✅ Instagramon” üzenet alatti 🗑 gombbal (ha az API nem engedi, megírja, hol töröld kézzel).
Kézi futtatás: python instagram.py [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import kollektiva_content as kc

log = logging.getLogger("kollektiva.instagram")
API = "https://graph.instagram.com"
STATE = kc.BASE_DIR / "data" / "ig_state.json"
CARD_DIR = kc.BASE_DIR / "public" / "img" / "ig"
# Napi 16 automata poszt után az Instagram spam-védelme letiltotta a fiókot (2207051, „action is blocked”) –
# új fióknál a napi néhány poszt a biztonságos. A sikertelen posztok is számítanak a napi keretbe.
DAILY_MAX = int(os.getenv("IG_DAILY_MAX", "4"))
GAP_H = float(os.getenv("IG_GAP_H", "2.5"))
PAUSE_H = int(os.getenv("IG_PAUSE_H", "48"))  # tiltás után ennyi óráig nem próbálkozik (a próbálkozás meghosszabbítja)
DELAY_MIN = int(os.getenv("IG_DELAY_MIN", "45"))
W, H = 1080, 1350
NAVY, BRASS, PARCH = (14, 16, 36), (201, 164, 92), (236, 230, 216)
# a Kollektíva-logó (K + holdpálya), 260 px széles átlátszó PNG – a kártya jobb alsó sarkába kerül
LOGO_PNG = base64.b64decode("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")


def _env_token() -> str:
    return os.getenv("IG_ACCESS_TOKEN", "").strip()


def _fp(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()[:12]


def _key() -> bytes:
    return hashlib.sha256(("kollektiva-ig|" + os.getenv("TG_WEBHOOK_SECRET", "")).encode()).digest()


def _xor(data: bytes, nonce: bytes) -> bytes:
    out, k = bytearray(), _key()
    for i in range(0, len(data), 32):
        block = hashlib.sha256(k + nonce + i.to_bytes(8, "big")).digest()
        out += bytes(a ^ b for a, b in zip(data[i:i + 32], block))
    return bytes(out)


def _seal(t: str) -> dict:
    nonce = os.urandom(16)
    ct = _xor(t.encode(), nonce)
    mac = hmac.new(_key(), nonce + ct, "sha256").hexdigest()
    return {"n": base64.b64encode(nonce).decode(), "c": base64.b64encode(ct).decode(), "m": mac}


def _open(box: dict) -> str:
    try:
        nonce, ct = base64.b64decode(box["n"]), base64.b64decode(box["c"])
        if not hmac.compare_digest(hmac.new(_key(), nonce + ct, "sha256").hexdigest(), box["m"]):
            return ""
        return _xor(ct, nonce).decode()
    except Exception:  # noqa: BLE001
        return ""


def token() -> str:
    """A meghosszabbított (titkosítva tárolt) token, ha ugyanabból a Secretből származik; különben a Secret."""
    env = _env_token()
    tk = load_state().get("token") or {}
    if env and tk.get("box") and tk.get("from") == _fp(env) and os.getenv("TG_WEBHOOK_SECRET"):
        return _open(tk["box"]) or env
    return env


def api(path: str, params: dict, post: bool = False, method: str = "") -> dict:
    q = urllib.parse.urlencode({**params, "access_token": token()})
    url = f"{API}/{path.lstrip('/')}"
    if method:
        req = urllib.request.Request(f"{url}?{q}", method=method)
    else:
        req = urllib.request.Request(url, data=q.encode(), method="POST") if post else urllib.request.Request(f"{url}?{q}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Instagram API {e.code}: {body}") from None


def load_state() -> dict:
    return kc.read_json(STATE, {"posts": []})


def save_state(st: dict) -> None:
    st["posts"] = st.get("posts", [])[-200:]
    kc.write_json_atomic(STATE, st)


def _tg():
    try:
        import telegram_review as tr
        chat = tr.load_state().get("chat_id") if os.getenv("TELEGRAM_BOT_TOKEN") else None
        return (tr, chat) if chat else (None, None)
    except Exception:  # noqa: BLE001
        return None, None


def _notify(text: str) -> None:
    tr, chat = _tg()
    if tr:
        tr.tg("sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": True})


# ---------------------------------------------------------------------------
# Képkártya
# ---------------------------------------------------------------------------

def _pil():
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pillow"], check=False)
    from PIL import Image, ImageDraw, ImageFont  # noqa: F811
    return Image, ImageDraw, ImageFont


def _font(ImageFont, size: int, serif: bool = True):
    names = (["DejaVuSerif-Bold.ttf", "LiberationSerif-Bold.ttf", "NotoSerif-Bold.ttf"] if serif
             else ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf"])
    for root in ("/usr/share/fonts/truetype", "/usr/share/fonts"):
        for p in Path(root).rglob("*.ttf") if Path(root).exists() else []:
            if p.name in names:
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def _hero_bytes(art: dict) -> Optional[bytes]:
    hero = art.get("hero_image") or {}
    url = hero.get("url") or ""
    if url.startswith(kc.SITE_URL + "/"):
        p = kc.BASE_DIR / urllib.parse.unquote(url[len(kc.SITE_URL) + 1:])
        if p.exists():
            return p.read_bytes()
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers=kc.WIKI_UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        log.warning("Főkép letöltése sikertelen: %s", e)
        return None


def _wrap(draw, text: str, font, width: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _generated_bg(art: dict) -> Optional[bytes]:
    """Kép nélküli cikkhez: generált grafika a kártya hátterének."""
    try:
        gen = kc.generate_illustration(kc.AIClient(kc.Config.from_env()), art.get("title", ""), art.get("lead", ""),
                                       "ig", n=1)
        if gen:
            return Path(gen[0]["local"]).read_bytes()
    except Exception as e:  # noqa: BLE001
        log.warning("Instagram-háttérgrafika kimaradt: %s", str(e)[:200])
    return None


def make_card(art: dict, bg: Optional[bytes] = None, suffix: str = "") -> Path:
    Image, ImageDraw, ImageFont = _pil()
    img = Image.new("RGB", (W, H), NAVY)
    is_cover = bool((art.get("hero_image") or {}).get("cover"))  # márkás borító: a kártya maga a borító, ne duplázzunk
    raw = bg or (None if is_cover else (_hero_bytes(art) or _generated_bg(art)))
    if raw:
        try:
            ph = Image.open(io.BytesIO(raw)).convert("RGB")
            s = max(W / ph.width, H / ph.height)
            ph = ph.resize((int(ph.width * s) + 1, int(ph.height * s) + 1))
            img.paste(ph, ((W - ph.width) // 2, (H - ph.height) // 2))
        except Exception as e:  # noqa: BLE001
            log.warning("Főkép feldolgozása sikertelen: %s", e)
    # alsó sötét átmenet, hogy a cím olvasható legyen
    grad = Image.new("L", (1, H))
    for y in range(H):
        grad.putpixel((0, y), int(max(0, min(1, (y - H * 0.30) / (H * 0.55))) * 235))
    img.paste(Image.new("RGB", (W, H), NAVY), (0, 0), grad.resize((W, H)))
    d = ImageDraw.Draw(img)
    pad = 72
    # cím: a lehető legnagyobb betűvel, legfeljebb 5 sorban
    title = art.get("title", "")
    for size in (84, 76, 68, 60, 54, 48):
        f_title = _font(ImageFont, size)
        lines = _wrap(d, title, f_title, W - 2 * pad)
        if len(lines) <= 5:
            break
    lines = lines[:5]
    lh = int(size * 1.18)
    y = H - 150 - lh * len(lines)
    sec = (kc.SECTIONS.get(art.get("category"), {}) or {}).get("name") or ("Ekkor történt" if art.get("category") == "retro" else "")
    if sec:
        f_k = _font(ImageFont, 30, serif=False)
        d.text((pad, y - 62), sec.upper(), font=f_k, fill=BRASS)
        d.rectangle((pad, y - 18, pad + 90, y - 13), fill=BRASS)
    for i, ln in enumerate(lines):
        d.text((pad, y + i * lh), ln, font=f_title, fill=PARCH)
    f_logo = _font(ImageFont, 44)
    d.text((pad, H - 100), "Kollektíva", font=f_logo, fill=PARCH)
    lw = d.textlength("Kollektíva", font=f_logo)
    d.text((pad + lw + 2, H - 100), ".", font=f_logo, fill=BRASS)
    try:  # letisztult: lent balra a név, jobbra a logó (K + holdpálya) – a kép tetején nincs semmi
        logo = Image.open(io.BytesIO(LOGO_PNG)).convert("RGBA")
        lw2 = 118
        logo = logo.resize((lw2, int(lw2 * logo.height / logo.width)))
        img.paste(logo, (W - pad - lw2, H - 66 - logo.height // 2 - 12), logo)
    except Exception as e:  # noqa: BLE001
        log.warning("Logó kimaradt: %s", e)
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    out = CARD_DIR / (re.sub(r"[^a-z0-9-]", "", (art.get("slug") or art.get("id", "x")).lower())[:70] + suffix + ".jpg")
    img.save(out, "JPEG", quality=88, optimize=True)
    return out


def caption(art: dict) -> str:
    def tag(t: str) -> str:
        return "#" + re.sub(r"[^\wáéíóöőúüű]", "", str(t).lower().replace(" ", ""))
    sec = (kc.SECTIONS.get(art.get("category"), {}) or {}).get("name", "")
    # max. 5 hashtag (Instagram, 2025 dec. óta): 2 téma (a cikk címkéi: név, ügy) + a nagy magyar hírlapok által is
    # használt általános hírcímkék (rovatcímke + #hírek + #magyarország) – így a posztok ugyanazokra a hashtag-oldalakra
    # kerülnek, ahol az ő posztjaik is vannak. Más lapok márkanév-címkéje (#telex) nem: az az ő gyűjtőoldaluk, spamnek hat.
    sec_tag = {"kozelet": "#belföld", "vilag": "#külföld", "penzvilag": "#gazdaság", "tech": "#tudomány",
               "eletmod": "#egészség", "kultura": "#kultúra", "bulvar": "#sztárok"}.get(art.get("category"), "")
    base = [t for t in (sec_tag, "#hírek", "#magyarország") if t]
    topical = [tag(t) for t in (art.get("tags") or [])]
    topical = [t for i, t in enumerate(topical) if 2 < len(t) <= 30 and t not in topical[:i] and t not in base][:5 - len(base)]
    tags = topical + base
    text = (f"{art.get('title', '')}\n\n{art.get('lead', '')}\n\n"
            "👉 A teljes cikk a kollektíva.hu-n – link a bióban.\n\n" + " ".join(tags[:5]))
    return text[:2150]


# ---------------------------------------------------------------------------
# Ütemezés
# ---------------------------------------------------------------------------

def _paused(st: dict, now: datetime) -> bool:
    return (st.get("paused_until") or "") > now.isoformat()


def _block(st: dict, now: datetime, msg: str) -> None:
    """Az Instagram letiltotta az automatikus posztolást: PAUSE_H óra szünet, a sorban állók törlődnek, egyetlen jelzés."""
    st["paused_until"] = (now + timedelta(hours=PAUSE_H)).isoformat(timespec="seconds")
    for q in st.get("posts", []):
        if q.get("status") == "queued":
            q["status"] = "cancelled"
            q["error"] = "Instagram-tiltás miatt kimaradt"
    _notify(f"⛔ Az Instagram ideiglenesen letiltotta az automatikus posztolást (spam-védelem: „action is blocked”). "
            f"{PAUSE_H} órára leállítottam, a sorban álló posztokat töröltem; utána naponta legfeljebb {DAILY_MAX} poszt megy, "
            f"legalább {GAP_H:g} óra különbséggel. Ha az Instagram-appban rákérdez, jelezd, hogy tévedés "
            "(„Tell us if you think we made a mistake”).")
    log.warning("Instagram-tiltás: szünet %s-ig (%s)", st["paused_until"], msg[:120])


def _pick(articles: list, st: dict, now: datetime) -> Optional[dict]:
    if not (8 <= now.hour < 21 or (now.hour == 21 and now.minute < 30)) or _paused(st, now):
        return None
    posts = [p for p in st.get("posts", []) if p.get("status") != "cancelled"]
    today = now.date().isoformat()
    if sum(1 for p in posts if (p.get("created") or "").startswith(today)) >= DAILY_MAX:
        return None
    last = max((p.get("created") or "" for p in posts), default="")
    if last and datetime.fromisoformat(last) > now - timedelta(hours=GAP_H):
        return None
    done = {p.get("art_id") for p in st.get("posts", [])}
    for hours in (8, 20):  # elsősorban friss (8 óra); ha nincs, a napi anyagból (20 óra)
        fresh = (now - timedelta(hours=hours)).isoformat()
        cands = [a for a in articles if a.get("status") == "published" and a.get("id") not in done
                 and (a.get("published_at") or a.get("created_at") or "") >= fresh
                 and not (a.get("legal") or {}).get("hold") and a.get("category") not in ("horoscope", "bulvar")]
        if cands:  # képes cikk előnyben; kép nélkülihez generált grafika készül
            return max(cands, key=lambda a: (a.get("hot_score") or 0) + (2 if a.get("hero_image") else 0))
    return None


def enqueue(out_dir: Path, st: dict, now: datetime, dry: bool = False) -> bool:
    articles = kc.read_json(out_dir / "articles.json", {"articles": []}).get("articles", [])
    art = _pick(articles, st, now)
    if not art:
        return False
    card = make_card(art)
    cap = caption(art)
    pid = hashlib.sha1(art["id"].encode()).hexdigest()[:10]
    item = {"id": pid, "art_id": art["id"], "title": art.get("title"), "url": art.get("url"),
            "card": str(card.relative_to(kc.BASE_DIR)), "card_url": f"{kc.SITE_URL}/{card.relative_to(kc.BASE_DIR).as_posix()}",
            "caption": cap, "created": now.isoformat(timespec="seconds"),
            "due": (now + timedelta(minutes=DELAY_MIN)).isoformat(timespec="seconds"), "status": "queued"}
    log.info("Instagram-poszt sorban: %s", art.get("title"))
    if dry:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return True
    st.setdefault("posts", []).append(item)
    try:  # a kártya kerüljön ki az oldalra (az Instagram onnan tölti le)
        (kc.BASE_DIR / "data" / "deploy_now").write_text("1")
    except OSError:
        pass
    tr, chat = _tg()
    if tr:
        item["tg"] = _preview(tr, chat, item, card)
    return True


def _preview(tr, chat: int, item: dict, card: Path) -> list:
    r1 = tr._send_photo_file(chat, str(card), f"📸 Instagram-poszt kb. {DELAY_MIN} perc múlva:\n{item.get('title')}\n"
                                              "(Más háttér: válaszolj erre egy képpel vagy matricával.)"[:1000])
    r2 = tr.tg("sendMessage", {"chat_id": chat, "text": "Képaláírás:\n\n" + item.get("caption", "")[:3500],
                               "reply_markup": {"inline_keyboard": [[
                                   {"text": "🚫 Ne menjen ki", "callback_data": f"igno|{item['id']}"}]]}})
    return [m for m in (tr._mid(r1), tr._mid(r2)) if m]


def post_for_message(mid: int) -> Optional[str]:
    """Melyik várakozó poszt előnézetéhez tartozik ez a Telegram-üzenet."""
    for p in load_state().get("posts", []):
        if p.get("status") == "queued" and mid in (p.get("tg") or []):
            return p["id"]
    return None


def set_background(pid: str, data: bytes) -> str:
    """A chatben küldött kép/matrica lesz a kártya háttere; a kártya újra elkészül, és új előnézet jön."""
    st = load_state()
    p = next((x for x in st.get("posts", []) if x.get("id") == pid and x.get("status") == "queued"), None)
    if not p:
        return "Ez a poszt már nem várakozik."
    articles = kc.read_json(kc.Config.from_env().output_dir / "articles.json", {"articles": []}).get("articles", [])
    art = next((a for a in articles if a.get("id") == p.get("art_id")), None)
    if not art:
        return "A cikket nem találom."
    try:
        card = make_card(art, bg=data, suffix=f"-{int(time.time()) % 100000}")
    except Exception as e:  # noqa: BLE001
        return f"Ezzel a képpel nem sikerült a kártya: {str(e)[:120]}"
    now = datetime.now(ZoneInfo(kc.Config.from_env().timezone))
    p.update({"card": str(card.relative_to(kc.BASE_DIR)), "card_url": f"{kc.SITE_URL}/{card.relative_to(kc.BASE_DIR).as_posix()}",
              "due": max(p.get("due") or "", (now + timedelta(minutes=5)).isoformat(timespec="seconds"))})
    try:
        (kc.BASE_DIR / "data" / "deploy_now").write_text("1")
    except OSError:
        pass
    tr, chat = _tg()
    if tr:
        for mid in p.get("tg") or []:
            tr.tg("deleteMessage", {"chat_id": chat, "message_id": mid})
        p["tg"] = _preview(tr, chat, p, card)
    save_state(st)
    return "🖼 Új háttér kész – az új előnézet fent látszik."


def post_now(pid: str) -> str:
    """🚀 gomb: azonnal posztol (ha a kártya már kint van az oldalon; különben 1–2 percen belül, amint kikerül)."""
    st = load_state()
    p = next((x for x in st.get("posts", []) if x.get("id") == pid), None)
    if not p or p.get("status") != "queued":
        return "Ez a poszt már nem várakozik."
    now = datetime.now(ZoneInfo(kc.Config.from_env().timezone))
    p["due"] = now.isoformat(timespec="seconds")
    if not _live(p["card_url"]):
        save_state(st)
        return "A kártya még nem került ki az oldalra – amint kint van (1–3 perc), magától posztolom."
    publish_due(st, now, only=pid)
    save_state(st)
    return "" if p.get("status") == "posted" else f"Nem sikerült: {p.get('error', '')[:200]}"


def publish_pending() -> int:
    """A Telegram-figyelő ciklusából 2 percenként: az esedékes posztok kimennek (nem kell a következő futásig várni)."""
    if not token() or os.getenv("IG_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return 0
    st = load_state()
    now = datetime.now(ZoneInfo(kc.Config.from_env().timezone))
    if not any(p.get("status") == "queued" and (p.get("due") or "") <= now.isoformat() for p in st.get("posts", [])):
        return 0
    n = publish_due(st, now)
    save_state(st)
    return n or 1  # az állapot változhatott (pl. hibás poszt) – mentsük


def _collapse(p: dict, line: str, buttons: Optional[list] = None) -> None:
    """Az előnézet (kép + képaláírás) helyett egyetlen rövid sor marad a Telegramon."""
    tr, chat = _tg()
    if not tr:
        return
    for mid in p.get("tg") or []:
        tr.tg("deleteMessage", {"chat_id": chat, "message_id": mid})
    r = tr.tg("sendMessage", {"chat_id": chat, "text": line, "disable_web_page_preview": True,
                              **({"reply_markup": {"inline_keyboard": [buttons]}} if buttons else {})})
    p["tg"] = [m for m in [tr._mid(r)] if m]


def cancel(pid: str) -> bool:
    st = load_state()
    for p in st.get("posts", []):
        if p.get("id") == pid and p.get("status") == "queued":
            p["status"] = "cancelled"
            _collapse(p, f"🚫 Nem ment ki Instagramra: {p.get('title')}")
            save_state(st)
            return True
    return False


def delete_post(pid: str) -> str:
    """Kint lévő Instagram-poszt leszedése (az API 2025 vége óta engedi; ha mégsem, megírja, hol töröld kézzel)."""
    st = load_state()
    p = next((x for x in st.get("posts", []) if x.get("id") == pid), None)
    if not p or p.get("status") != "posted" or not p.get("media_id"):
        return "Ez a poszt nincs kint (vagy már leszedted)."
    try:
        api(p["media_id"], {}, method="DELETE")
    except Exception as e:  # noqa: BLE001
        log.warning("Instagram-törlés: %s", e)
        return ("Az Instagram nem engedte a törlést az API-n át – töröld kézzel az appban"
                + (f": {p['permalink']}" if p.get("permalink") else " (Profil → a poszt → ⋯ → Törlés).") + f"\n({str(e)[:160]})")
    p["status"] = "deleted"
    _collapse(p, f"🗑 Leszedve Instagramról: {p.get('title')}")
    save_state(st)
    return "Leszedve."


def _live(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 (KollektivaBot)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200 and "image" in (r.headers.get("content-type") or "")
    except Exception:  # noqa: BLE001
        return False


def publish_due(st: dict, now: datetime, only: str = "") -> int:
    n = 0
    if _paused(st, now):
        return 0
    for p in st.get("posts", []):
        if p.get("status") != "queued" or (p.get("due") or "") > now.isoformat() or (only and p.get("id") != only):
            continue
        if not _live(p["card_url"]):
            if datetime.fromisoformat(p["created"]) < now - timedelta(hours=6):
                p["status"] = "failed"
                p["error"] = "a kártya 6 óra alatt sem került ki az oldalra"
                _notify(f"⚠️ Instagram: nem ment ki ({p['error']}): {p.get('title')}")
            continue
        stage = "prep"
        try:
            # az Instagram API-kerete 0 követőnél nagyon szűk (a napi keret az elérésekkel arányos), ezért minimális hívás:
            # a fiók-azonosító elmentve, a feldolgozás figyelése ritkán, permalink-lekérés nincs
            uid = st.get("uid") or api("me", {"fields": "user_id"}).get("user_id")
            st["uid"] = uid
            cid = p.get("container")  # újrapróbálásnál ugyanazt a feltöltést tesszük ki (nincs dupla poszt)
            if not cid:
                cid = api(f"{uid}/media", {"image_url": p["card_url"], "caption": p["caption"]}, post=True).get("id")
                p["container"] = cid
            status = ""
            for _ in range(4):  # a feltöltött képet az Instagram feldolgozza (általában 10–30 mp)
                time.sleep(12)
                status = api(cid, {"fields": "status_code"}).get("status_code") or ""
                if status in ("FINISHED", "ERROR", "EXPIRED"):
                    break
            if status in ("ERROR", "EXPIRED"):
                p.pop("container", None)
                raise RuntimeError(f"az Instagram nem tudta feldolgozni a képet ({status})")
            if status != "FINISHED":  # még dolgozik rajta: a következő körben (2 perc) újra
                p["tries"] = p.get("tries", 0) + 1
                if p["tries"] < 5:
                    continue
            stage = "publish"
            r = api(f"{uid}/media_publish", {"creation_id": cid}, post=True)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            p["tries"] = p.get("tries", 0) + 1
            limit = "2207051" in msg or "request limit" in msg or '"code":4,' in msg
            if limit and stage == "publish":
                # tapasztalat (10.15–20.): a poszt ilyenkor KIMEGY, csak a válasz hibás → nem hiba, nem jelzünk
                p.update({"status": "posted", "unconfirmed": True, "posted": now.isoformat(timespec="seconds")})
                n += 1
                try:
                    _collapse(p, f"✅ Instagramon: {p.get('title')}")
                except Exception as e2:  # noqa: BLE001
                    log.warning("Instagram-jelzés kimaradt: %s", e2)
                continue
            if limit and p["tries"] < 5:  # a feltöltés előtt ütközött a keretbe: 40 perc múlva újra, jelzés nélkül
                p["due"] = (now + timedelta(minutes=40)).isoformat(timespec="seconds")
                log.info("Instagram: API-keret, később újra (%s)", p.get("title"))
                break
            if ("9007" in msg or "not ready" in msg or "is_transient\":true" in msg) and p["tries"] < 5:
                log.info("Instagram: a kép még nincs kész, később újra (%s)", p.get("title"))
                continue  # átmeneti hiba – a poszt várakozik tovább, a 2 perces körben újrapróbálja
            p["status"] = "failed"
            p["error"] = msg[:300]
            log.warning("Instagram-posztolás sikertelen: %s", e)
            _notify(f"⚠️ Instagram-posztolás sikertelen: {p.get('title')}\n{msg[:300]}")
            continue
        p.update({"status": "posted", "media_id": r.get("id"), "posted": now.isoformat(timespec="seconds")})
        n += 1
        try:
            _collapse(p, f"✅ Instagramon: {p.get('title')}" + (f"\n{p['permalink']}" if p.get("permalink") else ""),
                      [{"text": "🗑 Leszedés Instagramról", "callback_data": f"igdel|{p['id']}"}])
        except Exception as e:  # noqa: BLE001
            log.warning("Instagram-jelzés kimaradt: %s", e)
    return n


def check_token(st: dict, now: datetime) -> None:
    """Hetente (vagy új Secret után egy nap múlva) meghosszabbítja a tokent, és az újat titkosítva elmenti.
    Ha a lejárat 7 napon belül van (mert a meghosszabbítás nem megy), Telegramon szól."""
    env = _env_token()
    tk = st.setdefault("token", {})
    if tk.get("from") != _fp(env):  # új Secret: onnantól az számít
        st["token"] = tk = {"from": _fp(env), "first_seen": now.isoformat(timespec="seconds")}
    first = datetime.fromisoformat(tk["first_seen"])
    due = (tk.get("checked") or "") <= (now - timedelta(days=7)).isoformat() and first <= now - timedelta(hours=26)
    if due:
        tk["checked"] = now.isoformat(timespec="seconds")
        try:
            r = api("refresh_access_token", {"grant_type": "ig_refresh_token"})
            if r.get("access_token") and os.getenv("TG_WEBHOOK_SECRET"):
                tk["box"] = _seal(r["access_token"])
                tk["expires"] = (now + timedelta(seconds=int(r.get("expires_in") or 5184000))).isoformat(timespec="seconds")
                tk.pop("warned", None)
                log.info("Instagram-token meghosszabbítva: %s-ig", tk["expires"][:10])
        except Exception as e:  # noqa: BLE001
            log.warning("Instagram-token frissítése: %s", e)
    exp = tk.get("expires") or (first + timedelta(days=59)).isoformat()
    if exp < (now + timedelta(days=7)).isoformat() and not tk.get("warned"):
        tk["warned"] = True
        _notify(f"⏳ Az Instagram-token kb. {exp[:10]}-én lejár. Generálj újat (Meta → Kollektíva poster → API setup with "
                "Instagram login → Generate token), és cseréld a GitHub Secretben (IG_ACCESS_TOKEN).")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not token() or os.getenv("IG_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return 0
    cfg = kc.Config.from_env()
    now = datetime.now(ZoneInfo(cfg.timezone))
    st = load_state()
    if not args.dry_run:
        check_token(st, now)
        save_state(st)  # a token() a mentett állapotból olvas
        publish_due(st, now)
    enqueue(cfg.output_dir, st, now, args.dry_run)
    if not args.dry_run:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
