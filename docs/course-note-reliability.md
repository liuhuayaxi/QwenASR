# Course note recovery and segment ownership

Course notes use one shared authoring, validation, compilation and visual-review pipeline. The behavior below applies to subsequent recordings and generated notes; recovery never depends on a recording ID or a manually edited returned proposal.

## Expected behavior

| Trigger | Required result |
| --- | --- |
| One AI request contains several transcript segments | Every result retains exactly one original segment ID, its real times and its own correction candidate. This also applies to uploaded-audio global editing and translation. |
| AI merges, omits or reorders IDs | Reject that mapping and use the existing bounded split recovery. Persist completed ranges before processing their siblings. |
| An old global result is replaced by a different request fingerprint | Preserve the displaced result in refinement history. |
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
