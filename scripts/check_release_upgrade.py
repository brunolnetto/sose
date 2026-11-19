"""Release gate: durable state written by the previous minor's wheel upgrades to this one.

Builds the previous minor from a git ref and writes a tutorial job that is mid-flight
(started, completion scheduled). If this release refuses the database as unowned
(SOSE <= 0.8, ADR-0009) it is migrated with ``sose migrate-owners``; otherwise it is
opened directly. This release's runtime must then finish the job.

usage: check_release_upgrade.py --previous-ref <git ref> --wheel <this release's wheel>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

OWNER = "upgrade"

# Works against any previous minor: pass owner_id only where that API takes it.
WRITE_PREVIOUS = """
import inspect, json, sys
from datetime import timedelta
from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import (
    ORIGIN, build_runtime, seed_job, start_and_schedule_completion,
)
from sose.persistence.sqlite import SQLitePersistence
owner = sys.argv[2]
def owned(function):
    return {"owner_id": owner} if "owner_id" in inspect.signature(function).parameters else {}
persistence = SQLitePersistence(sys.argv[1])
job = seed_job(persistence, **owned(seed_job))
_, engine = build_runtime(persistence, **owned(build_runtime))
engine.rebuild_backend(SimPyBackend(origin=ORIGIN))
start_and_schedule_completion(
    persistence, engine, job_id=job.id, complete_at=ORIGIN + timedelta(hours=2),
)
assert persistence.entity("tutorial_job", job.id).state == "running"
print(json.dumps({"job_id": job.id}))
"""

# Unowned (SOSE <= 0.8) databases are refused explicitly and must be migrated first.
NEEDS_MIGRATION = """
import sys
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_migrations import LegacyUnownedSnapshotError
try:
    SQLitePersistence(sys.argv[1]).close()
except LegacyUnownedSnapshotError:
    print("yes")
else:
    print("no")
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
        old_db = root / "previous.sqlite3"
        job_id = json.loads(run(str(previous), "-c", WRITE_PREVIOUS, str(old_db), OWNER))["job_id"]
        database = old_db
        if run(str(current), "-c", NEEDS_MIGRATION, str(old_db)).strip() == "yes":
            database = root / "upgraded.sqlite3"
            report = json.loads(run(
                str(current.parent / "sose"), "migrate-owners", "--sqlite", str(old_db),
                "--output", str(database), "--owner", OWNER,
            ))
            print(f"migrated {report['owned_records']} records; "
                  f"digest {report['semantic_digest']}")
        else:
            print("previous minor's database opens directly (already owner-qualified)")
        print(run(str(current), "-c", FINISH_CURRENT, str(database), job_id, OWNER).strip())

if __name__ == "__main__":
    main()
