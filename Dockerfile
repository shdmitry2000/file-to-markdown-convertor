# Base pinned by digest, not :latest. These images go through a scan gate, so a
# rebuild of the same commit must produce the same CVE profile; with a floating
# tag it silently would not. Bump this digest deliberately to pick up Red Hat
# errata — notably the sqlite-libs fix for CVE-2026-51302, unreleased as of
# 2026-08-19, which is the one critical these images still carry.
#-*--*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*#
#
#                                 |
#                                 |
#                                 |
#                                 |
#                                 |
#   This file was created by      |
#                                 |
#   Sisyphus the AI model         |
#                                 |
#                                 |
#                                 |
#                                 |
#                                 |
#                                 |
#
# -*--*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*#

# ---- Builder Stage ----
# This stage installs dependencies into a clean location.
#
# Base is RHEL9 UBI rather than Debian slim. Debian trixie ships perl-base with
# five criticals Debian has declined to fix ("postponed") plus a glibc scanf
# overflow marked <no-dsa>; UBI9 has no perl at all and glibc 2.34 under Red
# Hat's patch cadence. UBI is still glibc, so torch's manylinux_2_28 wheels
# (which have no musl build and no sdist) resolve exactly as before — that is
# why an Alpine base was never an option for this image.
FROM registry.access.redhat.com/ubi9/python-312-minimal@sha256:0682a7aa239c28eaad5187914699612daaa3b1fa63abddaee7ba46f5b76c3361 AS builder

USER 0
WORKDIR /app

# opencv's shared libraries, needed here and not only in the final stage: the
# post-install guard below imports cv2 to prove the rapidocr removal did not
# take it with it, and that import loads libxcb at build time.
RUN microdnf install -y \
    libgomp \
    mesa-libGL \
    glib2 \
    libxcb \
    libXext \
    libSM \
    libICE \
 && microdnf clean all

# Install uv, our build tool
RUN pip install --no-cache-dir uv

# Copy project file and app code for building
COPY pyproject.toml .
COPY ./app ./app

# Install ALL dependencies from pyproject.toml.
#
# Target /opt/app-root explicitly. UV_SYSTEM_PYTHON, which this used to set,
# resolves to the 3.12 under /usr on this base, while `python3` on PATH is the
# venv at /opt/app-root — so the install landed somewhere nothing imports from,
# and the final stage copies /opt/app-root. VENV is used by every uv call below.
ENV VENV=/opt/app-root/bin/python3
# Docling v2 pulls layout/table weights via Hugging Face — pin cache path for reproducible COPY.
ENV HF_HOME=/root/.cache/huggingface
RUN mkdir -p "${HF_HOME}"
RUN uv pip install --python $VENV --no-cache-dir .

# Drop rapidocr, the OCR engine. Nothing here calls it: no module imports it,
# every pipeline sets do_ocr=False, and the one way in was DOCLING_DO_OCR=true,
# which is off by default and was never viable for this corpus anyway since
# rapidocr ships no Hebrew model. Setting it true now raises at convert time
# rather than silently mis-reading Hebrew.
#
# Scope is rapidocr only. opencv-python and onnxruntime stay — onnxruntime is
# used by the Excel path, and opencv is left alone by request even though
# rapidocr was its main consumer here.
#
# To restore OCR, delete this block.
RUN uv pip uninstall --python $VENV rapidocr rapidocr-onnxruntime 2>/dev/null; \
    $VENV -c "import importlib.util as u, docling, onnxruntime; \
      assert u.find_spec('rapidocr') is None, 'rapidocr still present'; \
      import cv2; \
      print('rapidocr removed; docling + onnxruntime + cv2 intact')"

# Pre-download docling models (layout + table extraction, WITHOUT OCR)
# Match runtime API (DocumentConverter + PdfFormatOption + InputFormat).
RUN $VENV -c "\
from docling.document_converter import DocumentConverter, PdfFormatOption; \
from docling.datamodel.pipeline_options import PdfPipelineOptions; \
from docling.datamodel.base_models import InputFormat; \
opts = PdfPipelineOptions(); \
opts.do_ocr = False; \
opts.do_table_structure = True; \
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}); \
print('Docling models downloaded (layout + table, without OCR)'); \
"

# Bundle whatever landed under /root/.cache (HF hub, docling, etc.) for the runtime stage.
RUN mkdir -p /export/root-cache && \
    if [ -d /root/.cache ]; then cp -a /root/.cache/. /export/root-cache/; else true; fi


# ---- Final Stage ----
# This stage creates the final, small, production-ready image.
FROM registry.access.redhat.com/ubi9/python-312-minimal@sha256:0682a7aa239c28eaad5187914699612daaa3b1fa63abddaee7ba46f5b76c3361

USER 0

# Same runtime libraries as the Debian build, under their RHEL9 names: libgomp
# is torch's OpenMP runtime, the rest are what opencv links against. RHEL9's
# single libxcb carries the render/shape/xfixes extensions that Debian splits
# into libxcb-render0/-shape0/-xfixes0.
#
# curl is the one thing not carried over — nothing in the image shells out to it.
RUN microdnf install -y \
    libgomp \
    mesa-libGL \
    glib2 \
    libxcb \
    libXext \
    libSM \
    libICE \
 && microdnf clean all

# Copy the installed packages from the builder stage. This is the key to a small image.
COPY --from=builder /opt/app-root/lib/python3.12/site-packages /opt/app-root/lib/python3.12/site-packages
# Only the console script the CMD launches. The old `COPY /usr/local/bin` swept
# uv and pip along with it, and shipping those is what put their advisories in
# the scan; nothing at runtime invokes either.
COPY --from=builder /opt/app-root/bin/uvicorn /opt/app-root/bin/uvicorn
RUN rm -rf /opt/app-root/lib64/python3.12/site-packages/pip \
           /opt/app-root/lib64/python3.12/site-packages/pip-*.dist-info \
           /opt/app-root/bin/pip*

# UBI already provides uid 1001, gid 0, so there is no user to create — only a
# home to place. This still has to precede the cache COPY for the reason it
# always did: a directory that already exists is not re-chowned, which once left
# the cache root-owned and killed docling's HF download with EACCES on hub/.
# Group-writable so an arbitrary GKE UID can still write there. The rapidocr
# models directory that used to be pre-created 777 went with rapidocr itself.
RUN mkdir -p /home/appuser && chown 1001:0 /home/appuser && chmod g=u /home/appuser

# Copy pre-downloaded caches (HF / docling artifacts from builder)
COPY --from=builder --chown=1001:0 /export/root-cache/ /home/appuser/.cache/

# Match builder: docling uses Hugging Face hub for weights. The directory has to
# be writable by appuser — docling resolves weights lazily on first convert and
# writes them here, so a read-only cache fails every conversion, not just a cold one.
ENV HF_HOME=/home/appuser/.cache/huggingface
RUN mkdir -p "${HF_HOME}" && chown -R 1001:0 /home/appuser/.cache && chmod -R g=u /home/appuser/.cache

# This image has no C++ toolchain (slim base, no build-essential), and torch's
# inductor backend shells out to g++. Leaving dynamo on makes docling's layout
# model fail with InvalidCxxCompiler instead of falling back to eager.
ENV TORCHDYNAMO_DISABLE=1

# The OCR engine is uninstalled from this image (see the builder stage), so this
# is pinned off rather than left to worker.py's default. Flipping it true does
# not enable OCR — it makes docling raise on the first convert.
ENV DOCLING_DO_OCR=false

USER 1001
WORKDIR /home/appuser/app

# Copy the application code into the container
COPY --chown=1001:0 ./app /home/appuser/app

# Platform shared libs — only shared.llm_factory + shared.utils and their import-time
# dependencies (async_utils, connector_credentials, request_context, secret_box).
# Needed by app/converters/vlm.py (VLM_BACKEND=factory) and app/workers/worker.py.
# shared/db is deliberately not vendored: connector_credentials reaches it through a
# guarded lazy import that falls back to the global connector credentials.
#
# `share[d]` is an optional glob — present when built from the enterprise repo (the sync
# vendors it), absent in a standalone submodule build, where those two code paths remain
# unavailable exactly as they are today. pyproject.toml always matches, which is what lets
# a COPY containing a non-matching glob succeed; it is removed again below.
COPY --chown=1001:0 ./pyproject.toml ./share[d]/ /home/appuser/shared/
RUN if [ -d /home/appuser/shared/llm_factory ]; then \
      echo "shared libs: vendored"; \
    else \
      echo "shared libs: absent (standalone build — VLM_BACKEND=factory unavailable)"; \
    fi; \
    rm -f /home/appuser/shared/pyproject.toml

# Set PYTHONPATH so 'app' and 'shared' modules can be found
ENV PYTHONPATH=/home/appuser

# Expose the port the app runs on
EXPOSE 8000

# Specify the command to run on container startup
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
