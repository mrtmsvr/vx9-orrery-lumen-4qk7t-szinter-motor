#!/usr/bin/env python3
"""Heti Kollektíva – vasárnap reggel kiküldi a hírlevelet a Brevo-listának.

A robot 5 percenként hívja; csak vasárnap NEWSLETTER_HOUR (alap 8) óra után fut, és hetente egyszer
(data/robot.json: last_newsletter_week). Tartalom: AI-val írt heti összefoglaló a hét cikkeiből (témablokkok, a hét száma) + retro sztori;
ha az AI nem elérhető, a hét 5 legforróbb cikke.
Kell hozzá: BREVO_API_KEY (GitHub Secret). Feladó: NEWSLETTER_SENDER_EMAIL, vagy a Brevo első hitelesített feladója.
Kézi próba: python scripts/newsletter.py --force [--test cim@pelda.hu]
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kollektiva_content as kc  # noqa: E402

log = logging.getLogger("kollektiva.newsletter")
API = "https://api.brevo.com/v3"
LIST_NAME = "Kollektíva hírlevél"
RUNS = kc.BASE_DIR / "data" / "robot.json"
E = lambda t: html.escape(str(t or ""))  # noqa: E731


def api_key() -> str:
    k = os.getenv("BREVO_API_KEY", "").strip()
    if k and not k.startswith("xkeysib-"):  # az „MCP”-kulcs base64-be csomagolt JSON
        try:
            k = json.loads(base64.b64decode(k + "===").decode()).get("api_key", k)
        except (ValueError, UnicodeDecodeError):
            pass
    return k


def brevo(path: str, payload: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(API + path, method=method or ("POST" if payload is not None else "GET"),
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"api-key": api_key(), "accept": "application/json",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode() or "{}"
            return r.status, json.loads(body)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}


def list_id() -> int | None:
    if os.getenv("BREVO_LIST_ID"):
        return int(os.environ["BREVO_LIST_ID"])
    _, data = brevo("/contacts/lists?limit=50&offset=0")
    for l in data.get("lists", []):
        if l.get("name") == LIST_NAME:
            return l["id"]
    _, f = brevo("/contacts/folders?limit=10&offset=0")
    folder = (f.get("folders") or [{}])[0].get("id") or brevo("/contacts/folders", {"name": "Kollektíva"})[1].get("id")
    return brevo("/contacts/lists", {"name": LIST_NAME, "folderId": folder})[1].get("id")


def sender() -> dict | None:
    if os.getenv("NEWSLETTER_SENDER_EMAIL"):
        return {"name": "Kollektíva", "email": os.environ["NEWSLETTER_SENDER_EMAIL"]}
    code, data = brevo("/senders")
    act = [s for s in data.get("senders", []) if s.get("active")]
    if not act:
        log.warning("Brevo /senders: HTTP %s %s", code, str(data)[:300])
        # a hitelesített domain feladója akkor is használható, ha a listában nem „aktív” (a kampány-API szól, ha mégsem)
        return {"name": "Kollektíva", "email": os.getenv("NEWSLETTER_FALLBACK_SENDER", "szerkesztoseg@xn--kollektva-m5a.hu")}
    # a saját domaines feladó (szerkesztoseg@kollektíva.hu) az első – a Gmailes feladó gyakrabban megy spambe
    act.sort(key=lambda s: 0 if s.get("email", "").lower().endswith(("@xn--kollektva-m5a.hu", "@kollektíva.hu")) else 1)
    return {"name": "Kollektíva", "email": act[0]["email"]} if act else None


NL_SYSTEM = ("A Kollektíva magyar hírportál heti hírlevelének szerkesztője vagy. A hét cikkeiből érthető, "
             "pártatlan heti összefoglalót írsz. Csak a megadott cikkekben szereplő tényeket használhatod, nem találsz "
             "ki számot, nevet, idézetet. Természetes, élő magyar nyelv, töltelékmondatok nélkül. Csak JSON-t adsz vissza.")


def week_articles(tz: ZoneInfo) -> tuple[list, dict | None]:
    out = kc.Config.from_env().output_dir
    since = (datetime.now(tz) - timedelta(days=7)).isoformat()
    arts = [a for a in kc.read_json(out / "articles.json", {"articles": []}).get("articles", [])
            if (a.get("created_at") or "") >= since and a.get("status") == "published" and a.get("url")]
    arts.sort(key=lambda a: (a.get("hot_score") or 0), reverse=True)
    retro = next((a for a in kc.read_json(out / "retro_articles.json", {"articles": []}).get("articles", [])
                  if (a.get("created_at") or "") >= since), None)
    return arts, retro


def ai_summary(arts: list) -> dict | None:
    """A hét összefoglalója: bevezető + témablokkok (Itthon, Világ, Pénz, Tudomány és tech, Életmód és kultúra)
    + a hét száma. Az AI csak a cikkekből dolgozik; hiba esetén None (akkor a régi, egyszerű lista megy ki)."""
    try:
        ai = kc.AIClient(kc.Config.from_env())
        if not ai.enabled or not arts:
            return None
        lst = "\n".join(f"[{i}] ({kc.SECTIONS.get(a.get('category'), {}).get('name', a.get('category'))}, "
                        f"{a.get('date', '')}, forróság {a.get('hot_score') or 0}) {a.get('title')} – {a.get('lead') or ''} "
                        f"| {' | '.join(a.get('key_points') or [])[:400]}" for i, a in enumerate(arts[:60]))
        prompt = (f"A hét cikkei:\n{lst}\n\nÍrd meg a heti összefoglalót. Szabályok: 6–9 esemény összesen; előbb a "
                  "legfontosabb közéleti, világ- és gazdasági ügyek, aztán tudomány/tech, végül életmód/kultúra; bulvár "
                  "legfeljebb 1, és csak ha nagy ügy; ha ugyanarról az ügyről több cikk szól (többnapos sztori), vond "
                  "össze egy eseménnyé, és a legfrissebb cikk számát add meg. Minden eseménynél: rövid, KONKRÉT cím "
                  "(ki/mi, hol), és 2–3 mondat: mi történt, miért számít. "
                  'JSON: {"intro": "2–3 mondat: milyen hét volt, mi volt a fő téma", '
                  '"blocks": [{"heading": "Itthon | Világ | Pénz | Tudomány és tech | Életmód és kultúra", '
                  '"items": [{"idx": cikk száma, "title": "...", "summary": "..."}]}], '
                  '"number": {"value": "egy meglepő szám a cikkekből (pl. 24 milliárd)", "text": "egy mondat, mit jelent"} vagy null}')
        r = ai.complete_json(NL_SYSTEM, prompt, 4000)
        blocks = []
        for bl in r.get("blocks") or []:
            items = [it for it in bl.get("items") or [] if isinstance(it, dict) and isinstance(it.get("idx"), int)
                     and 0 <= it["idx"] < len(arts) and it.get("title") and it.get("summary")]
            if items:
                blocks.append({"heading": str(bl.get("heading") or "")[:40], "items": items})
        if not blocks or not r.get("intro"):
            return None
        num = r.get("number") if isinstance(r.get("number"), dict) and r["number"].get("value") else None
        return {"intro": str(r["intro"]), "blocks": blocks, "number": num}
    except Exception as e:  # noqa: BLE001 – a hírlevél ettől még kimegy (egyszerű listával)
        log.warning("Heti összefoglaló (AI) kimaradt: %s", e)
        return None


def build(tz: ZoneInfo) -> tuple[str, str]:
    now = datetime.now(tz)
    arts, retro = week_articles(tz)
    site = kc.SITE_URL
    utm = "?utm_source=hirlevel&utm_medium=email&utm_campaign=heti-" + now.strftime("%G-%V")
    link = lambda a: f"{site}{E(a['url'])}{utm}"  # noqa: E731
    img = lambda a: (f'<a href="{link(a)}"><img src="{E(a["hero_image"]["url"])}" width="560" alt="" '  # noqa: E731
                     'style="width:100%;max-width:560px;height:auto;border-radius:10px;display:block;margin:0 0 12px"></a>'
                     if (a.get("hero_image") or {}).get("url") else "")
    card = lambda a: (  # noqa: E731
        f'<tr><td style="padding:0 0 22px">{img(a)}'
        f'<div style="font:600 12px Arial,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#A8853F">'
        f'{E(kc.SECTIONS.get(a.get("category"), {}).get("name", "Retro"))}</div>'
        f'<a href="{link(a)}" style="font:700 22px Georgia,serif;color:#0E1024;text-decoration:none;'
        f'line-height:1.25">{E(a["title"])}</a>'
        f'<p style="font:15px/1.55 Arial,sans-serif;color:#3b3d58;margin:8px 0 0">{E(a.get("lead"))}</p></td></tr>')
    summ = ai_summary(arts)
    if summ:
        first = arts[summ["blocks"][0]["items"][0]["idx"]]
        parts = [f'<tr><td style="padding:0 0 16px;font:16px/1.6 Arial,sans-serif;color:#3b3d58">{E(summ["intro"])}</td></tr>']
        if img(first):
            parts.append(f'<tr><td style="padding:0 0 6px">{img(first)}</td></tr>')
        for bl in summ["blocks"]:
            parts.append(f'<tr><td style="padding:14px 0 8px;font:700 13px Arial,sans-serif;letter-spacing:.14em;'
                         f'text-transform:uppercase;color:#A8853F;border-top:1px solid #eee">{E(bl["heading"])}</td></tr>')
            for it in bl["items"]:
                a = arts[it["idx"]]
                parts.append(f'<tr><td style="padding:0 0 16px"><a href="{link(a)}" style="font:700 19px/1.3 Georgia,serif;'
                             f'color:#0E1024;text-decoration:none">{E(it["title"])}</a>'
                             f'<p style="font:15px/1.55 Arial,sans-serif;color:#3b3d58;margin:6px 0 0">{E(it["summary"])} '
                             f'<a href="{link(a)}" style="color:#A8853F;text-decoration:none;font-weight:600">Tovább →</a></p></td></tr>')
        if summ["number"]:
            n = summ["number"]
            parts.append(f'<tr><td style="padding:8px 0 20px"><div style="background:#0E1024;border-radius:12px;padding:18px 20px">'
                         f'<div style="font:600 11px Arial,sans-serif;letter-spacing:.16em;color:#9492B3">A HÉT SZÁMA</div>'
                         f'<div style="font:700 34px Georgia,serif;color:#C9A45C;margin:6px 0 4px">{E(n["value"])}</div>'
                         f'<div style="font:14px/1.5 Arial,sans-serif;color:#ECE6D8">{E(n.get("text"))}</div></div></td></tr>')
        items = "".join(parts)
        subject = f"A hét röviden: {summ['blocks'][0]['items'][0]['title']}"
    else:
        top, per = [], {}
        for a in arts:  # változatos: rovatonként legfeljebb 2 cikk
            if per.get(a.get("category"), 0) < 2:
                top.append(a)
                per[a.get("category")] = per.get(a.get("category"), 0) + 1
            if len(top) == 5:
                break
        items = "".join(card(a) for a in top)
        subject = f"Heti Kollektíva: {top[0]['title']}" if top else "Heti Kollektíva"
    retro_html = (f'<tr><td style="padding:10px 0 4px;font:700 13px Arial,sans-serif;color:#0E1024">'
                  f'EKKOR TÖRTÉNT – A HÉT RETRO SZTORIJA</td></tr>{card({**retro, "category": "retro"})}') if retro else ""
    more = (f'<tr><td align="center" style="padding:6px 0 18px"><a href="{site}/{utm}" style="display:inline-block;'
            f'background:#0E1024;color:#ECE6D8;font:600 14px Arial,sans-serif;text-decoration:none;border-radius:999px;'
            f'padding:12px 22px">Minden friss hír a kollektíva.hu-n</a></td></tr>')
    body = f"""<!doctype html><html><body style="margin:0;background:#ECE6D8">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ECE6D8"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border-radius:14px">
<tr><td style="background:#0E1024;border-radius:14px 14px 0 0;padding:22px 20px;font:700 26px Georgia,serif;color:#ECE6D8">
Kollektíva<span style="color:#C9A45C">.</span><div style="font:13px Arial,sans-serif;color:#9492B3;margin-top:4px">
Heti Kollektíva · a hét röviden · {now.strftime('%Y.%m.%d.')}</div></td></tr>
<tr><td style="padding:24px 20px 4px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">
{items}{retro_html}{more}</table></td></tr>
<tr><td style="padding:14px 20px 22px;font:12px/1.5 Arial,sans-serif;color:#8a8aa3;border-top:1px solid #eee">
Azért kapod ezt a levelet, mert feliratkoztál a Heti Kollektívára a kollektíva.hu oldalon.<br>
<a href="{{{{ unsubscribe }}}}" style="color:#8a8aa3">Leiratkozás</a></td></tr>
</table></td></tr></table></body></html>"""
    return subject[:150], body


def _tg(text: str) -> None:
    try:
        import telegram_review as tr
        chat = tr.load_state().get("chat_id")
        if chat and os.getenv("TELEGRAM_BOT_TOKEN"):
            tr.tg("sendMessage", {"chat_id": chat, "text": text})
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram-jelzés kimaradt: %s", e)


def _failed(runs: dict, week: str, fails: int, why: str, args) -> int:
    """Hiba: Telegram-jelzés (hetente az első és a harmadik, utolsó próbánál), legfeljebb 3 próba hetente."""
    log.warning("Hírlevél sikertelen: %s", why)
    if args.force:
        return 0
    fails += 1
    kc.write_json_atomic(RUNS, {**kc.read_json(RUNS, {}), "newsletter_fail": {week: fails}})
    if fails in (1, 3):
        _tg(f"⚠️ A heti hírlevél nem ment ki ({fails}. próba{', többet ezen a héten nem próbálom' if fails == 3 else ', később újrapróbálom'}): {why}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="most azonnal (nem csak vasárnap)")
    p.add_argument("--test", help="próbalevél erre a címre (a lista helyett)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not api_key():
        return 0
    tz = ZoneInfo(kc.Config.from_env().timezone)
    now = datetime.now(tz)
    runs = kc.read_json(RUNS, {})
    week = now.strftime("%G-W%V")
    if not args.force and not (now.weekday() == 6 and now.hour >= int(os.getenv("NEWSLETTER_HOUR", "8"))):
        return 0
    if not args.force and runs.get("last_newsletter_week") == week:
        return 0
    fails = (runs.get("newsletter_fail") or {}).get(week, 0)
    if not args.force and fails >= 3:
        return 0  # ezen a héten 3-szor nem sikerült – nem próbálkozik tovább (szólt Telegramon)
    lid, snd = list_id(), sender()
    if not lid or not snd:
        return _failed(runs, week, fails, f"nincs Brevo-lista ({lid}) vagy hitelesített feladó ({snd})", args)
    subject, body = build(tz)
    code, camp = brevo("/emailCampaigns", {
        "name": f"Heti Kollektíva {week}", "subject": subject, "sender": snd, "htmlContent": body,
        "recipients": {"listIds": [lid]}, "inlineImageActivation": False})
    if code >= 300:
        return _failed(runs, week, fails, f"kampány létrehozása: HTTP {code} {str(camp)[:200]}", args)
    if args.test:
        code, r = brevo(f"/emailCampaigns/{camp['id']}/sendTest", {"emailTo": [args.test]})
    else:
        code, r = brevo(f"/emailCampaigns/{camp['id']}/sendNow", {})
    log.info("Hírlevél (%s): HTTP %s %s", "teszt" if args.test else "kiküldve", code, r if code >= 300 else "")
    if code >= 300:
        brevo(f"/emailCampaigns/{camp['id']}", method="DELETE")  # ne gyűljenek a piszkozatok
        return _failed(runs, week, fails, f"kiküldés: HTTP {code} {str(r)[:200]}", args)
    if not args.test:
        kc.write_json_atomic(RUNS, {**kc.read_json(RUNS, {}), "last_newsletter_week": week})
        _, info = brevo(f"/contacts/lists/{lid}")
        _tg(f"📧 Kiment a heti hírlevél ({info.get('uniqueSubscribers') or info.get('totalSubscribers') or '?'} feliratkozó): {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
