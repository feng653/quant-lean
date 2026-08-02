# Generation publication for multi-file market-data cache

## Problem and boundary

A POSIX rename atomically changes one pathname, not a Parquet file and its
provenance JSON together. Replacing `pool.parquet` and `pool.meta.json` in
sequence could let another backend process combine different versions. This is
not repaired by an in-process `asyncio.Lock`, because independent processes do
not share that lock.

`DataCache` now publishes daily data as an immutable generation:

```text
write+validate staged Parquet and metadata
    -> move both into daily/generations/<pool>/<generation>/
    -> fsync files/directories
    -> atomically replace daily/generation-manifests/<pool>.json
```

Readers read only the manifest, validate its schema and SHA-256/size for every
artifact, then load the named generation. A failed write before the last step
leaves an orphaned generation that no normal reader can discover; the prior
manifest remains active. A malformed, missing or hash-mismatched manifest is a
fail-closed cache error, never a best-effort fallback.

## Operating rules

- Do not edit anything under `daily/generations/` or
  `daily/generation-manifests/` manually.
- Existing flat `daily/<pool>.parquet`/`.meta.json` pairs are audit-only after
  upgrade. A controlled force refresh creates the first generation; runtime
  research refuses the old pair rather than guessing its consistency.
- Invalidation removes only the active manifest. It intentionally retains old
  generations for forensic review. Retention cleanup must be a separately
  reviewed job that first lists active manifest references and never removes
  them.
- This mechanism covers the coupled Parquet/provenance daily-cache view. It
  does **not** claim cross-database failover or a SQLite transaction spanning
  arbitrary Parquet files. Those remain NEXT-08 follow-up work if/when a
  runtime operation needs a new cross-store publication contract.

## Verification

Run:

```bash
python -m pytest backend/tests/test_generation_manifest.py \
  backend/tests/test_data_cache_ranges.py -v --timeout=120
```

The tests inject a failure between artifact installs and run a concurrent
reader/writer loop. Accepted observations are exactly `(pivot-v1, meta-v1)` or
`(pivot-v2, meta-v2)`; a mixed pair, partial generation, or modified artifact
fails the test.

## Publication-path audit (2026-08-02)

| Path | Existing consistency boundary | Result |
|---|---|---|
| Daily cache Parquet + source-provenance JSON | Two independently replaced files | **Covered here** by the generation manifest. |
| PIT master/activation metadata | SQLite transaction | SQLite readers see a committed transaction, but this is not a paired Parquet publication path. |
| Research snapshots + run manifests | Immutable content-addressed Parquet, then SQLite references | Exact runs use their saved hash; this is not an active-cache replacement operation. |
| Factor/protocol/run stores | SQLite transactions | No Parquet counterpart is published in the same operation. |

Consequently this change must not be represented as a completed general
SQLite/Parquet distributed transaction. If a later importer activates a SQLite
batch and replaces a Parquet runtime view in one user-visible operation, that
operation must either (a) be represented by a single generation manifest that
binds both immutable artifacts and the committed SQLite batch identity, with a
reader that validates both, or (b) move to a database/storage system offering a
documented consistent snapshot. Until then it remains fail-closed and tracked
as the unfinished part of NEXT-08.
