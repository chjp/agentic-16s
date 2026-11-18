#!/usr/bin/env bash
# Every automatic check in one place (layers L0-L1 of docs/ci-cd-plan.md). GitHub Actions will run
# this same script, so a clean run here should mean a clean CI run.
#
#   scripts/check.sh           all checks
#   scripts/check.sh --fast    skip the ML tests (they take about a minute)
#
# Needs only uv; versions come from uv.lock. terraform is optional: without it the infra checks are
# skipped with a note. The database tests in ml/tests run when DATABASE_URL (.env) reaches
# PostgreSQL, and skip otherwise (CI gives them a Postgres container).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

# The standard-library code (pipeline/bin, infra/*.py) must run on both of these: the Mac's system
# Python and the GCE worker's Ubuntu 24.04. Add Google Batch's Debian Python once its probe job has run.
PY_MAC=3.9
PY_VM=3.12

failed=()
skipped=()
run() {
  local name=$1 t0=$SECONDS
  shift
  printf '\n== %s\n' "$name"
  if "$@"; then
    printf -- '-- ok: %s (%ss)\n' "$name" $((SECONDS - t0))
  else
    printf -- '-- FAILED: %s\n' "$name"
    failed+=("$name")
  fi
}
skip() {
  printf '\n== %s\n-- skipped: %s\n' "$1" "$2"
  skipped+=("$1")
}
# A second environment for the older Python, so switching versions never rebuilds .venv
uv_mac() { UV_PROJECT_ENVIRONMENT=.venv-py39 uv run --locked --python "$PY_MAC" "$@"; }
uv_vm() { uv run --locked --python "$PY_VM" "$@"; }

syntax_check() {
  # compile() without writing .pyc files
  # progress/ is gitignored, so it is checked only where it exists (the Mac, not CI)
  "$@" python - <<'PY'
import glob, sys
files = sorted(glob.glob("pipeline/bin/*.py") + glob.glob("infra/*.py") + glob.glob("progress/*.py")
               + glob.glob("discovery/*.py"))
sys.argv[1:] = files
bad = 0
for f in files:
    try:
        compile(open(f, encoding="utf-8").read(), f, "exec")
    except SyntaxError as e:
        print(f"{f}:{e.lineno}: {e.msg}")
        bad += 1
print(f"{len(sys.argv) - 1} files compiled on Python {sys.version.split()[0]}, {bad} with syntax errors")
sys.exit(1 if bad else 0)
PY
}

json_check() {
  python3 - <<'PY'
import glob, json, sys
files = sorted(set(glob.glob("studies/*/*.json")) | set(glob.glob("progress/plan.json")))
bad = 0
for f in files:
    try:
        json.load(open(f, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"{f}: {e}")
        bad += 1
print(f"{len(files)} JSON files parsed, {bad} invalid")
sys.exit(1 if bad else 0)
PY
}

# The repo is private now and will be public later, so nothing personal may enter the history:
# email addresses, the GCE VM's OS Login home folder (/home/<user>_gmail_com), billing account IDs.
personal_data_check() {
  local files
  if git rev-parse --git-dir >/dev/null 2>&1; then
    files=$(git ls-files --cached --others --exclude-standard)
  else
    files=$(find . -type f -not -path './.git/*' -not -path './.venv*' -not -path '*/__pycache__/*' \
      -not -path './.ruff_cache/*' -not -path './.pytest_cache/*' -not -path './infra/.terraform/*')
  fi
  local hits
  # shellcheck disable=SC2086
  hits=$(printf '%s\n' $files | grep -v '^scripts/check.sh$' | tr '\n' '\0' |
    xargs -0 grep -nIE '[A-Za-z0-9.]+_gmail_com|[A-Za-z0-9._%+-]+@(gmail|googlemail)\.com|\b[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b' 2>/dev/null |
    cut -c1-160)
  if [[ -n $hits ]]; then
    echo "$hits"
    echo "personal data found (email, OS Login home path or billing ID): remove it before committing"
    return 1
  fi
  echo "no email addresses, OS Login paths or billing IDs in $(printf '%s\n' $files | wc -l | tr -d ' ') files"
}

terraform_check() {
  local tf
  tf=$(command -v terraform || echo "$HOME/.local/bin/terraform")
  # fmt and validate only: no state, no credentials, no plan or apply
  "$tf" -chdir=infra fmt -check -diff || return 1
  if [[ ! -d infra/.terraform ]]; then
    "$tf" -chdir=infra init -backend=false -input=false >/dev/null || return 1
  fi
  "$tf" -chdir=infra validate -no-color
}

run "uv.lock matches pyproject.toml" uv lock --check
run "ruff (bug rules)" uv_vm ruff check .
run "syntax on Python $PY_MAC (Mac system Python)" syntax_check uv_mac
run "syntax on Python $PY_VM (cloud VM)" syntax_check uv_vm
run "JSON files parse" json_check
run "failure taxonomy names real tests" python3 -I scripts/check_taxonomy.py
run "no personal data in committed files" personal_data_check
run "shellcheck" uv_vm shellcheck -S warning infra/*.sh infra/gcloud16s scripts/*.sh
run "actionlint (GitHub workflows)" uv_vm actionlint .github/workflows/*.yml
if command -v terraform >/dev/null || [[ -x "$HOME/.local/bin/terraform" ]]; then
  run "terraform fmt + validate" terraform_check
else
  skip "terraform fmt + validate" "terraform not installed"
fi
run "pipeline tests on Python $PY_MAC" uv_mac pytest -q pipeline/tests
run "pipeline tests on Python $PY_VM" uv_vm pytest -q pipeline/tests
run "database loader tests on Python $PY_VM" uv_vm --group db pytest -q db/tests
run "discovery tests on Python $PY_MAC" uv_mac pytest -q discovery/tests
run "discovery tests on Python $PY_VM" uv_vm pytest -q discovery/tests
if [[ $FAST == 1 ]]; then
  skip "ML tests" "--fast"
else
  run "ML tests on Python $PY_VM" uv_vm --group ml pytest -q ml/tests
fi

printf '\n== summary\n'
[[ ${#skipped[@]} -gt 0 ]] && printf 'skipped: %s\n' "${skipped[@]}"
if [[ ${#failed[@]} -gt 0 ]]; then
  printf 'FAILED: %s\n' "${failed[@]}"
  exit 1
fi
echo "all checks passed"
