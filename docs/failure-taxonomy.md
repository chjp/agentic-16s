# Failure taxonomy: data problems the pipeline knows how to handle

Every public 16S study has its own quirks. This catalog is how the pipeline grows more robust
with each new study: one row per kind of data problem, with the study where it first appeared,
how the pipeline notices it, what it does about it, and the tests that keep that behaviour.
Infrastructure failures (machines, containers, cloud) stay in [`experience-log.md`](experience-log.md);
the experience log entry (E-number) is linked when a problem first showed up as a failed run.

## The rule (also in CLAUDE.md)

When a study shows a problem the pipeline did not handle:

1. **General or rare?** If it can recur in other studies, handle it in code. If it is truly a one-off,
   make sure it ends in `needs_review` or a QC flag (never a silent wrong result), and record it here
   with the status `review only`.
2. **Handling = detection + action.** The pipeline must notice the problem from the data or the
   archive, act on it, and write down what it did (manifest, `params.json` reasons, log). It never
   guesses silently; when the evidence is not enough, it stops for a person.
3. **A regression test** named after the case, offline (no network, no LLM).
4. **A row below** (next free Q-number). `scripts/check.sh` checks that every test named here exists.
5. **The commit says which problem it addresses:** the subject names the data problem; the body has
   Problem (study, numbers), Cause, Handling, Not affected (dry check on earlier studies), Tests, and
   the Q-number.

## Catalog

Status: `handled` (code detects and acts), `review only` (code stops for a person), `curation`
(handled by the curation agent's rules, not by pipeline code yet).

| Q | Data problem | First seen | Detection | Handling | Tests | Status |
|---|---|---|---|---|---|---|
| Q01 | ENA's FASTQ lost the quality scores (derived from SRA Lite; every quality the same) | 23013615 (454, E3); 37696680 (MiSeq paired, E22) | preflight: at most 2 distinct quality values in the sampled reads | NCBI's normalized .sra via the SDL locator, md5-checked; single-end through `sra_source.fetch` (454 clip points), paired through `fetch_split` (R1/R2); same reads as ENA required | `test_picks_normalized_not_lite_or_sff`, `test_none_when_only_lite`, `test_decide_454_trimmed_reverse_reads_with_lost_quality`, `test_quality_fallback_dumps_paired_runs_as_r1_and_r2`, `test_mate_files_ignore_the_unpaired_reads_file` | handled |
| Q02 | ENA lists a FASTQ with size and md5 but serves an HTML page | 35756051 (E10) | fetchngs' final error names the run | that run from NCBI's .sra in ENA's read format, at most max(3, 5 %) runs per study | `test_failed_runs_only_from_final_error`, `test_failed_runs_none_for_other_errors`, `test_fallback_limit`, `test_split_dump_uses_ena_read_names` | handled |
| Q03 | ENA answers with empty content (FASTQ or run metadata) | 37696680 smoke, 36111238 full (E22) | a mate file yields no reads; fetchngs' runinfo step writes nothing | the run counts as unreadable; no readable run means `needs_review`; a fetch failure is resubmitted once by hand | `test_empty_mate_file_is_an_error_not_zero_percent`, `test_decide_paired_without_r2_observations_needs_review` | handled (preflight); review only (fetchngs metadata) |
| Q04 | Archive declares SINGLE but serves an R1/R2 pair | 36111238 | ENA layout vs `_1`/`_2` files vs protocol.json | processed as PAIRED when the paper says paired, recorded in the manifest; otherwise `layout_mismatch` | `test_single_in_ena_with_mate_files_and_paired_protocol_is_processed_paired`, `test_single_in_ena_with_mate_files_without_protocol_support_is_flagged_not_guessed`, `test_ordinary_runs_are_left_alone` | handled |
| Q05 | One sequencing batch has shorter reads than the truncation chosen for the majority | 36111238 (E23: 14 runs 2x300 among 2x301 lost 98 % of reads) | preflight: exact read length of every run from ENA, shortest batch of >= 3 runs or 5 %; QC gate: per-run filter retention | truncation capped below what the shortest batch reaches, if pairs still merge; else `needs_review` | `test_run_read_lengths_keep_only_fixed_length_runs`, `test_shortest_batch_ignores_a_lone_short_run`, `test_truncation_capped_when_a_batch_has_shorter_reads`, `test_truncation_untouched_when_all_runs_share_one_length`, `test_truncation_cap_that_breaks_merging_goes_to_review`, `test_qc_gate_flags_runs_lost_in_quality_filtering` | handled |
| Q06 | Automatic truncation would leave too little overlap to merge pairs | 35756051 | predicted overlap from quality profiles and the amplicon length | explicit `trunclenf/trunclenr`; `needs_review` if the reads cannot overlap at all | `test_truncation_keeps_auto_when_overlap_is_ample`, `test_truncation_set_explicitly_when_auto_would_break_merging`, `test_truncation_review_when_reads_cannot_overlap` | handled |
| Q07 | 454 reads of variable length | 23013615 (E4) | platform LS454 / ION_TORRENT | fixed `trunclenf` where 75 % of reads reach and quality holds | `test_decide_454_trimmed_reverse_reads_with_lost_quality` | handled |
| Q08 | Reads start with the reverse primer (sequenced from the 3' end) | 23013615 | reverse primer at the read start, or conserved 16S sites in reverse | primers swapped, `dada_taxonomy_rc`, ASVs flipped back by collect.py | `test_decide_454_reverse_orientation`, `test_orientation_votes_from_conserved_sites` | handled |
| Q09 | Primers already removed in the archive | rule | primer at the start of < 20 % of reads | `skip_cutadapt` | `test_decide_primers_absent_skips_cutadapt` | handled |
| Q10 | Primers in only part of the reads | rule | between the absent and present thresholds | `needs_review` with the shares | `test_decide_partial_primers_needs_review` | review only |
| Q11 | Spacer or barcode before the primer | 36111238 (2-8 nt) | primer found up to 12 nt into the read | cutadapt removes it with the primer; the largest spacer counts in the truncation cap | `test_find_primer_offset_and_mismatch` | handled |
| Q12 | Primer 5' end clipped in the reads | 37661264 | 3' part of the primer at the read start | counted as present | `test_partial_primer_at_read_start` | handled |
| Q13 | Paper prints a primer shorter than the catalog's | 36111238 (338F without its last base) | exact catalog match fails | 3'-shortened match (up to 2 nt), so the amplicon length is known | `test_catalog_matches_a_primer_printed_one_base_short`, `test_expected_amplicon_from_catalog` | handled |
| Q14 | Paper gives no primer sequences | 37661264; 37696680 (only a citation) | protocol.json without sequences | catalog primer pair clearly at the read start is adopted and marked `inferred` | `test_partial_primer_at_read_start` | handled |
| Q15 | Quality scores binned (3-7 values) | rule | distinct quality values | `needs_review` (set `--binned_quality`) | — | review only |
| Q16 | Non-16S runs in the same project (shotgun WGS, fungal ITS2 with identical strategy and layout) | 29335008; 37696680 | curation: library names, design text, BioSample titles, primers in the reads | `is_16s = no` in the sample sheet; only `is_16s = yes` runs are processed | — | curation |
| Q17 | Several platforms or BioProjects in one study (MiSeq + PacBio; 454 + MiSeq controls) | 42349924; 37696680 | instrument_model, layout, bioproject per run | protocol.json `select` takes the runs the pipeline can process | `test_protocol_select_keeps_only_the_runs_the_pipeline_can_process` | handled |
| Q18 | Labels sit somewhere unusual (BioSample attributes, names, titles, descriptions, supplements, third-party copies) | LESSONS 1-5; 36111238 (BioSample description) | curation agent searches every place in LESSONS order | labels with their source in the sample sheet | — | curation |
| Q19 | Controls come from another (biobank) BioProject and the paper does not say which were used | 37696680 | curation | owner decides per study (37696680: patients only, case-only) | — | curation |
| Q20 | Samples the paper counts are missing from the archive | 42349924 (7 controls) | validation.json counts | verdict `partial` plus a human task for the authors | — | review only |
| Q21 | Sequencing batch lines up with case/control | 42349924 (all controls one run); 36111238 (read length) | curation notes batch fields in `other_json` | recorded; how the classifier treats such studies is an owner decision | — | review only |
| Q22 | Archive metadata fields are filled with placeholders or invented values (e.g. one collection date per day in accession order) | 36111238 | curation | field not used, reason in summary.md | — | curation |
| Q23 | The screening model's decision contradicts its own reason (INCLUDE with "should be excluded") | ibd100 (two DSS mouse papers) | a phrase list over the reason of every INCLUDE | the prompt asks for the reason first; a contradicted INCLUDE becomes UNCERTAIN with a marker | `test_include_whose_reason_argues_for_exclusion_is_caught`, `test_screen_one_sends_a_contradicted_include_to_review`, `test_prompt_asks_for_the_reason_before_the_decision` | handled |
