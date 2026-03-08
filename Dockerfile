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

# Copy project file to cache dependency installation
COPY pyproject.toml .

# Install dependencies system-wide within this temporary stage.
# This makes it easy to find and copy the installed packages.
ENV UV_SYSTEM_PYTHON=true
RUN uv pip install --no-cache-dir \
    fastapi uvicorn pyzmq docling python-frontmatter


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

# Create a non-root user for better security
RUN useradd --create-home appuser && \
    mkdir -p /usr/local/lib/python3.12/site-packages/rapidocr/models && \
    chmod -R 777 /usr/local/lib/python3.12/site-packages/rapidocr/models

USER appuser
WORKDIR /home/appuser/app

# Copy the application code into the container
COPY --chown=appuser:appuser ./app .

# Expose the port the app runs on
EXPOSE 8000

# Specify the command to run on container startup
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
