"""Local deployment release pointer, atomic CAS under an OS lock."""
from pathlib import Path
from hashlib import sha256
import fcntl
import json
import os
import tempfile

def activate_release(*, pointer, release_path, expected_sha256):
    target = Path(pointer)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = Path(release_path).read_bytes()
    release = json.loads(content)
    if release.get("status") != "published" or not all(release.get("release_metadata", {}).get(k) for k in ("candidate_sha256", "approved_by", "approval_ref")):
        raise ValueError("Only approved published releases may be activated")
    fingerprint = sha256(content).hexdigest()
    with open(str(target)+".lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(target.read_text()) if target.exists() else {}
        if current.get("sha256", "") != expected_sha256:
            raise ValueError("Release pointer changed since review; activation rejected")
        retained = list(current.get("retained") or [])
        if current.get("path") and current.get("sha256"):
            retained.append({"path": current["path"], "sha256": current["sha256"]})
        retained = list({item["sha256"]: item for item in retained if item["sha256"] != fingerprint}.values())
        value = {"path": str(Path(release_path).resolve()), "sha256": fingerprint, "retained": retained,
            "previous": {k: current[k] for k in ("path", "sha256") if k in current}}
        fd, temporary = tempfile.mkstemp(prefix=target.name+"-", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, sort_keys=True); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return value
