FROM python:3.11-slim

WORKDIR /app

# Copy the pinned lockfile first to leverage Docker cache. requirements.txt
# stays the human-edited source of truth (see backend/requirements.lock.txt
# for regeneration steps); the image installs the fully-pinned lockfile.
COPY requirements.lock.txt .

# Install packages
RUN pip install --no-cache-dir -r requirements.lock.txt

# Copy backend directory
COPY . /app/backend

# Run as a fixed non-root uid/gid (10001) so a code-execution bug in the app
# does not land as root in the container. The code stays root-owned and
# world-readable (the app never writes to it); the ONE tree the app may touch
# is /app/data (legacy OTA image store, FIRMWARE_IMAGE_DIR, mounted from the
# `firmware_images` named volume in production). The uid is pinned because the
# live volume must be chowned to it once (docs/DEPLOYMENT.md, "Non-root
# containers" runbook).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && chmod -R a+rX /app \
    && mkdir -p /app/data/firmware-images \
    && chown -R 10001:10001 /app/data
USER 10001:10001

# Expose FastAPI port (>1024, so no privileged-port capability is needed)
EXPOSE 8000

# Run FastAPI app with Uvicorn
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
