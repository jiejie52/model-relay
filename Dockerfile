FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy build identity/dependency inputs first. These log lines are diagnostic only;
# a mixed/stale requirements.txt must not stop the build before the explicit
# material-storage dependency install below can run.
COPY BUILD_INFO.txt ./
COPY requirements.txt ./

RUN echo "=== MODEL RELAY BUILD MARKER ===" \
    && cat BUILD_INFO.txt \
    && echo "=== requirements.txt ===" \
    && cat requirements.txt \
    && echo "=== boto requirements (diagnostic; non-fatal) ===" \
    && (grep -E '^(boto3|botocore)' requirements.txt || echo "WARN: boto3/botocore not present in build-context requirements.txt; Dockerfile will install them explicitly")

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Critical runtime packages are explicit here on purpose. This protects Railway
# from a mixed source tree where Dockerfile is new but requirements.txt is stale.
RUN python -m pip install --no-cache-dir \
        "boto3>=1.40,<2" \
        "botocore>=1.40,<2" \
        "python-multipart>=0.0.20,<1" \
    && python -m pip show boto3 botocore python-multipart \
    && python -c "import boto3, botocore, multipart; print('material storage/runtime clients OK', boto3.__version__, botocore.__version__)"

# Install the normal application dependency set. If build-context requirements.txt
# is an older compatible copy, the explicit packages above remain installed.
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "import boto3, botocore, fastapi, httpx, pydantic, multipart; print('runtime dependencies OK')"

COPY app ./app
COPY scripts ./scripts
COPY sql ./sql

RUN python scripts/preflight_runtime.py \
    && python -m compileall -q app \
    && RELAY_API_TOKEN=build-check SUPABASE_URL=https://example.invalid SUPABASE_SECRET_KEY=build-check python -c "import app.api, app.api_v2_app; print('application imports OK')"

CMD ["sh", "-c", "uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
