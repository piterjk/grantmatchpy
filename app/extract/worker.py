"""추출 작업 프로세스: `python -m app.extract.worker <파일>` — 결과를 JSON 한 줄로 표준 출력에 낸다.
{"format","text","chars"[,"parts"]} 또는 {"error": "한국어 사유"}. sandbox.py 가 시간 제한을 걸고 이 프로세스를 띄운다."""

from __future__ import annotations

import json
import os
import sys

from .errors import ExtractError

MAX_JSON = 32 * 1024 * 1024


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("사용: python -m app.extract.worker <파일>\n")
        return 2
    from .limits import apply_memory_limit

    apply_memory_limit(int(os.environ.get("EXTRACT_MEMORY_MB", "1024") or 0))
    from . import text_extractor

    try:
        r = text_extractor.extract(argv[1])
        out = json.dumps(r, ensure_ascii=False)
        if len(out) > MAX_JSON:
            raise ExtractError("추출한 글자가 너무 많음")
    except ExtractError as e:
        out = json.dumps({"error": str(e)}, ensure_ascii=False)
    except MemoryError:
        out = json.dumps({"error": "파일이 너무 큼(추출 중 메모리 상한 초과)"}, ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
