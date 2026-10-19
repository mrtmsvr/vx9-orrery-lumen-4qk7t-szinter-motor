#!/usr/bin/env bash
# Kollektíva robot – mentés és feltöltés a Cloudflare build-keret figyelésével.
# Havi keret: BUILD_BUDGET (alap 470 a 500-ból); a még megmaradt buildek a hónap hátralévő napjaira elosztva. Ha elfogyott a
# mai rész, a változás elmentődik [CF-Pages-Skip]-pel, és a következő szabad alkalommal kerül ki az oldalra.
# Két kirakás között legalább MIN_DEPLOY_GAP_MIN perc (alap 8), hogy a sok gombnyomás ne egyenként építsen.
set -u
LABEL="${1:-Robot}"
FLAG="data/deploy_pending"
git add -A public/ data/ 2>/dev/null || true
git add -A ads.txt index.html 2>/dev/null || true  # AdSense: a gyökérben

site_changed=0
if git diff --cached --name-only | grep -qv '^data/'; then site_changed=1; fi

deploy=0
# Közvetlen feltöltés (Wrangler, CLOUDFLARE_API_TOKEN): nem a havi 500-as Cloudflare build-keretből fogy, így nincs
# napi/havi adagolás – csak két kirakás között legalább WRANGLER_GAP_MIN perc (alap 3). A git-push ilyenkor mindig
# [CF-Pages-Skip], a Cloudflare nem épít belőle.
USE_WR=0
[ -n "${CLOUDFLARE_API_TOKEN:-}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ] && USE_WR=1
if [ "$USE_WR" = 1 ]; then
  if [ "$site_changed" = 1 ] || [ -f "$FLAG" ] || [ -f data/deploy_now ]; then
    last=$(cat data/last_deploy 2>/dev/null || echo 0)
    gap=$(( $(date +%s) - ${last:-0} ))
    if [ -f data/deploy_now ] || [ "$gap" -ge $(( ${WRANGLER_GAP_MIN:-3} * 60 )) ]; then
      deploy=1; rm -f "$FLAG" data/deploy_now; date +%s > data/last_deploy
    else
      touch "$FLAG"
    fi
    git add -A data/
  fi
elif [ "$site_changed" = 1 ] || [ -f "$FLAG" ] || [ -f data/deploy_now ]; then
  month_start=$(date -u +%Y-%m-01)
  day=$(date -u +%-d)
  dim=$(date -u -d "$month_start +1 month -1 day" +%-d)
  used=$(git log HEAD --since="${month_start}T00:00:00Z" --format=%s | grep -vc 'CF-Pages-Skip' || true)
  bot_today=$(git log HEAD --since="$(date -u +%F)T00:00:00Z" --author='kollektiva-bot' --format=%s | grep -vc 'CF-Pages-Skip' || true)
  left=$(( dim - day + 1 ))
  # a havi keretből még megmaradt buildek egyenletesen elosztva a hónap hátralévő napjaira (csak a robot mai kirakásai számítanak)
  allowed=$(( (${BUILD_BUDGET:-470} - ${used:-0} + ${bot_today:-0}) / left ))
  last=$(git log HEAD -1 --format=%ct --invert-grep --grep='CF-Pages-Skip' 2>/dev/null || echo 0)
  gap=$(( $(date +%s) - ${last:-0} ))
  if [ -f data/deploy_now ] && [ "$gap" -ge 60 ] && [ "${used:-0}" -lt "${BUILD_BUDGET:-470}" ]; then
    # levétel / törlés: azonnal kikerül, a napi rész nem tartja vissza (csak a havi keret)
    deploy=1; rm -f "$FLAG" data/deploy_now
  elif [ "$gap" -lt $(( ${MIN_DEPLOY_GAP_MIN:-8} * 60 )) ]; then
    echo "Az előző kirakás óta csak ${gap} mp telt el – ez a változás a következő körrel kerül ki."; touch "$FLAG"
  elif [ "${bot_today:-0}" -lt "$allowed" ] && [ "${used:-0}" -lt "${BUILD_BUDGET:-470}" ]; then
    deploy=1; rm -f "$FLAG"
  else
    echo "Build-keret: ma ${bot_today}/${allowed} (hónap: ${used}/${BUILD_BUDGET:-470}) – a változás később kerül ki."; touch "$FLAG"
  fi
  git add -A data/
fi

git diff --cached --quiet && exit 0
MSG="$LABEL: $(TZ=Europe/Budapest date '+%F %H:%M')"
if [ "$USE_WR" = 1 ] || [ "$deploy" != 1 ]; then MSG="$MSG [CF-Pages-Skip]"; fi

wr_deploy() {
  # csak a gitben követett fájlok mennek ki (a futás közbeni szemét, .env, __pycache__ nem)
  local dir; dir=$(mktemp -d)
  git archive HEAD | tar -x -C "$dir"
  if (cd "$dir" && npx --yes wrangler@3.114.0 pages deploy . --project-name="${CF_PAGES_PROJECT:-kollektiva}" \
        --branch=main --commit-dirty=true --commit-message="$MSG" >/tmp/wrangler.log 2>&1); then
    echo "Kirakva (Wrangler): $MSG"
  else
    echo "Wrangler-kirakás sikertelen – a következő mentésnél újrapróbálja:" >&2; tail -20 /tmp/wrangler.log >&2
    touch "$FLAG"; rm -f data/last_deploy
  fi
  rm -rf "$dir"
}
git commit -q -m "$MSG"
for i in 1 2 3 4; do
  # ütközésnél (két futás egyszerre írta a data/ állapotfájlokat) a mostani futás változata nyer – különben az egész
  # futás munkája elveszne és hibás futásról jönne e-mail
  if git pull -q --rebase --autostash -X theirs origin main && git push -q; then
    if [ "$deploy" = 1 ] && [ "$USE_WR" = 1 ]; then wr_deploy; fi
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  sleep $(( i * 5 ))
done
echo "A feltöltés nem sikerült." >&2
exit 1
