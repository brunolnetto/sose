from __future__ import annotations

import argparse
from pathlib import Path
import tomllib


def project_version(root: Path) -> str:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def validate(root: Path, *, tag: str | None) -> None:
    version = project_version(root)

    init_ns: dict[str, object] = {}
    exec((root / "src" / "sose" / "__init__.py").read_text(encoding="utf-8"), init_ns)
    runtime_version = str(init_ns["__version__"])
    if runtime_version != version:
        raise SystemExit(
            f"version mismatch: pyproject={version}, sose.__version__={runtime_version}"
        )

    if tag:
        expected_tag = f"v{version}"
        if tag != expected_tag:
            raise SystemExit(f"release tag mismatch: expected {expected_tag}, got {tag}")
        release_note = root / "docs" / "releases" / f"{tag}.md"
        if not release_note.is_file():
            raise SystemExit(f"missing completed release note: {release_note}")

    print(f"release candidate metadata valid for sose {version}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    args = parser.parse_args()
    validate(Path(__file__).resolve().parents[1], tag=args.tag)


if __name__ == "__main__":
    main()
