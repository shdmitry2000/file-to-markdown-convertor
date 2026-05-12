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
RUN uv pip install --no-cache-dir .

# Pre-download docling models (layout + table extraction, WITHOUT OCR)
# Initialize DocumentConverter with OCR disabled to trigger selective model downloads.
# This downloads only: layout models + table extraction (~500-800MB), skips OCR models (~2GB).
RUN python3 -c "\
from docling.document_converter import DocumentConverter; \
from docling.datamodel.pipeline_options import PdfPipelineOptions; \
opts = PdfPipelineOptions(); \
opts.do_ocr = False; \
opts.do_table_structure = True; \
converter = DocumentConverter(pdf_options=opts); \
print('Docling models downloaded (layout + table, without OCR)'); \
"


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

# Copy pre-downloaded docling models (layout + table extraction, WITHOUT OCR)
# This makes the image production-ready with ~500-800MB of models instead of 2.5GB.
COPY --from=builder /root/.cache/docling /home/appuser/.cache/docling

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
