# agentic-16s — instructions for agents

## Keep the progress board current

`plan-progress.html` is how the project owner sees where the project stands. `progress/build.py` writes its data block from `progress/plan.json` plus a scan of the disk.

Whenever you start, finish, or get blocked on a piece of work:

1. Update `progress/plan.json`:
   - Add a task under the right phase if the work is not listed. Give it `id`, `title`, `owner` (`script`, `agent`, `human`, or `agent + human`), and a one-sentence `note`.
   - List the files the task produces under `outputs` (paths relative to the repo root; `{d}` expands to every candidate disease key) and set `"status": "auto"`. The board then derives the status from what is on disk. Prefer this over a declared status.
   - Use a declared status (`todo`, `in_progress`, `done`, `blocked`, `needs_human`) only when the task has no file outputs.
   - Put anything that waits on the owner in `needs_you`, and record choices that were made in `decisions` with the date.
2. Run `python3 progress/build.py`.

Do not mark a task `done` unless its outputs exist and were checked. Do not edit the data block inside `plan-progress.html` by hand.

## Keep the experience log current

When a pipeline run fails, needs a rerun, or uses much more time or memory than expected, add an
entry to `docs/experience-log.md` the same day: symptom, cause, cost (wall time, peak memory),
fix, and what it means at larger scale (machine sizing, cloud). Give numbers and say whether they
were measured, sampled or estimated. Per-step resource use is written automatically to
`studies/<PMID>/resource_profile.json` by `pipeline/bin/resource_monitor.py`; summarize it with
`python3 -I pipeline/bin/resource_monitor.py report`.

## One repository

All code lives in this repository (github.com/chjp/agentic-16s). The literature stages are in
`discovery/` (moved from `paper-discover2sra` at 8c602c0, which is frozen: do not edit or commit there).
Discovery runs are written to `discovery/runs/` (git-ignored).

## When a study shows a data problem the pipeline did not handle

The pipeline should get more robust with every study. When new data breaks it or gives a wrong
result:

1. Decide whether the problem can recur in other studies. If so, handle it in code: detect it from
   the data or the archive, act on it, and record what was done (manifest, `params.json` reasons,
   log). Never guess silently; when the evidence is not enough, stop with `needs_review`. A truly
   rare case only needs to end in `needs_review` or a QC flag.
2. Add an offline regression test named after the case.
3. Add a row to `docs/failure-taxonomy.md` (next Q-number: problem, first study, detection,
   handling, tests, status). `scripts/check.sh` fails if a named test does not exist.
4. Dry-check the change against the studies already processed and say what it changes.
5. Write the commit so a reviewer understands it without the chat: the subject names the data
   problem; the body gives Problem (study, numbers), Cause, Handling, Not affected (the dry check),
   Tests, and the Q-number. If a run failed, also add the experience-log entry (E-number).

## Run the checks before you finish

After changing code, run `scripts/check.sh` (about 20 s; `--fast` skips the ML tests) and fix what
it reports. It is the same script CI will run: ruff, syntax on Python 3.9 (the Mac's system Python,
which runs `pipeline/bin` and `infra/*.py`) and 3.12 (cloud VMs), JSON parsing, shellcheck, terraform
fmt/validate, and the tests. Python dependencies live in `pyproject.toml` / `uv.lock`; use
`uv run --group db|ml|dashboard ...` instead of `uv run --with ...`, and after changing
`pyproject.toml` run `uv lock`. Code in `pipeline/bin` and `infra/*.py` stays standard-library only.

## Google Cloud commands

Run gcloud for this project as `infra/gcloud16s <args>`, never plain `gcloud`. The owner's Mac has two Google accounts, and another tool overwrites the default gcloud sign-in with the wrong one. If any Google call returns 403 or `invalid_grant`, first check which email the token really belongs to (the owner's global CLAUDE.md has the command and the full story) before suspecting the project, billing or the owner's login.
