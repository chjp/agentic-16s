#!/bin/bash
# Runs as root on every boot of the worker (Terraform metadata startup-script). Must stay idempotent.
set -euo pipefail

# 1. Tools: Docker for the nf-core containers, Java 21 for Nextflow, tmux so runs survive SSH disconnects
if ! command -v docker >/dev/null || ! command -v java >/dev/null || ! command -v tmux >/dev/null || ! command -v fastq-dump >/dev/null; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io openjdk-21-jre-headless tmux python3 sra-toolkit
  systemctl enable --now docker
fi
if ! command -v nextflow >/dev/null; then
  cd /tmp && curl -fsSL https://get.nextflow.io | bash && install -m 755 nextflow /usr/local/bin/nextflow
fi

# 2. Read-only status for Claude Code on the Mac: ssh agentic16s-worker agentic16s-status
cat >/usr/local/bin/agentic16s-status <<'STATUS'
#!/bin/bash
uptime; free -h | head -2; df -h / | tail -1
pgrep -f 'nextflow' >/dev/null && echo "nextflow: running" || echo "nextflow: not running"
for f in /home/*/agentic-16s/studies/*/log.jsonl; do [ -f "$f" ] && echo "$f: $(tail -n 1 "$f")"; done
for f in /home/*/run.log; do [ -f "$f" ] && { echo "== $f"; tail -n 15 "$f"; }; done
STATUS
chmod 755 /usr/local/bin/agentic16s-status

# 3. Stop the VM after 60 idle minutes (no nextflow process), but never in the first hour after boot
cat >/etc/cron.d/idle-shutdown <<'CRON'
*/10 * * * * root pgrep -f nextflow >/dev/null && touch /run/agentic16s-busy; [ -n "$(find /run/agentic16s-busy -mmin -60 2>/dev/null)" ] || [ "$(cut -d. -f1 /proc/uptime)" -lt 3600 ] || /sbin/shutdown -h now
CRON
echo "startup done $(date -Is)"
