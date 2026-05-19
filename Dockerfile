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
FROM python:3.12-slim AS builder

# Install uv, our build tool
RUN pip install uv

# Copy project file and app code for building
COPY pyproject.toml .
COPY ./app ./app

# Install ALL dependencies from pyproject.toml
ENV UV_SYSTEM_PYTHON=true
# Docling v2 pulls layout/table weights via Hugging Face — pin cache path for reproducible COPY.
ENV HF_HOME=/root/.cache/huggingface
RUN mkdir -p "${HF_HOME}"
RUN uv pip install --no-cache-dir .

# Pre-download docling models (layout + table extraction, WITHOUT OCR)
# Match runtime API (DocumentConverter + PdfFormatOption + InputFormat).
RUN python3 -c "\
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
FROM python:3.12-slim

# Install system dependencies for OpenCV and document processing
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    libxcb1 \
    libxcb-render0 \
    libxcb-shape0 \
    libxcb-xfixes0 \
    libxext6 \
    libsm6 \
    libice6 \
    libglib2.0-0 \
    libgomp1 \
    libgl1 \
    libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Copy the installed packages from the builder stage. This is the key to a small image.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
# Copy the executables (like uvicorn) from the builder stage.
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy pre-downloaded caches (HF / docling artifacts from builder)
RUN mkdir -p /home/appuser/.cache
COPY --from=builder /export/root-cache/ /home/appuser/.cache/

# Match builder: docling uses Hugging Face hub for weights
ENV HF_HOME=/home/appuser/.cache/huggingface

# Create a non-root user for better security
RUN useradd --create-home appuser && \
    mkdir -p /usr/local/lib/python3.12/site-packages/rapidocr/models && \
    chmod -R 777 /usr/local/lib/python3.12/site-packages/rapidocr/models

USER appuser
WORKDIR /home/appuser/app

# Copy the application code into the container
COPY --chown=appuser:appuser ./app /home/appuser/app

# Set PYTHONPATH so 'app' module can be found
ENV PYTHONPATH=/home/appuser

# Expose the port the app runs on
EXPOSE 8000

# Specify the command to run on container startup
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
