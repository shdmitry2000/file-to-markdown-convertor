# Runtime scenarios

Observable journeys for the converter, as it works today.
Shared runtime verification owns execution and evidence.

**How to submit a file (all journeys):** copy it under `PROJECTS_BASE_PATH`, then
`POST /convert` with JSON `{"file_path": "<absolute path under PROJECTS_BASE_PATH>"}`.
The response is `{"conversion_id": "..."}`. The first Docling conversion after a cold
start can take ~5 minutes while models download; allow 10 minutes.

## A PDF becomes Markdown

1. Submit `files_to_convert/simple.pdf`.
2. Poll `GET /convert/{conversion_id}` until the status is `completed`.
3. `GET /convert/{conversion_id}/content`.

**What would make this fail:** the body is empty or only frontmatter, the text is not
from the PDF, or the status never leaves `pending`/`processing`.

## Excel keeps its formulas

1. Convert `tests/fixtures/excel/edge_cases.xlsx`.
2. Fetch the content.

**What would make this fail:** the table is missing, there is no formulas section with
the real formula text and its cached result, or a value with no cached result shows a
number instead of `(no cached result)`.

## Cancelling is not failing

1. Start converting `files_to_convert/439.pdf`.
2. Cancel it with `POST /debug/queue/{conversion_id}/cancel`.
3. Poll its status, then check `/health`, then convert `simple.pdf`.

**What would make this fail:** the status is `failed` instead of `cancelled`, `/health`
is unhealthy, or the next conversion does not complete.

## Same file, same job

1. Submit `files_to_convert/439.pdf`.
2. Submit it again while the first is still pending or processing.

**What would make this fail:** the two responses have different `conversion_id`s.

## Chunking

1. Convert `simple.pdf` and fetch the Markdown.
2. `POST /chunk` with that Markdown.

**What would make this fail:** no chunks, or any chunk with empty text.
