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

# Expose FastAPI port
EXPOSE 8000

# Run FastAPI app with Uvicorn
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
