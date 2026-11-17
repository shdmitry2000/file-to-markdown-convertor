# Holdout scenarios

Acceptance checks graded against the product's intent. Builders never read this file.
Sources: README.md and docs/ (MISSION.md and harness/END-TO-END.md were named as sources but
do not exist in the repository at the time of writing).

## H-01 A cancelled queued document never reaches the converter
Protects: README "Cancel a Conversion" - a job that has not started is dropped before it reaches the converter; cancelled is not failed
Touches: POST /convert, DELETE /convert/{id}, GET /convert/{id}, /debug/queue, converted output folder, worker
Setup: Service running with exactly one worker. A large PDF (several minutes to convert) and a second, different small PDF are in the input folder.
Steps: Submit the large PDF, wait until it is reported converting. Submit the small PDF; while it is still queued, cancel it. Let the large PDF finish. Wait a further 60 seconds.
Expect: The small PDF's status is cancelled (not failed, not completed) on every read, before and after the large PDF finishes. It never appears as converting in GET /convert/{id} or /debug/queue. No markdown file for the small PDF appears in the converted folder, and worker logs show no conversion attempt for it. The large PDF completes normally.
Author: writer, 2026-11-17, initial holdout set from README cancellation rules

## H-02 Cancelling a running conversion frees the worker for the next document
Protects: README "Cancel a Conversion" - a converting job has its converter subprocess killed so the slot returns to the pool
Touches: DELETE /convert/{id}, worker pool, converter subprocess, GET /convert/{id}
Setup: Service running with exactly one worker. A large PDF (minutes to convert) and a small PDF.
Steps: Submit the large PDF; once it reports converting, submit the small PDF (now queued behind it). Cancel the large PDF. Time how long until the small PDF completes.
Expect: The large PDF ends as cancelled, not failed. No converter process for it survives the cancel (process list shows it gone within a few seconds). The small PDF starts and completes without waiting for the large one's natural finish time (well under the large PDF's normal conversion time; a cold-start of roughly 10-12 s is acceptable). No partial markdown file for the large PDF is left in the converted folder as if complete.
Author: writer, 2026-11-17, initial holdout set from README cancellation rules

## H-03 One caller cancelling does not stop another caller's conversion
Protects: README "Conversions are shared between callers" - work stops only when the last caller has cancelled; until then the real status is reported
Touches: POST /convert (twice, same file), DELETE /convert/{id}, GET /convert/{id}
Setup: Service running; one PDF that takes at least a minute to convert.
Steps: Caller A and caller B each submit the same file. Caller A cancels. Read the status repeatedly until it settles. Repeat the whole sequence with a fresh identical file, this time with both A and B cancelling.
Expect: Both callers receive the same conversion id. In the first run, A's cancel response and every subsequent status read report the real status (queued/converting, then completed), never cancelled, and the markdown is produced. In the second run, after B also cancels, the conversion ends cancelled and the converter stops. Cancelling the same caller twice must not count as two callers.
Author: writer, 2026-11-17, initial holdout set from README cancellation rules

## H-04 A document that was cancelled converts normally when submitted again
Protects: README "Cancellation" - terminal statuses clear their own marker; stale markers are swept; a marker never stops a job it was not meant for
Touches: DELETE /convert/{id}, POST /convert, cancel marker directory, API restart, sweeper
Setup: Service running with one worker; CANCEL_MARKER_TTL_SECONDS and CANCEL_MARKER_SWEEP_INTERVAL_SECONDS set short (e.g. 30 s / 10 s) for one variant, defaults for the others.
Steps: (a) Submit a PDF, cancel it while converting, wait for cancelled, immediately submit the same PDF again. (b) Submit a PDF while the only worker is busy, cancel it while queued, restart the API, then submit the same PDF again. (c) With short TTL, create a cancel for a job that is never dispatched, wait past TTL plus one sweep interval.
Expect: In (a) and (b) the resubmitted conversion completes and produces markdown on the very next request; it is never reported cancelled because of the earlier cancel. In (c) the marker is gone from the cancel marker directory after the TTL and sweep. After every terminal status, no marker for that conversion remains.
Author: writer, 2026-11-17, initial holdout set from README cancellation rules

## H-05 A token sent with a conversion never appears anywhere
Protects: README "Sending an LLM with a conversion" - the token is never logged, stored or written into the output; README "/llm" never shows the token
Touches: POST /convert, /debug/convert, GET /convert/{id}, /debug/queue, GET /llm, /health, converted markdown and frontmatter, API and worker logs, error messages
Setup: Service running with an environment LLM whose PROXY_AUTH_TOKEN is a distinctive string T1. A distinctive per-request token T2.
Steps: Submit a dbank (or other image-reading) conversion with llm.auth_token = T2, once against a working proxy and once against an unreachable base_url so that it fails. Do the same through /debug/convert with the llm form field. Read every status, queue, health and /llm response; fetch the markdown; collect all API and worker logs at DEBUG level; list the converted and cancel-marker folders.
Expect: Neither T1 nor T2 appears (in full or as a recognisable substring) in any response body, any log line, any file in the converted folder, any frontmatter, or any failure reason. GET /llm reports only that a token is set.
Author: writer, 2026-11-17, initial holdout set from README LLM rules

## H-06 A "space" LLM uses exactly what was sent, never the environment
Protects: README "mode: space" - use exactly this LLM, nothing from markdown-api's environment; a missing required field fails the conversion and names it
Touches: POST /convert, /debug/convert, environment LLM, GET /convert/{id}
Setup: Environment LLM configured and reachable (requests to it are observable, e.g. via a recording proxy). A second recording proxy for the request's LLM.
Steps: Submit an image-reading conversion with mode "space" and a complete vertex_proxy object pointing at the second proxy. Then submit one with mode "space" missing connector_id. Then the same through /debug/convert.
Expect: The complete request's LLM calls all go to the second proxy; the environment LLM receives zero calls. The incomplete request ends failed with a reason that names the missing field, and the environment LLM still receives zero calls (no silent fallback).
Author: writer, 2026-11-17, initial holdout set from README LLM rules

## H-07 A per-request model does not stick to later conversions
Protects: README "Choosing the LLM" - decided per conversion; "params" mode overlays the converter LLM; the converter LLM is the environment's
Touches: POST /convert, GET /llm, environment LLM
Setup: Environment LLM with LLM_TYPE = model M1, observable via a recording proxy.
Steps: Submit an image-reading conversion with llm {"model": "<type>/M2"} (no mode). Immediately after, submit another image-reading conversion with no llm. Also run two such conversions concurrently, one with M2 and one without. Then read GET /llm.
Expect: The first conversion's calls use M2 on the environment's connection; the next conversion and the concurrent no-llm conversion use M1 only. GET /llm still reports M1 throughout.
Author: writer, 2026-11-17, initial holdout set from README LLM rules

## H-08 The converter LLM cannot be changed at runtime by any route
Protects: README - changing the converter LLM at runtime (PUT / DELETE /llm, LLM_RUNTIME_CHANGE) was removed in 1.4.8.5
Touches: /llm (PUT, DELETE, POST, PATCH), LLM_RUNTIME_CHANGE env, GET /llm, conversions
Setup: Service running with environment LLM M1; for one variant also start with LLM_RUNTIME_CHANGE=true.
Steps: Attempt PUT /llm, DELETE /llm, PATCH /llm and POST /llm with a body naming model M2 and a different base URL. Then read GET /llm and run an image-reading conversion with no llm.
Expect: None of the attempts succeeds; GET /llm still reports M1 and the original address; the conversion's calls go to M1 at the original address. Same result with LLM_RUNTIME_CHANGE=true.
Author: writer, 2026-11-17, initial holdout set from README LLM rules

## H-09 Excel totals keep their formulas, never recalculated, never via an LLM
Protects: README "Excel: formulas, not just numbers" - formula section with named terms, dependency section, one table per region, no LLM called, nothing recalculated, "(no cached result)"
Touches: POST /convert on .xlsx and .xlsm, excel converter, converted markdown, LLM
Setup: LLM configured to an unreachable address (any call would be visible as an error or delay). A workbook with: one sheet holding two separate blocks of cells; a total cell =SUM over five labelled rows with a cached value; a formula whose result was never saved by Excel although its inputs are all present; a cell referenced by a formula on another sheet. Same workbook saved as .xlsm.
Steps: Convert both files and fetch the markdown.
Expect: Each block renders as its own table. The total appears in a formulas section with its formula, its cached value, and its expanded terms carrying human names (not only cell addresses). The unsaved formula shows "(no cached result)" and no computed number anywhere. The cross-sheet input appears in a dependencies section pointing at the other sheet's cell. Conversion succeeds with no LLM call attempted. .xlsm output matches .xlsx output in these respects.
Author: writer, 2026-11-17, initial holdout set from README Excel rules

## H-10 Excel tuning limits are honoured exactly
Protects: README Excel tuning - EXCEL_MAX_REFS_PER_FORMULA terms then "+ …", EXCEL_EMIT_DEPENDENCIES=false drops dependencies, EXCEL_MAX_CELLS marks truncated sheets; legacy .xls stays on markitdown
Touches: excel converter, environment settings, converted markdown
Setup: A workbook with a formula summing 30 distinct cells, and a second sheet larger than 1000 cells. The same content saved as legacy .xls.
Steps: Convert with defaults; with EXCEL_MAX_REFS_PER_FORMULA=3; with EXCEL_EMIT_DEPENDENCIES=false; with EXCEL_MAX_CELLS=1000. Convert the .xls. Record, per run, the number of expanded terms on the 30-cell formula, whether "+ …" follows, whether a dependencies section exists, and whether the large sheet is marked truncated, as JSON for the judge.
Expect: Defaults: exactly 20 terms then "+ …". Limit 3: exactly 3 terms then "+ …". Dependencies off: no dependencies section, formulas section still present. Cell budget: the large sheet is marked truncated. The .xls converts successfully without a formulas section. Judge: .factory/holdout/judges/h10_excel_limits.py
Author: writer, 2026-11-17, initial holdout set from README Excel rules

## H-11 Time spent queued never counts against a conversion's limits
Protects: README "Conversion limits" - bounded only in the worker from the moment it takes the job; a long conversion is not a stuck one
Touches: worker, DOCLING_TIMEOUT_SECONDS, DOCLING_STALL_SECONDS, GET /convert/{id} elapsed_seconds and pages_done
Setup: One worker; DOCLING_TIMEOUT_SECONDS=120; a PDF A that takes about 100 s, a PDF B that takes about 60 s; a PDF C that takes about 300 s for a third variant.
Steps: Submit A then B immediately (B waits ~100 s queued). Poll both. Separately, submit C.
Expect: A and B both complete (B's total wall time ~160 s exceeds 120 s, but it is not cut). B's elapsed_seconds while converting counts from when it was taken, not submitted. Docling conversions report a growing pages_done. C is cut at about 120 s by the hard ceiling and ends failed (not cancelled), and the worker then takes the next job.
Author: writer, 2026-11-17, initial holdout set from README conversion limits

## H-12 Status reads agree on every surface
Protects: README status model - pending, processing, completed, failed, cancelled; /debug/queue lists pending and active conversions
Touches: GET /convert/{id}, /debug/queue, DELETE /convert/{id}, POST /convert
Setup: One worker, three PDFs.
Steps: Submit all three; poll GET /convert/{id} for each and /debug/queue at the same moments; cancel the third while queued; let the rest finish. Also query an unknown conversion id.
Expect: At every moment the status per id from GET /convert/{id} matches its presence in /debug/queue (queued ones listed as pending, the running one as active, terminal ones absent). Only documented statuses appear. An unknown id is reported as not found, never as pending.
Author: writer, 2026-11-17, initial holdout set from README API endpoints

## H-13 The debug upload returns an id at once and the result can be polled
Protects: README "Conversion limits" - POST /debug/convert returns the id at once (wait=false is the default); callers poll
Touches: POST /debug/convert, GET /convert/{id}, /converted/{path}
Setup: A PDF that takes at least 60 s to convert.
Steps: Upload it through /debug/convert without a wait parameter, timing the response. Poll GET /convert/{id} until terminal and fetch the result.
Expect: The response arrives within a few seconds carrying a conversion id; the conversion then completes and the markdown is retrievable. A client with a 30 s timeout never sees a cut connection.
Author: writer, 2026-11-17, initial holdout set from README debug endpoint

## H-14 No route reads a file outside the service's own folders
Protects: README - files come from files_to_convert and converted files are served from the converted folder only
Touches: POST /convert file_path, /debug/convert, GET /converted/{path}, GET /convert/{id}
Setup: Service running; a secret file outside both folders (e.g. /etc/passwd and a marker file in the API's home directory); a symlink inside files_to_convert pointing at the marker file; a symlink inside the converted folder pointing at it.
Steps: Request conversion with file_path values using "../", absolute paths, URL-encoded and double-encoded traversal, and the symlink. Request /converted/ with the same traversal forms and the converted-folder symlink.
Expect: Every attempt is refused (or not found); no response, status reason, or converted file contains the secret file's content or confirms its existence by echoing its contents. Legitimate nested paths still work.
Author: writer, 2026-11-17, initial holdout set from README file rules

## H-15 Output mirrors the input tree and carries matching frontmatter
Protects: README "Output Structure" and "Metadata Format"
Touches: POST /convert, converted folder, GET /converted/{path}, frontmatter
Setup: files_to_convert/project1/sub/doc.pdf, files_to_convert/project1/doc.docx, and a .xlsx in another subfolder.
Steps: Convert all three; fetch each result via /converted with the mirrored relative path.
Expect: Each .md lands at the same relative path under the converted folder. Each has frontmatter whose conversion_id equals the id returned at submission, whose source_file names the input, and with a conversion_date. Converting the PDF a second time later does not leave a stale conversion_id in the file.
Author: writer, 2026-11-17, initial holdout set from README output rules

## H-16 Embedded workers come back after a crash and conversions continue
Protects: README "Embedded workers" - a worker that exits is restarted; all stop with the API; /health reports configured, running, restarts
Touches: MARKDOWN_EMBEDDED_WORKERS, /health, worker processes, POST /convert
Setup: One container with MARKDOWN_EMBEDDED_WORKERS=2 and only port 8000 exposed.
Steps: Convert a PDF. Kill one worker process. Read /health over the next 30 s. Convert two more PDFs concurrently. Stop the API.
Expect: /health shows configured 2, running returns to 2, restarts incremented by 1. All conversions complete. After the API stops no worker process remains. With the variable unset or 0, no workers are started inside the API.
Author: writer, 2026-11-17, initial holdout set from README embedded workers

## H-17 The image converts documents with no network at all
Protects: README "Air-gapped images" - nothing is downloaded at runtime; non-baked tokenizer fails with a clear message
Touches: container image, docling converter, docling_hybrid, /health models.docling_models_baked
Setup: Built image run with --network none and an empty HF cache volume.
Steps: Read /health. Convert a text PDF with docling and with docling_hybrid using the baked tokenizer id. Convert with docling_hybrid naming a different Hub tokenizer id.
Expect: /health reports docling models baked. Both conversions complete with no network attempt and nothing written into the HF cache. The other tokenizer id fails with a message naming the problem rather than hanging or retrying.
Author: writer, 2026-11-17, initial holdout set from README air-gapped rules

## H-18 LLM failures are retried on the documented schedule and a daily cap stops at once
Protects: README shared client retries - 429/503 back off 5 s doubling to 30 s, 180 s total, Retry-After wins; timeout/5xx retried twice (1 s, 2 s); "tokens per day" stops the conversion at once and queued pages are cancelled; POST /llm/test makes one try
Touches: image-reading converters (dbank, slides), POST /llm/test, LLM proxy
Setup: A stub LLM endpoint that records each request's arrival time and can be scripted to return 429, 503, 500, a Retry-After header, or a "tokens per day" error.
Steps: Run a one-page dbank conversion against: always 429; always 500; 429 with Retry-After: 12; a "tokens per day" error on the first call of a 20-page document. Call POST /llm/test against always 429. Record request timestamps per run as JSON for the judge.
Expect: 429: gaps start near 5 s, double, cap at 30 s, and retrying stops near 180 s total. 500: exactly 3 requests with gaps near 1 s and 2 s. Retry-After: the first gap honours 12 s. Token cap: the conversion fails at once, with no further requests for the remaining pages. /llm/test: exactly one request and a reported failure. Judge: .factory/holdout/judges/h18_retry_schedule.py
Author: writer, 2026-11-17, initial holdout set from README LLM retry rules

## H-19 Existing callers keep working unchanged
Protects: README - the older llm_connector field still works as before; minimal {"file_path"} request; documented response shapes
Touches: POST /convert, GET /convert/{id}, GET /converted/{path}, /health
Setup: Service running with a configured environment LLM.
Steps: Submit a request with only file_path. Submit a request using the older llm_connector field and no llm. Restart the API and workers, then fetch a markdown produced before the restart.
Expect: Both requests return a conversion_id and complete. The llm_connector request uses the connector it names. The pre-restart markdown is still retrievable after the restart. /health answers throughout once started.
Author: writer, 2026-11-17, initial holdout set from README backward-compatible fields
