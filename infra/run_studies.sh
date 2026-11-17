#!/bin/bash
# Full pipeline runs for several studies, one after another, on the GCE worker (Docker, no database).
#   infra/run_studies.sh 29335008 35756051 ...      (run from the repo root on the VM, inside tmux)
#
# The `prepare` stage runs on the Mac first, because it needs PostgreSQL to leave out runs the database
# marks as skipped; its manifest.tsv and runs.csv are copied to DATA_DIR/pipeline/<PMID>/main/ on the VM.
# This script starts each study at preflight with --no-db: load is skipped, cleanup still deletes the
# FASTQ once the integrity checks pass. A failed study is recorded and the next one starts.
# One line per study goes to ~/run_studies.status; ~/run_studies.done is written when all are finished.
set -uo pipefail
export NF_PROFILE="${NF_PROFILE:-docker}"
export DATA_DIR="${DATA_DIR:-$HOME/agentic16s-data}"
status="$HOME/run_studies.status"
rm -f "$HOME/run_studies.done" "$status"
for pmid in "$@"; do
  start=$(date -u +%FT%T)
  echo "$start start $pmid" >>"$status"
  python3 -I pipeline/bin/run_study.py "$pmid" --from preflight --no-db
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "$(date -u +%FT%T) ok $pmid (started $start)" >>"$status"
  else
    echo "$(date -u +%FT%T) FAILED $pmid exit=$rc (started $start)" >>"$status"
  fi
done
grep -cE ' ok ' "$status" | xargs -I{} echo "$(date -u +%FT%T) finished: {} ok of $# studies listed in this batch" >"$HOME/run_studies.done"
cat "$HOME/run_studies.done"
