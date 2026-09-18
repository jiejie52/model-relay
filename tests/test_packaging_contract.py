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


def test_dockerfile_has_explicit_boto_install_and_build_marker():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    build_info = (ROOT / "BUILD_INFO.txt").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "2.0.3-hotfix3" in build_info
    assert 'python -m pip install --no-cache-dir "boto3>=1.40,<2" "botocore>=1.40,<2"' in dockerfile
    assert "python -m pip show boto3 botocore" in dockerfile
    assert "material storage client OK" in dockerfile
    assert "boto3>=1.40,<2" in requirements
    assert "botocore>=1.40,<2" in requirements
