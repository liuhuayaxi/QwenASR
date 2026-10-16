# Deep UI operation regression

Run from the repository root with Node 22 after installing the existing browser dependencies:

```sh
(cd tests/browser && npm ci --ignore-scripts && npx playwright install chromium)
node tests/browser/deep-ui/run.cjs . output/playwright/deep-ui
node --test tests/test_note_autosave_ui_audit.cjs tests/test_task_scope_ui_audit.cjs
```

The optional fourth argument is a case-name regular expression:

```sh
node tests/browser/deep-ui/run.cjs . output/playwright/deep-ui-pdf 'pdf'
```

## What is rendered

Cases 01-34 render the complete production task-center HTML, styles and JavaScript. Cases 40-83 extract the actual inference, MinerU, backup, discard and continuation elements from `static/index.html` into isolated panel hosts, then load their production modules. The isolated host adds layout spacing; it is not the full recording workbench. Every result includes a `surface` label to distinguish these modes.

The runner executes real Chromium clicks, input, native dialogs, keyboard and focus actions. Imports are bundled as data URLs; task-page location/history and API, localStorage, picker responses and completion events are synthetic. Network access is blocked and unexpected requests/page errors fail the case. This is frontend operation acceptance, not live backend or hardware end-to-end acceptance.

Delayed requests are held explicitly until another user action or completion event has occurred. Assertions inspect actual outgoing request identities, methods, headers, generations and payloads, as well as visible controls and drafts. Each case writes a screenshot, request/event log and Playwright trace. `results.json` records final outcomes; `cases-progress.json` preserves progress during execution.

## Coverage

| Cases | Count | Focus |
| --- | ---: | --- |
| 01-34 | 34 | Task filters, selection and bulk scope; partial failure; PDF draft/picker ownership; cancel; history; responsive bounds; focus |
| 40-47 | 8 | Inference settings save/test concurrency, edited drafts, validation and reload failure |
| 50-57 | 8 | MinerU draft preservation, collapsed invalid fields, blank numbers, defaults and tier controls |
| 60-65 | 6 | Backup read/create serialization and uncertain outcomes |
| 70-77 | 8 | Two-stage discard and stale POST/poll/completion ordering |
| 80-83 | 4 | Continuation request contract, errors and stale refresh |

Case numbering has gaps; there are 68 cases, not 83. On the October 16 audit baseline `4b50a9e2fbd8756b41cec378dfe026389ae81b36`, the same suite produced 21 passes and 47 failures. The patched production sources produced 68 passes in Chromium 144.0.7559.96. Failed cases share root causes and are not a count of independent bugs.

## Environment overrides

`QWEN_ASR_BROWSER_EXECUTABLE` selects an installed Chromium executable. `PLAYWRIGHT_MODULE` selects an installed Playwright package path. `QWEN_ASR_INDEX_FIXTURE` is only for reproducing the audit in a partial source snapshot: it must contain the native panel HTML extracted from the same baseline. Normal repository/CI runs read `static/index.html` directly and need no overrides.

## Remaining live verification

The suite does not verify microphone capture, actual Finder dialogs, archive downloads, native URL navigation/history, real filesystem migration, live WebSocket transport, Windows inference or ASR/MinerU model execution. These require the running application and relevant hardware. No production recordings, credentials or model configuration are used by this suite.
