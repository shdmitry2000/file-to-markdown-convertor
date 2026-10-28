"""Describe the pictures inside Word and PowerPoint files with our VLM.

docling turns every embedded picture into ``<!-- image -->`` and keeps nothing of
what it shows, so a chart or a scanned page pasted into a .docx was invisible to
search. docling has a picture-description stage for exactly this, run by its
simple pipeline (docx, pptx) after the backend; its built-in "api" engine needs
an OpenAI-compatible URL, which the platform lanes do not expose. So this module
registers our own engine (kind ``ragp_vlm``) that sends each picture through
``VLMConverter.transcribe_page`` — the same LLMFactory client, model (VLM_MODEL)
and retry/rate-limit handling as the dbank and slides lanes. No OCR anywhere.

docling prints the description right after the placeholder, also inside table
cells, so it is collapsed to one line. A picture that fails to describe gets no
description and never fails the document.

DOCLING_DESCRIBE_PICTURES=false turns it off (default on).
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, ClassVar, Iterable, Literal

from docling.datamodel.pipeline_options import PictureDescriptionBaseOptions
from docling.models.picture_description_base_model import PictureDescriptionBaseModel

logger = logging.getLogger(__name__)

KIND = "ragp_vlm"

_PROMPT = """Describe this picture from a document so it can be found by search.

- If it contains text, transcribe the text exactly, in its original language and \
logical reading order (Hebrew stays Hebrew, right to left; do not translate).
- For a chart or table, give the title, labels and the numbers.
- For a photo or diagram, say briefly what it shows.
- A logo, icon or decoration: answer with its name or one or two words.

Plain text in one paragraph. No markdown, no preamble."""

# Icons, bullets and spacer images: not worth a VLM call.
_MIN_SIDE_PX = 48

#: Set by the worker (_install_page_progress): called every _BEAT_SECONDS while
#: pictures are being described, so the stall watch sees the work moving.
on_progress: Callable[[], None] | None = None
_BEAT_SECONDS = 2.0


def enabled() -> bool:
    return os.getenv("DOCLING_DESCRIBE_PICTURES", "true").lower() in ("true", "1", "yes")


class VlmPictureDescriptionOptions(PictureDescriptionBaseOptions):
    kind: ClassVar[Literal["ragp_vlm"]] = KIND
    concurrency: int = int(os.getenv("DOCLING_PICTURE_CONCURRENCY", "4"))


class VlmPictureDescriptionModel(PictureDescriptionBaseModel):
    @classmethod
    def get_options_type(cls):
        return VlmPictureDescriptionOptions

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.provenance = KIND
        self._vlm = None  # built on first use: a lane without VLM creds still converts

    def _describe(self, image) -> str:
        if min(image.size) < _MIN_SIDE_PX:
            return ""
        try:
            if self._vlm is None:
                from app.converters.vlm import VLMConverter
                self._vlm = VLMConverter(backend="factory", user_prompt=_PROMPT)
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            text = self._vlm.transcribe_page(base64.b64encode(buf.getvalue()).decode())
        except Exception as exc:  # noqa: BLE001 — a picture never fails the document
            logger.warning("picture description failed (%s: %s)", type(exc).__name__, str(exc)[:200])
            return ""
        return re.sub(r"\s+", " ", text).replace("|", "/").strip()

    def _annotate_images(self, images: Iterable) -> Iterable[str]:
        # The worker's stall watch counts docling's PDF page handoffs, and a docx
        # has none: without a beat, pictures waiting on the VLM read as a stuck
        # conversion. A VLM call is bounded by its own retry budget, and the
        # whole job by DOCLING_TIMEOUT_SECONDS.
        done = threading.Event()

        def beat():
            while not done.wait(_BEAT_SECONDS):
                if on_progress is not None:
                    on_progress()

        threading.Thread(target=beat, daemon=True).start()
        try:
            with ThreadPoolExecutor(max_workers=self.options.concurrency) as ex:
                results = list(ex.map(self._describe, images))
        finally:
            done.set()
        yield from results


def register() -> None:
    """Add the engine to docling's picture-description factory (idempotent).

    The factory is an lru_cache keyed by the exact call; the pipeline calls it as
    ``get_picture_description_factory(allow_external_plugins=False)``, so we must too.
    """
    from docling.models.factories import get_picture_description_factory

    factory = get_picture_description_factory(allow_external_plugins=False)
    if KIND not in factory.registered_kind:
        factory.register(VlmPictureDescriptionModel, "ragp", __name__)


def office_format_options() -> dict:
    """docling ``format_options`` for docx and pptx with picture description on,
    or ``{}`` when DOCLING_DESCRIBE_PICTURES is off (docling's defaults then apply)."""
    if not enabled():
        return {}
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import ConvertPipelineOptions
    from docling.document_converter import PowerpointFormatOption, WordFormatOption

    register()
    opts = ConvertPipelineOptions(
        do_picture_description=True,
        picture_description_options=VlmPictureDescriptionOptions(),
    )
    return {
        InputFormat.DOCX: WordFormatOption(pipeline_options=opts),
        InputFormat.PPTX: PowerpointFormatOption(pipeline_options=opts),
    }
