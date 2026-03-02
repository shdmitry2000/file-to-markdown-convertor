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
FROM python:3.12-slim as builder

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

# Create a non-root user for better security
RUN useradd --create-home appuser
USER appuser
WORKDIR /home/appuser/app

# Copy the installed packages from the builder stage. This is the key to a small image.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
# Copy the executables (like uvicorn) from the builder stage.
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the application code into the container
COPY --chown=appuser:appuser ./app .

# Expose the port the app runs on
EXPOSE 8000

# Specify the command to run on container startup
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
