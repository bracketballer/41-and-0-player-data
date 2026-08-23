# On-floor independent audits

The sample_onfloor_audit module writes a review manifest for issue #4 concern
#1:

~~~bash
python -m scripts.analysis.sample_onfloor_audit --season 2026 \
    --output docs/audits/onfloor-2026-v1.json
~~~

The generated file is intentionally incomplete. A reviewer must fill each
record's expected_player_ids from an official game record or timestamped
full-game video, add an evidence URL and locator, and set annotation_status to
complete. CBBD endpoints cannot be used as the expected answer because the
purpose of this audit is to test source-lineage independence.

The validator counts only completed records with exactly five unique expected
IDs. It reports unresolved/missing events separately and calculates a Wilson
95% interval. The release gate is at least 135 independently verifiable
events, at least 95% exact-five accuracy, a 90% lower confidence bound, and no
systematic substitution-boundary error.
