// Cloudflare Pages Function: /api/kick – külső ébresztő a robotnak.
// Miért: a GitHub ütemezett futásai (cron */5) terhelés alatt órákig kimaradnak (10.27.: 11:22–15:38 és 16:24–20:38
// között egy futás sem volt, a 13:00-s jóváhagyási kör el is maradt). Egy ingyenes külső időzítő (pl. cron-job.org)
// 5 percenként meghívja ezt a címet, ez pedig elindítja a robotot (workflow_dispatch), ha épp nem fut.
// GET /api/kick  (ha a KICK_KEY környezeti változó be van állítva: /api/kick?k=<KICK_KEY>)
// Kell: GH_DISPATCH_TOKEN (ugyanaz, mint a Telegram-webhooknál), D1 binding: DB.
const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
});

export async function onRequestGet({ request, env }) {
  if (env.KICK_KEY && new URL(request.url).searchParams.get('k') !== env.KICK_KEY) return json({ ok: false }, 403);
  if (!env.DB || !env.GH_DISPATCH_TOKEN) return json({ ok: false, error: 'not_configured' }, 503);
  await env.DB.prepare('CREATE TABLE IF NOT EXISTS tg_meta (k TEXT PRIMARY KEY, v TEXT)').run();
  const now = Date.now();
  const last = await env.DB.prepare("SELECT v FROM tg_meta WHERE k = 'dispatched'").first();
  if (last && now - Number(last.v) < 4 * 60000) return json({ ok: true, status: 'recent' });  // max. 4 percenként
  await env.DB.prepare("INSERT OR REPLACE INTO tg_meta (k, v) VALUES ('dispatched', ?)").bind(String(now)).run();
  const repo = env.GH_REPO || 'mrtmsvr/vx9-orrery-lumen-4qk7t-szinter-motor';
  // ha már fut vagy sorban áll egy futás, nem indítunk újat (a workflow concurrency-csoportja amúgy is egyet futtat)
  const h = { authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`, accept: 'application/vnd.github+json', 'user-agent': 'kollektiva-kick' };
  try {
    const runs = await (await fetch(`https://api.github.com/repos/${repo}/actions/workflows/telegram.yml/runs?per_page=5`, { headers: h })).json();
    const busy = (runs.workflow_runs || []).filter(r => r.status === 'in_progress' || r.status === 'queued' || r.status === 'pending');
    if (busy.length >= 2 || busy.some(r => r.status !== 'in_progress')) return json({ ok: true, status: 'busy' });
  } catch (e) { /* ha nem olvasható, indítunk */ }
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/telegram.yml/dispatches`, {
    method: 'POST', headers: { ...h, 'content-type': 'application/json' }, body: JSON.stringify({ ref: 'main' }),
  });
  return json({ ok: r.status === 204, status: r.status === 204 ? 'dispatched' : `http_${r.status}` });
}
