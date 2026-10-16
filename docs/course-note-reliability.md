# Course note recovery and segment ownership

Course notes use one shared authoring, validation, compilation and visual-review pipeline. The behavior below applies to subsequent recordings and generated notes; recovery never depends on a recording ID or a manually edited returned proposal.

## Expected behavior

| Trigger | Required result |
| --- | --- |
| One AI request contains several transcript segments | Every result retains exactly one original segment ID, its real times and its own correction candidate. This also applies to uploaded-audio global editing and translation. |
| AI merges, omits or reorders IDs | Reject that mapping and use the existing bounded split recovery. Persist completed ranges before processing their siblings. |
| An old global result is replaced by a different request fingerprint | Preserve the displaced result in refinement history. |
| A rewrite changes a number, unit, negation or formula condition | A fresh AI request checks every candidate against acoustic ASR text, nearby segments and available original course evidence. Adopt supported corrections; keep the previous text for rejected or uncertain changes. Uncertainty is an inline note and does not require individual approval. |
| A verifier returns missing, reordered or duplicate IDs, or unknown evidence | Retry the structured verdict once; an invalid batch changes no text. Retain the proposals and show a recoverable task failure. |
| A verifier finishes but the process stops before saving the recording | Replay the hashed receipt and per-candidate journals, provided the candidate and its original source still match. A changed source is never overwritten. |
| A service restarts with saved correction candidates | Resume independent verification using server-saved settings. Explicitly paused or cancelled tasks stay stopped. New batch, global, periodic and note-generation paths also invoke the verifier. |
| A reader confirms one global correction | Update that segment and rebuild its aggregate export text. Neighboring segments retain their own text and decisions. |
| The writer emits a simple table float | Convert its wrapper to an in-place table and its caption to `captionof{table}`. Keep all cells; validate the resulting body with the normal command allowlist. |
| Text contains Latin accents | Normalize simple text accents to plain Latin letters. Mathematical commands such as `hat`, `bar` and `tilde` retain their meaning. |
| A failed writer checkpoint is resumed | Revalidate the last complete returned source before making another AI request, for both short and batched notes. Keep original proposals and writer receipts. |
| A material session exits silently | Treat an empty exit with code 0 or 1 as a recoverable provider failure. Review batches have a three-attempt limit and remain unreviewed until image reads and completion are attested. |
| A provider explicitly rejects authentication, quota, sandbox or CLI configuration | Preserve results and surface the actionable failure. Repeating the same rejected request cannot repair it. |
| Compiler adapter changes produce identical PDF bytes | Reuse the most advanced matching review, including partial batches with session receipts. PDF bytes, source binding, template, images, references, toolchain and review policy must match. |
| A reader opens a correction or types an edit while polling continues | Preserve disclosure state and the edit. Unchanged polls do not redraw the transcript; delayed polls cannot overwrite newer local results. |
| A save response arrives after a redraw | Apply its acknowledgement to the current card. A failure leaves the draft and retry controls available. |

The writer instructions include the actual command and environment allowlists from the validator. Normalization does not enable file access, macro definitions, arbitrary packages or engine primitives. Invalid or incomplete source, missing image reads and changed evidence still prevent publication.

## Regression commands

Run from the repository root with the project dependencies installed:

```sh
QWEN_ASR_REAL_LATEX_TEST=1 .venv/bin/python -m unittest discover -s tests -q
node --test tests/*.cjs
# After installing tests/browser dependencies and Chromium:
node tests/browser/transcription-review.cjs
```

The native LaTeX tests compile synthetic optics and assembly notes through the same table, code and mathematical-body path. The provider tests simulate malformed output, transient failures, permanent rejections, interruption and recovery; they do not claim a real provider request.

The browser regression runs in GitHub Actions. For a manual check of its isolated correction UI fixture, serve the repository locally and open `/tests/browser/transcription-review.html`. It imports the production component and replaces its network calls with synthetic data. Check this sequence:

1. Open the first segment and type a manual correction.
2. Trigger a redraw: the editor stays open and retains the text.
3. Save, redraw, and return a failure: the draft and retry controls remain available.
4. Retry, redraw, and complete the delayed save: the result still has two separate time ranges and the neighboring candidate remains pending.

## Release checks

Verify the published PDF's hash against its completed visual-review record and its download response. Keep original audio and transcript hashes across a service restart. Load updated service code only after active jobs have saved their checkpoints; restore any deliberately deferred indexing after the restart.

A real 34-page course note passed complete visual review during this change. Its download returned HTTP 200 and matched the reviewed PDF hash. A real latest recording retained 64 independent transcript ranges after restoring four legacy merged pairs; playback opened at the selected segment time. These runtime checks complement the synthetic recovery tests.

## Memory controls

Idle backlog polling reads changed metadata headers and does not import the audio decoder or NumPy. Task polling visits recording objects sequentially, and the read cache stays bounded during the scan as well as afterward. Cache limits are four recent records and an estimated 8 MiB of serialized input, plus the active recording; the serialized size is not a claim about exact Python heap size.

Native PDF text extraction, selected-page rendering, image matching and completion inspection run in short-lived workers. One shared parent lock serializes those operations; each render request accepts at most four selected pages. Worker exit releases its native image cache. This does not change the original courseware or lower the quality of the released PDF.

Knowledge-store connections close at each transaction boundary. Vector serialization retains the existing float32 format. Exact cosine ranking streams database rows into a bounded top-k heap using the standard library, so retrieval does not load a permanent NumPy runtime or allocate a full matrix in the web service. Actual local inference remains a separate, on-demand process.

Remote search initially loads only replica identities and versions. It fetches vector bytes only for the next changed batch (at most 64 vectors), and loads the original text only for returned, freshly revalidated hits. Waiting for Windows therefore does not retain every vector and paragraph in a course. Deleted entries, changed source versions and unmatched result IDs remain excluded.

Measure an idle service with no recording, AI, indexing or export work. On macOS compare physical footprint (including the process's compressed/swapped allocation), not just RSS; record active worker processes separately. A cold process, an indexing process and a service after real requests are different measurements.
