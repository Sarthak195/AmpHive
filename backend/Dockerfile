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

# Drop root: run as a non-root system user (uid 10001). pip install above needs
# root (it writes to /usr/local); everything from here down runs unprivileged.
# The only path the app writes to at runtime is the firmware-image dir
# (FIRMWARE_IMAGE_DIR, default /app/data/firmware-images) — create it and hand
# /app to the app user so uvicorn starts and uploads can land. NOTE: a NAMED
# docker volume mounted at that path inherits this 10001 ownership only when it
# is EMPTY; a pre-existing root-owned firmware_images volume (e.g. on the live
# relay, created while this ran as root) must be chowned to 10001 on the host or
# firmware upload 500s with EACCES — see the compose-file security notes.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data/firmware-images \
    && chown -R app:app /app
USER app

# Expose FastAPI port
EXPOSE 8000

# Run FastAPI app with Uvicorn. Port 8000 is unprivileged, so this needs no
# Linux capabilities — the compose files run this service with cap_drop: [ALL]
# and no-new-privileges.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
