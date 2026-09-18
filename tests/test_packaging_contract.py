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


def test_dockerfile_has_explicit_runtime_install_and_nonfatal_dependency_diagnostic():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    build_info = (ROOT / "BUILD_INFO.txt").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "2.0.4-hotfix4" in build_info
    assert '"boto3>=1.40,<2"' in dockerfile
    assert '"botocore>=1.40,<2"' in dockerfile
    assert '"python-multipart>=0.0.20,<1"' in dockerfile
    assert "python -m pip show boto3 botocore python-multipart" in dockerfile
    assert "material storage/runtime clients OK" in dockerfile
    assert "diagnostic; non-fatal" in dockerfile
    assert "WARN: boto3/botocore not present" in dockerfile
    assert "boto3>=1.40,<2" in requirements
    assert "botocore>=1.40,<2" in requirements
    assert "python-multipart>=0.0.20,<1" in requirements
