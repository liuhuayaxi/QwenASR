# macOS idle memory validation - 2026-10-22

## Scope and metric

Measured on the actual Apple Silicon Mac, macOS 27.0.1, CPython 3.12.13,
with the existing library and normal background services enabled. The goal is
**under 100 decimal MB of physical footprint when idle**, including the main
service and its remaining Python resource-tracker child. RSS is also recorded;
it is a different metric and has **not** been reduced below 100 MB.

The browser, the separate sharingnotes-monitor project, and unrelated processes
are not included. Model memory during active inference is not an idle target.

## Changes

- Run file audio decoding, AAC conversion/splitting and playback conversion in
  short-lived native-codec workers. PCM uses an anonymous disk-backed mapping,
  rather than transferring whole recordings through JSON.
- Run replay extraction/stream decoding in a disposable process. Each PCM chunk
  waits for its durable checkpoint acknowledgement before decoding continues.
  Cancellation, failed callbacks and worker crashes reap the child.
- Run PDF validation, page counts, text/image conversion, attachment packaging
  and text statistics in the existing disposable PDF worker. The web process
  no longer needs to load PyMuPDF for these operations.
- Release completed upload event buffers. Live subscribers are woken first;
  reconnects obtain completion data from the durable recording instead of
  retaining duplicate transcripts indefinitely.
- Apply a per-service macOS allocator policy **before launching Python**:
  `PYTHONMALLOC=malloc`, `MallocAggressiveMadvise=1`, `MallocLargeCache=0`.
  This lets native idle reclamation address small Python allocations as well
  as large freed blocks. Existing operator values are preserved. Other
  platforms are unchanged. `QWEN_ASR_MALLOC_TUNING=0` disables this policy.
  No global shell, OS security setting, model weights or user data is changed.

## Measurements

All MB below are decimal. `vmmap -summary` reports rounded binary quantities,
which were converted to bytes before calculating MB.

| State | Main physical footprint | Main RSS |
| --- | ---: | ---: |
| Original running process, idle model | approximately 198.6 MB | approximately 210 MB |
| Optimized idle service before test | 66.48 MB | 123.22 MB |
| First real local transcription completed; model unloaded | 70.46 MB | 130.78 MB |
| Second real local transcription completed; model unloaded | 70.78 MB | 130.92 MB |
| Settled after invalid-input failure and test cleanup | 72.46-72.67 MB | 139.03-139.12 MB |

The settled range consists of seven samples spanning approximately 63 seconds.
Every sample had zero active tasks, no local ASR model and no ASR worker PID.
A separate process-tree sample measured 72.457 MB for the main service and
9.912 MB for the resource tracker: **82.369 MB combined**. No codec/PDF/model
worker remained. This is a bounded acceptance test, not a claim of indefinite
stability for every future workload.

Two consecutive real Qwen3-ASR-1.7B-MLX-bf16 transcriptions of locally generated
English speech completed correctly (6.40 and 4.43 seconds in this run).
A deliberately invalid audio upload failed cleanly. The saved remote/power
policy was restored immediately after submitting each isolated local test;
test recordings were moved to Trash and the temporary category was removed.
No live microphone or private recording was used as a fixture.

## Regression evidence

- 73 focused tests passed from an isolated copy of the **staged Git tree**,
  both with default interpreter allocation and with the final allocator policy.
  This excludes the unrelated uncommitted local changes from the test copy.
- The local working-tree full run executed 954 tests: 938 passed, 8 skipped,
  7 assertion failures and 1 import error. The same seven assertion failures
  and the import error were reproduced against the pre-change backup of the
  affected modules (28 tests), so they were not introduced by this change.
  The affected areas are AGY session receipts, knowledge membership, summary
  naming and the existing classroom-chat test import.
- Real native audio/PDF operations, exact replay resume, cancellation, child
  crash, checkpoint callback failure, upload stream reconnect and allocator
  opt-out/explicit overrides have dedicated regression coverage.

## Repeat the read-only measurement

With the service running and no tasks active:

```sh
.venv/bin/python scripts/sample_memory_footprint.py --seconds 60 --output /tmp/qwenasr-memory.json
```

The sampler reports physical footprint for the main process and all currently
owned descendants, RSS separately, active task counts and model state. Missing
process measurements do not count as a successful acceptance result. Do not
compare RSS to the physical-footprint target or count startup/indexing as idle.

To compare allocator behavior, stop and start the service normally with
`QWEN_ASR_MALLOC_TUNING=0` in the launch environment, then remove that variable
and restart to restore the optimized default. There is no global `purge` or
memory-pressure simulation in this implementation.

## Reference

CPython documents `PYTHONMALLOC=malloc` in its memory-management and command-line
manuals. This changes the allocation family, not a live-memory limit. The
libmalloc launch knobs are process-local and should be revalidated when the
Python/macOS runtime changes. The measured benefit here comes from the actual
Mac A/B runs, not from assuming that an allocator switch always saves memory.
