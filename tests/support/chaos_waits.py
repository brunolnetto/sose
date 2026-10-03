from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
import time
from typing import TypeVar


_T = TypeVar("_T")


def wait_for_marker_text(
    marker: Path,
    process: subprocess.Popen,
    *,
    timeout: float,
    premature_exit_label: str,
    timeout_label: str,
    termination_timeout: float,
) -> str:
    return wait_for_marker_parsed(
        marker,
        process,
        timeout=timeout,
        parser=lambda payload: payload,
        premature_exit_label=premature_exit_label,
        timeout_label=timeout_label,
        termination_timeout=termination_timeout,
    )


def wait_for_marker_exists(
    marker: Path,
    process: subprocess.Popen,
    *,
    timeout: float,
    premature_exit_label: str,
    timeout_label: str,
    termination_timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"{premature_exit_label} (code={process.returncode})\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.01)

    process.kill()
    stdout, stderr = process.communicate(timeout=termination_timeout)
    raise TimeoutError(
        f"{timeout_label} (timeout={timeout}s): {marker}\n"
        f"stdout={stdout}\nstderr={stderr}"
    )


def wait_for_marker_parsed(
    marker: Path,
    process: subprocess.Popen,
    *,
    timeout: float,
    parser: Callable[[str], _T | None],
    premature_exit_label: str,
    timeout_label: str,
    termination_timeout: float,
) -> _T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            payload = marker.read_text(encoding="utf-8").strip()
            parsed = parser(payload)
            if parsed is not None:
                return parsed
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"{premature_exit_label} (code={process.returncode})\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.01)

    process.kill()
    stdout, stderr = process.communicate(timeout=termination_timeout)
    raise TimeoutError(
        f"{timeout_label} (timeout={timeout}s): {marker}\n"
        f"stdout={stdout}\nstderr={stderr}"
    )


def wait_for_process_exit(
    process: subprocess.Popen,
    *,
    timeout: float,
    label: str,
    termination_timeout: float,
) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate(timeout=termination_timeout)
        raise AssertionError(
            f"{label} did not exit in time (timeout={timeout}s)\n"
            f"stdout={stdout}\nstderr={stderr}"
        ) from exc


def wait_for_process_exit_or_kill(
    process: subprocess.Popen,
    *,
    timeout: float,
    termination_timeout: float,
) -> tuple[bool, str, str]:
    try:
        process.wait(timeout=timeout)
        stdout, stderr = process.communicate(timeout=termination_timeout)
        return False, stdout, stderr
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=termination_timeout)
        return True, stdout, stderr
