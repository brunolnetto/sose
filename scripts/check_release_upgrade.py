"""Release gate: durable state written by the previous minor's wheel upgrades to this one.

Builds the previous minor from a git ref, writes a tutorial job that is mid-flight (started,
completion scheduled), migrates the database with this release's ``sose migrate-owners``
(ADR-0009: SOSE <= 0.8 data is unowned) and finishes the job with this release's runtime.

usage: check_release_upgrade.py --previous-ref <git ref> --wheel <this release's wheel>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

WRITE_PREVIOUS = """
import json, sys
from datetime import timedelta
from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import (
    ORIGIN, build_runtime, seed_job, start_and_schedule_completion,
)
from sose.persistence.sqlite import SQLitePersistence
persistence = SQLitePersistence(sys.argv[1])
job = seed_job(persistence)
_, engine = build_runtime(persistence)
engine.rebuild_backend(SimPyBackend(origin=ORIGIN))
start_and_schedule_completion(
    persistence, engine, job_id=job.id, complete_at=ORIGIN + timedelta(hours=2),
)
assert persistence.entity("tutorial_job", job.id).state == "running"
print(json.dumps({"job_id": job.id}))
"""

FINISH_CURRENT = """
import sys
from datetime import timedelta
from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import ORIGIN, build_runtime
from sose.persistence.sqlite import SQLitePersistence
database, job_id, owner = sys.argv[1:4]
with SQLitePersistence(database) as persistence:
    assert persistence.entity("tutorial_job", job_id).state == "running"
    _, engine = build_runtime(persistence, owner_id=owner)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN + timedelta(hours=2))
    assert persistence.entity("tutorial_job", job_id).state == "completed"
    assert persistence.scheduled_work() == ()
print("upgraded job completed under the new runtime")
"""


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout


def venv_with(wheel: Path, root: Path, name: str) -> Path:
    python = root / name / "bin" / "python"
    run(sys.executable, "-m", "venv", str(root / name))
    run(str(python), "-m", "pip", "install", "--quiet", f"{wheel}[simpy]")
    return python


def build_previous_wheel(ref: str, root: Path) -> Path:
    source = root / "previous-source"
    source.mkdir()
    archive = subprocess.run(["git", "archive", ref], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(source)], input=archive, check=True)
    run(sys.executable, "-m", "build", "--wheel", "--outdir", str(root / "previous-dist"),
        cwd=source)
    return next((root / "previous-dist").glob("*.whl"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-ref", required=True)
    parser.add_argument("--wheel", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        previous = venv_with(build_previous_wheel(args.previous_ref, root), root, "previous")
        current = venv_with(args.wheel.resolve(), root, "current")
        old_db, new_db, owner = root / "previous.sqlite3", root / "upgraded.sqlite3", "upgrade"
        job_id = json.loads(run(str(previous), "-c", WRITE_PREVIOUS, str(old_db)))["job_id"]
        report = json.loads(run(
            str(current.parent / "sose"), "migrate-owners", "--sqlite", str(old_db),
            "--output", str(new_db), "--owner", owner,
        ))
        print(f"migrated {report['owned_records']} records; digest {report['semantic_digest']}")
        print(run(str(current), "-c", FINISH_CURRENT, str(new_db), job_id, owner).strip())


if __name__ == "__main__":
    main()
