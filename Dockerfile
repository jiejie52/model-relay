FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "import boto3, botocore, fastapi, httpx, pydantic; print('runtime dependencies OK')"

COPY app ./app
COPY scripts ./scripts
COPY sql ./sql

# Fail the image build early if the packaged application cannot import.
# Configuration variables are allowed to be absent here; preflight reports them as WARN.
RUN python scripts/preflight_runtime.py \
    && python -m compileall -q app \
    && RELAY_API_TOKEN=build-check SUPABASE_URL=https://example.invalid SUPABASE_SECRET_KEY=build-check python -c "import app.api, app.api_v2_app; print('application imports OK')"

# Default: compatibility API + V2 API.
# V2 Worker: python -m app.worker
# Material URL Worker: python -m app.material_worker
# Legacy/Fusion Worker during coexistence: python -m app.legacy_worker
# Core-only API alternative: uvicorn app.api_v2_app:app --host 0.0.0.0 --port $PORT
CMD ["sh", "-c", "uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
