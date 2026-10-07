"""글자 추출을 별도 파이썬 프로세스(worker)에서 돌린다: 시간 제한·메모리 제한, 한 파일이 이상해도 서비스는 안전.
시간을 넘기면 자식(pdftotext 등)까지 강제 종료하고 `글자 추출 시간 초과(N초)` 로 실패한다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from .errors import ExtractError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 프로젝트 루트(app 의 부모)


def worker_command(path: str) -> list[str]:
    return [sys.executable, "-X", "utf8", "-m", "app.extract.worker", path]


def kill_tree(p: subprocess.Popen) -> None:
    """자식까지 확실히 끝낸다 (Windows 는 kill 이 손자 프로세스를 남길 수 있다)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=15)
        else:
            import signal

            os.killpg(p.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        p.kill()
    except Exception:  # noqa: BLE001
        pass


def run_command(cmd: list[str], timeout_sec: float, env: dict[str, str] | None = None) -> tuple[int, bytes, bytes, bool]:
    """(종료 코드, stdout, stderr, 시간 초과 여부). 셸을 거치지 않는다."""
    kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    p = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env, **kwargs
    )
    try:
        out, err = p.communicate(timeout=timeout_sec)
        return p.returncode, out, err, False
    except subprocess.TimeoutExpired:
        kill_tree(p)
        try:
            out, err = p.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out, err = b"", b""
        return -1, out, err, True


def extract(path: str, timeout_sec: int = 120, memory_mb: int = 1024, cmd: list[str] | None = None) -> dict[str, Any]:
    """@return {format, text, chars[, parts]} — 실패는 ExtractError. cmd 는 시험용(다른 프로세스를 대신 띄움)."""
    env = dict(os.environ)
    env["EXTRACT_MEMORY_MB"] = str(memory_mb)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    code, out, err, timed_out = run_command(cmd or worker_command(path), timeout_sec, env)
    if timed_out:
        raise ExtractError(f"글자 추출 시간 초과({timeout_sec}초)")
    try:
        d = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        d = None
    if not isinstance(d, dict):
        msg = err.decode("utf-8", errors="replace").strip()
        # 메모리 상한에 걸려 죽은 경우 등: 마지막 줄만 보여 준다
        last = msg.splitlines()[-1] if msg else "응답 없음"
        if "MemoryError" in msg:
            raise ExtractError("파일이 너무 큼(추출 중 메모리 상한 초과)")
        raise ExtractError("글자 추출 실패: " + last[:120])
    if "error" in d:
        raise ExtractError(str(d["error"]))
    r: dict[str, Any] = {"format": d["format"], "text": d["text"], "chars": d["chars"]}
    if isinstance(d.get("parts"), list):
        r["parts"] = d["parts"]
    return r
