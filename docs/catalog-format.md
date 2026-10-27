# Catalog format

The catalog is the list of model builds that ablit can run. This page describes the file format and the rules that decide when an entry can be launched. To add an entry, follow the steps in CONTRIBUTING.md.

## Files

Each build is one YAML file at `catalog/models/<id>.yaml`, and `catalog/schema/v1.json` checks it. The file name must match the `id` field. `tools/build_catalog.py` compiles all entries into `catalog/dist/catalog.json` and prints its SHA-256, and each run record stores a digest of the catalog files it used. Catalog releases use the version form `YYYY.MM.DD.N`, which is kept in `catalog/VERSION`. The integer `schema` field changes only when the format changes, and a CLI release refuses schema versions it does not know.

Catalog data is CC0-1.0. Each entry records its model's own license, which the catalog license does not change. Entries hold data only, and all commands live in the reviewed engine templates under `engines/`.

## Entry layout

The following shows the fields of an entry. The entry `catalog/models/ds41-1017-xq-v3-uncensored.yaml` is a complete example.

```yaml
schema: 1
id: ds41-1017-xq-v3-uncensored
display_name: DeepSeek-V4.1-Flash 1017, XQ V3 uncensored quant (Xolotl)
source:
  repo: IndrasMirror/DeepSeek-V4.1-Flash-1017-XQ-Uncensored
  revision: 9d3c01d86709261e51d566b02e1dda75f40d009a   # a full commit hash
  license: mit
  base_model: deepseek-ai/DeepSeek-V4.1-Flash-1017
  modification: abliterated        # none | abliterated | finetune | quantized
artifacts:                         # every file the engine reads
  - {path: ..., bytes: ..., sha256: ..., role: weight}
  - {path: ..., bytes: ..., sha256: ..., git_blob_id: ..., role: tokenizer}   # small files with no LFS hash
  - {repo: ..., revision: ..., path: ..., bytes: ..., sha256: ..., role: engram-source}   # a file from another repo
staging:                           # order of steps, used to compute peak disk
  - {step: download, artifacts: all}
  - {step: join, inputs: [weight], output_bytes: ..., keeps_inputs_until_done: false, delete_inputs_after: true}
profile:
  engine: xolotl
  engine_lock: xolotl-961d390
  gpu: {vendor: nvidia, count: 1, vram_gb_each: 24, compute_capability_min: 7.5}
  host_ram_gb: 90
  storage: {random_read_required: true}
  context_tokens: 4096
  concurrency: 1
  port: 8150
  startup_hours_estimate: 0.3
  min_download_mbps: 1000
sources: [url, ...]                # one URL for each number in profile
tests: []                          # boot test records, see below
status: unverified                 # unverified | boot-tested | removed
notes: []                          # where each figure came from
```

## Rules for the schema

1. `artifacts` lists every file the engine reads, including draft models and tokenizer files.
2. Large files carry the Hugging Face LFS SHA-256. Small files without an LFS record carry the Git blob ID, and `tools/verify_entry.py` downloads them at the pinned revision to compute a SHA-256.
3. Peak disk is computed from `staging` and is never written by hand. A `join` step with `keeps_inputs_until_done: false` deletes each input before it reads the next, so the peak is the whole download and not twice the weights. The engine lock adds a margin of one weight part on top of the peak (`disk_margin_gb`), and room for the fallback Engram extraction (`extract_disk_gb`) when the engine has one.
4. `profile` is one exact combination of engine lock, GPU arrangement, context length and concurrency. A test of one profile does not approve another.

## Status and launch rules

An entry is `unverified` once CI passes and a maintainer merges it. It becomes `boot-tested` when a `tests` record points to a log from a real run of its current build and profile. The logs are in `catalog/boot-tests/`.

Each test record has `backend`, `date` and `log`, and a current test also needs `image_digest`, `build_sha256` and `bootstrap_sha256`:

```yaml
tests:
  - backend: vast
    date: "2026-10-27"
    log: boot-tests/<id>-2026-10-27.md
    image_digest: "sha256:<64 hex characters>"
    build_sha256: "<64 hex characters>"
    bootstrap_sha256: "<64 hex characters>"
```

The `image_digest` must match the current engine lock's digest. The `build_sha256` identifies the entry's `source`, `artifacts`, `staging` and `profile`, including its engine lock name. The `bootstrap_sha256` hashes `engines/<engine>/bootstrap.sh`, so a change to serving defaults in the script invalidates the test even when the image is unchanged.

Every new run records all three hashes when it is created. ablit preserves the bootstrap hash on resume and checks the bundle's script against it before upload. A mismatch stops the run and starts cleanup. `ablit up --report` includes the recorded hashes in its JSON report. Copy the hashes from the tested run, rather than from an entry that may have changed since the run started. Old runs without a recorded bootstrap hash report `null` for that field and cannot approve the current build.

Validation refuses `status: boot-tested` unless at least one record matches all three hashes. Changing a model revision, artifact, staging step, profile, engine image or bootstrap script needs a new test. Keep historical records and set the entry to `unverified` until the new test passes. Descriptions, notes and source citations can change without invalidating a test.

Launching an `unverified` entry needs `--untested`, and the quote then says "Nobody has booted this exact build and profile yet. The engine may fail to start, and you pay for the time until ablit destroys the run." A `removed` entry cannot be launched. Before renting, ablit checks that every artifact is still public at its pinned revision.

## Checks on contributions

CI runs the checks listed in CONTRIBUTING.md on every pull request, and none of them rents anything. A weekly workflow, `.github/workflows/weekly-catalog.yml`, runs the Hugging Face checks again on every entry and fails when an artifact has disappeared. A maintainer then sets that entry to `removed`.
