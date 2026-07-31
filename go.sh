#!/usr/bin/env bash
# ============================================================================
# Chipa — one-command setup.
#
#   ./go.sh push            commit + push the code to GitHub
#   ./go.sh test            run the full test suite locally
#   ./go.sh local           run Chipa on this machine (needs DATABASE_URL)
#   ./go.sh verify URL      check a deployed service is healthy
#   ./go.sh sync URL TOKEN  trigger the first real sync and report
#
# Nothing here needs a browser. Everything prints what it's doing.
# ============================================================================

set -uo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YEL=$'\033[33m'; OFF=$'\033[0m'

say()  { printf "%s==>%s %s\n" "$BOLD" "$OFF" "$*"; }
ok()   { printf "  %s✓%s %s\n" "$GRN" "$OFF" "$*"; }
bad()  { printf "  %s✗%s %s\n" "$RED" "$OFF" "$*"; }
warn() { printf "  %s!%s %s\n" "$YEL" "$OFF" "$*"; }

need_python() {
  command -v python3 >/dev/null 2>&1 || { bad "python3 not found"; exit 1; }
}

# ── push ────────────────────────────────────────────────────────────────────
cmd_push() {
  say "Pushing Chipa v2 to GitHub"

  # A partially-created .git can be left behind if the folder was touched
  # from a sandbox without write permission. Detect and reset it.
  if [ -d .git ] && ! git rev-parse --git-dir >/dev/null 2>&1; then
    warn "found a broken .git — resetting it"
    rm -rf .git || { bad "could not remove .git — run: rm -rf .git"; exit 1; }
  fi
  rm -f .git/index.lock 2>/dev/null

  if [ ! -d .git ]; then
    git init -q
    git branch -M main
    ok "initialised repo"
  fi

  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    git branch -M main 2>/dev/null
  fi

  if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin https://github.com/CueOne-Ux/chipa.git
    ok "added origin"
  fi

  git add -A
  if git diff --cached --quiet; then
    ok "nothing new to commit"
  else
    git commit -q -m "Chipa v2 — taxonomy matching, OCR catalogues, basket engine"
    ok "committed"
  fi

  say "Pushing (v2 branch, so v1 history stays intact)"
  if git push -u origin main:v2 2>&1 | tail -3; then
    ok "pushed to branch v2"
    echo
    echo "  Point Render at the ${BOLD}v2${OFF} branch."
  else
    bad "push failed — you'll need to authenticate"
    echo "  gh auth login       (if you have the GitHub CLI)"
    echo "  or use a personal access token as the password"
  fi
}

# ── test ────────────────────────────────────────────────────────────────────
cmd_test() {
  need_python
  say "Running the test suite"
  cd backend
  python3 -m pytest tests/ -q 2>&1 | tail -5
}

# ── local ───────────────────────────────────────────────────────────────────
cmd_local() {
  need_python
  say "Running Chipa locally"

  if [ -z "${DATABASE_URL:-}" ] && [ -f backend/.env ]; then
    set -a; . backend/.env; set +a
    ok "loaded backend/.env"
  fi

  if [ -z "${DATABASE_URL:-}" ]; then
    bad "DATABASE_URL is not set"
    echo
    echo "  Get the POOLED string from the Neon console (project Chipa ->"
    echo "  Connect), then either:"
    echo
    echo "    export DATABASE_URL='postgresql://...-pooler...'"
    echo "    export RAPIDAPI_KEY='...'"
    echo
    echo "  or put both in backend/.env and re-run."
    exit 1
  fi

  command -v tesseract >/dev/null 2>&1 \
    && ok "tesseract found (PDF OCR enabled)" \
    || warn "tesseract missing — PDF upload will fail. brew install tesseract"

  cd backend
  python3 -c "import fastapi" 2>/dev/null || {
    say "Installing dependencies"
    python3 -m pip install -q -r requirements.txt
  }

  say "Applying schema and syncing 2 pages (~100 products)"
  python3 -m app.cli sync 2 2>&1 | tail -20

  say "Starting the server"
  echo "  Open ${BOLD}http://localhost:8000${OFF}   (ctrl-c to stop)"
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
}

# ── verify ──────────────────────────────────────────────────────────────────
cmd_verify() {
  local url="${1:-}"
  [ -z "$url" ] && { bad "usage: ./go.sh verify https://your-service.onrender.com"; exit 1; }
  url="${url%/}"

  say "Checking $url"
  warn "a sleeping free-tier service takes 30-50s to answer — waiting"

  local body="" code=""
  for i in $(seq 1 8); do
    body=$(curl -s --max-time 30 "$url/health" 2>/dev/null)
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 "$url/health" 2>/dev/null)
    [ "$code" = "200" ] && break
    printf "  waking (%d/8)...\n" "$i"; sleep 10
  done

  if [ "$code" != "200" ]; then
    bad "no response from $url/health"
    exit 1
  fi

  ok "service is up"
  echo "$body" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  database:", "connected" if d.get("database") else "NOT CONNECTED — set DATABASE_URL")
print("  feed key:", "set" if d.get("feed") else "NOT SET — set RAPIDAPI_KEY")
' 2>/dev/null || echo "  $body"
}

# ── sync ────────────────────────────────────────────────────────────────────
cmd_sync() {
  local url="${1:-}" token="${2:-}"
  [ -z "$url" ] || [ -z "$token" ] && {
    bad "usage: ./go.sh sync https://your-service.onrender.com ADMIN_TOKEN"; exit 1; }
  url="${url%/}"

  cmd_verify "$url" || exit 1

  say "Triggering first sync (2 pages — a full sync is ~742 requests)"
  curl -s --max-time 600 -X POST \
    -H "X-Admin-Token: $token" \
    "$url/api/admin/sync?max_pages=2" \
  | python3 -m json.tool 2>/dev/null || echo "  (no JSON returned)"

  say "Status"
  curl -s --max-time 60 -H "X-Admin-Token: $token" "$url/api/admin/status" \
  | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("  could not read status"); raise SystemExit
c=d.get("counts") or {}
for k in ("store_products","canonical_products","links","links_needing_review","catalog_offers"):
    print(f"  {k:24} {c.get(k)}")
print(f"  last_sync                {c.get(\"last_sync\")}")
' 2>/dev/null

  echo
  ok "if store_products is non-zero, v2 works end to end"
  echo "  Open ${BOLD}$url${OFF}"
}

case "${1:-}" in
  push)   cmd_push ;;
  test)   cmd_test ;;
  local)  cmd_local ;;
  verify) shift; cmd_verify "$@" ;;
  sync)   shift; cmd_sync "$@" ;;
  *)      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
