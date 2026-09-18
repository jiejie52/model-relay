from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerignore_does_not_exclude_sql_or_scripts():
    ignored = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "sql" not in ignored
    assert "scripts" not in ignored


def test_dockerfile_copies_runtime_support_directories():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY app ./app" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile
    assert "COPY sql ./sql" in dockerfile
    assert "python scripts/preflight_runtime.py" in dockerfile
    assert "import app.api, app.api_v2_app" in dockerfile
