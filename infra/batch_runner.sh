#!/bin/bash
# Google Batch job script: one study per job. Submitted by infra/batch_submit.py, which sets the
# variables below. Runs as root on Batch's Debian image (Batch supports only Debian and Rocky Linux
# images; the GCE worker used Ubuntu 24.04, so tool versions can differ: MODE=probe shows them).
#
#   MODE      probe | run
#   OUT_URI   gs://<bucket>/runs/<run_id>/output   (new objects only: the VM may create, not overwrite)
#   CODE_URI  gs://<bucket>/code/<sha>.tgz         fixed code snapshot (pipeline/ + studies/<PMID>/)
#   INPUT_URI gs://<bucket>/runs/<run_id>/input/inputs.tgz   manifest.tsv + runs.csv per tag (run mode)
#   PMID, RUN_ARGS (e.g. "--subset 6"), MAX_MIN (minutes before run_study.py is stopped)
#
# The same steps as the GCE worker (infra/startup.sh + infra/run_studies.sh): Docker, Java, Nextflow,
# sra-tools; run_study.py --from preflight --no-db; the Mac loads the tables afterwards.
set -uo pipefail
PATH_FROM_BATCH=$(env | grep '^PATH=' || echo "PATH not exported (bash default: $PATH)")
# Batch starts this script without an exported PATH: bash falls back to its built-in default but does
# not pass it on, so Python's subprocess searched only /bin:/usr/bin and missed /usr/local/bin/nextflow.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
export DEBIAN_FRONTEND=noninteractive HOME=/root NF_PROFILE=docker
W=/var/lib/agentic16s
OUT=$W/out
export DATA_DIR=$W/data
mkdir -p "$W/repo" "$DATA_DIR" "$OUT"
exec > >(tee -a "$OUT/runner.log") 2>&1
say() { echo "[runner $(date -u +%FT%TZ)] $*"; }
: "${MODE:=run}" "${OUT_URI:?}" "${CODE_URI:?}" "${PMID:=}" "${RUN_ARGS:=}" "${MAX_MIN:=360}"

upload() { gcloud storage cp --quiet "$@" "$OUT_URI/"; }

fetch_code() {
  gcloud storage cp --quiet "$CODE_URI" "$W/code.tgz" && tar -xzf "$W/code.tgz" -C "$W/repo" || return 1
  NXF_VER=$(sed -n 's/^NXF_VER=//p' "$W/repo/pipeline/versions.env")
  export NXF_VER
}

# Same tool set as infra/startup.sh; Java 21 if the image offers it, else 17 (Debian 12: 17).
install_tools() {
  local pkgs=()
  command -v docker >/dev/null || pkgs+=(docker.io)
  command -v fastq-dump >/dev/null || pkgs+=(sra-toolkit)
  command -v python3 >/dev/null || pkgs+=(python3)
  command -v curl >/dev/null || pkgs+=(curl)
  [ "$MODE" = probe ] && pkgs+=(python3-pytest)
  apt-get update -q || return 1
  if ! command -v java >/dev/null; then
    # `apt-cache show` succeeds for a name another package refers to even when it cannot be
    # installed (Debian 12: openjdk-21 is referred to, only 17 installs); ask for a candidate.
    if apt-cache policy openjdk-21-jre-headless 2>/dev/null | grep -q 'Candidate: [0-9]'; then
      pkgs+=(openjdk-21-jre-headless)
    else pkgs+=(openjdk-17-jre-headless); fi
  fi
  if [ ${#pkgs[@]} -gt 0 ]; then
    say "apt-get install ${pkgs[*]}"
    apt-get install -y -q "${pkgs[@]}" || return 1
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  if ! command -v nextflow >/dev/null; then
    (cd /tmp && curl -fsSL https://get.nextflow.io | bash && install -m 755 nextflow /usr/local/bin/nextflow) || return 1
  fi
}

probe() {
  local t0=$SECONDS
  {
    echo "== image before install"
    grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; uname -r
    echo "nproc $(nproc)"; free -g | head -2; df -h / | tail -1
    for c in gcloud docker java python3 fastq-dump nextflow curl; do printf '%-11s %s\n' "$c" "$(command -v $c || echo MISSING)"; done
  } | tee "$OUT/probe.txt"
  fetch_code || { say "code download failed"; return 1; }
  install_tools || { say "install failed"; return 1; }
  {
    echo "== after install ($((SECONDS - t0)) s)"
    python3 --version; java -version 2>&1 | head -1
    docker version --format 'docker {{.Server.Version}}'; docker info --format 'docker sees {{.NCPU}} CPUs, {{.MemTotal}} bytes'
    nextflow -version 2>&1 | grep -i version
    fastq-dump --version 2>&1 | grep -m1 -E '[0-9]+\.[0-9]+\.[0-9]+'
    gcloud --version | head -1
    # what run_study.py's subprocess calls will find, not what this shell finds
    python3 -I -c 'import shutil; print("python sees", *(f"{t}={shutil.which(t)}" for t in ("nextflow", "docker", "java", "fastq-dump")))'
    echo "== network"
    printf 'ENA      %s\n' "$(curl -s -o /dev/null -w '%{http_code}' https://ftp.sra.ebi.ac.uk/vol1/fastq/)"
    printf 'NCBI SDL %s\n' "$(curl -s -o /dev/null -w '%{http_code}' 'https://locate.ncbi.nlm.nih.gov/sdl/2/retrieve?acc=SRR15400852')"
    printf 'GitHub   %s\n' "$(curl -s -o /dev/null -w '%{http_code}' https://github.com/nf-core/ampliseq)"
    docker pull -q quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0 >/dev/null 2>&1 && echo "quay.io pull ok" || echo "quay.io pull FAILED"
    echo "== unit tests on this Python (E9: a 3.12-only bug once hid behind the Mac's 3.9)"
    (cd "$W/repo" && python3 -m pytest -q pipeline/tests 2>&1 | tail -5)
    echo "== total $((SECONDS - t0)) s"
  } 2>&1 | tee -a "$OUT/probe.txt"
}

# Pull every image in pipeline/containers.txt, retrying a failed pull. Each Batch job starts on a new
# VM without images, and one dropped pull inside ampliseq (docker exit 125, which nf-core does not
# retry) ended a 38-min run (experience log E20). Runs next to fetch, which mostly waits on ENA.
prepull() {
  local img n out ok=0 bad=0 t0=$SECONDS
  while read -r img _; do
    case "$img" in '' | \#*) continue ;; esac
    for n in 1 2 3 4 5; do
      if out=$(docker pull -q "$img" 2>&1); then ok=$((ok + 1)); break; fi
      echo "pull failed ($n/5): $img: ${out##*$'\n'}"
      if [ "$n" -eq 5 ]; then bad=$((bad + 1)); else sleep $((n * 20)); fi
    done
  done <"$W/repo/pipeline/containers.txt"
  echo "pre-pull: $ok images ready, $bad failed, $((SECONDS - t0)) s"
  [ "$bad" -eq 0 ]
}

run_study() {
  gcloud storage cp --quiet "$INPUT_URI" "$W/inputs.tgz" && tar -xzf "$W/inputs.tgz" -C "$DATA_DIR" || return 1
  install_tools || { say "install failed"; return 1; }
  say "tools: $(python3 --version), $(java -version 2>&1 | head -1), nextflow $NXF_VER, $(docker version --format '{{.Server.Version}}')"
  python3 -I -c 'import shutil, sys; sys.exit(0 if shutil.which("nextflow") else "nextflow is not on the PATH Python sees")' || return 1
  # Whole-VM memory every 10 s: under Docker the per-run peak in resource_profile.json misses the
  # containers (experience log E16); per-task peaks still come from the Nextflow trace.
  (echo -e "utc\tused_mib\ttotal_mib"
   while :; do free -m | awk -v t="$(date -u +%FT%TZ)" '/^Mem:/{print t"\t"$3"\t"$2}'; sleep 10; done) >"$OUT/vm_memory.tsv" &
  local sampler=$!
  prepull >"$OUT/prepull.log" 2>&1 &
  local puller=$! deadline=$((SECONDS + MAX_MIN * 60)) rc left
  # Two calls, so that ampliseq starts only once the images are there; both stay within MAX_MIN.
  say "run_study.py $PMID --from preflight --to fetch --no-db $RUN_ARGS (all stages stopped after $MAX_MIN min)"
  # shellcheck disable=SC2086
  (cd "$W/repo" && timeout --kill-after=120 "${MAX_MIN}m" python3 -I pipeline/bin/run_study.py "$PMID" --from preflight --to fetch --no-db $RUN_ARGS)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    wait "$puller" || say "some images did not pull; ampliseq will try them itself"
    say "$(tail -1 "$OUT/prepull.log")"
    left=$(((deadline - SECONDS) / 60))
    if [ "$left" -lt 1 ]; then
      say "no time left for ampliseq"; rc=124
    else
      say "run_study.py $PMID --from ampliseq --no-db $RUN_ARGS (stopped after $left min)"
      # shellcheck disable=SC2086
      (cd "$W/repo" && timeout --kill-after=120 "${left}m" python3 -I pipeline/bin/run_study.py "$PMID" --from ampliseq --no-db $RUN_ARGS)
      rc=$?
    fi
  fi
  kill "$puller" "$sampler" 2>/dev/null
  return "$rc"
}

package() {
  local rc=$1
  # Tables, reports, Nextflow logs and the study's log/profile/QC files; never FASTQ.
  tar -czf "$OUT/results.tgz" -C "$W" --exclude='*.fastq.gz' --exclude='*.fq.gz' --exclude='*.sra' \
    "data/pipeline/$PMID" "repo/studies/$PMID" 2>/dev/null
  if [ "$rc" -ne 0 ] && [ -d "$DATA_DIR/nf-work" ]; then
    (cd "$DATA_DIR" && find nf-work -type f \( -name '.command.*' -o -name '.exitcode' \) -size -5M) \
      | tar -czf "$OUT/nf_logs.tgz" -C "$DATA_DIR" -T - 2>/dev/null
  fi
}

say "job ${BATCH_JOB_ID:-?} task ${BATCH_TASK_INDEX:-0}, mode $MODE, study ${PMID:-none}; $PATH_FROM_BATCH"
if [ "$MODE" = probe ]; then
  probe; rc=$?
else
  fetch_code || { say "code download failed"; rc=1; }
  if [ "${rc:-0}" -eq 0 ]; then run_study; rc=$?; fi
  package "$rc"
fi
say "exit $rc"
echo "$rc" >"$OUT/exit_code"
upload "$OUT"/* || say "upload failed"
exit "$rc"
