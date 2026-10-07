"""실제 첨부 대조: PHP 가 뽑아 DB(grant_attachments)에 둔 글자와 이 서비스(Python)의 추출 결과를 견준다.

사용: .venv\\Scripts\\python.exe tools\\compare_with_db.py [--limit N] [--workers 4] [--only hwp,pdf] [--out 파일.json]

- DB 는 읽기 전용(SELECT). 접속 정보는 환경 변수 GM_DST_URL·GM_DST_USER·GM_DST_PASSWORD, 없으면 GM_ENV_FILE
  (기본 D:\\claude\\grantmatch\\grantmatch\\.env.local) 에서 읽는다. 값은 출력·저장하지 않는다.
- 첨부 파일 폴더: 환경 변수 GM_ATTACH_DIR (기본 D:\\claude\\jiwonnavi\\app\\backend-php\\storage\\app\\grants\\attachments)
- 판정: 같음(글자 그대로) / 공백만 다름(공백·줄바꿈을 모두 지우면 같음) / 차이 있음 / Python 쪽만 실패 / 파일 없음
- 같은 sha256 은 한 번만 견준다(DISTINCT).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_ENV = r"D:\claude\grantmatch\grantmatch\.env.local"
DEFAULT_ATT = r"D:\claude\jiwonnavi\app\backend-php\storage\app\grants\attachments"


def _load_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _conn_args() -> dict:
    env = _load_env_file(os.environ.get("GM_ENV_FILE", DEFAULT_ENV))
    get = lambda k: os.environ.get(k) or env.get(k, "")  # noqa: E731
    url = get("GM_DST_URL").replace("jdbc:", "", 1)
    u = urlparse(url)
    return dict(host=u.hostname or "127.0.0.1", port=u.port or 5432, dbname=(u.path or "/").lstrip("/"),
                user=get("GM_DST_USER"), password=get("GM_DST_PASSWORD"))


_conn = None


def _init_worker() -> None:
    global _conn
    import psycopg

    _conn = psycopg.connect(**_conn_args())
    _conn.read_only = True


PHP_BIN = os.environ.get("PHP_BIN") or os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\PHP.PHP.8.4_Microsoft.Winget.Source_8wekyb3d8bbwe\php.exe")
PHP_EXTRACT = os.environ.get("PHP_EXTRACT_DIR", r"D:\claude\jiwonnavi\packages\extract")


def _php_live(path: str) -> str | None:
    """지금 PHP 코드(bin/extract-text)로 다시 뽑은 글자. PHP 를 못 돌리면 None."""
    import subprocess

    if not os.path.isfile(PHP_BIN):
        return None
    try:
        p = subprocess.run([PHP_BIN, "-d", "memory_limit=512M", os.path.join(PHP_EXTRACT, "bin", "extract-text"), "--json", path],
                           capture_output=True, timeout=180)
        return json.loads(p.stdout.decode("utf-8")).get("text")
    except Exception:  # noqa: BLE001
        return None


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _first_diff(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _compare_one(sha: str, att_dir: str) -> dict:
    from app.extract import ExtractError, extract

    row = _conn.execute(
        "select format, chars, text, parts from grant_attachments where sha256=%s and status='EXTRACTED' limit 1", (sha,)
    ).fetchone()
    fmt, chars, php_text, php_parts = row
    php_text = php_text or ""
    path = os.path.join(att_dir, sha[:2], sha)
    res = {"sha": sha, "php_format": fmt, "php_chars": len(php_text)}
    if not os.path.isfile(path):
        res["verdict"] = "missing"
        return res
    t = time.time()
    try:
        r = extract(path)
    except ExtractError as e:
        res.update(verdict="py_fail", error=str(e), secs=round(time.time() - t, 2))
        return res
    except Exception as e:  # noqa: BLE001
        res.update(verdict="py_fail", error="EXC " + type(e).__name__ + ": " + str(e)[:100], secs=round(time.time() - t, 2))
        return res
    secs = round(time.time() - t, 2)
    py = r["text"]
    res.update(py_format=r["format"], py_chars=len(py), secs=secs)
    if r["format"] != fmt:
        res["format_mismatch"] = True
    if py == php_text:
        res["verdict"] = "same"
    elif _squash(py) == _squash(php_text):
        res["verdict"] = "ws_only"
    else:
        res["verdict"] = "diff"
        i = _first_diff(py, php_text)
        pl, ql = py.split("\n"), php_text.split("\n")
        sp, sq = Counter(pl), Counter(ql)
        only_py = list((sp - sq).elements())
        only_php = list((sq - sp).elements())
        res.update(
            first_diff=i,
            ctx_py=py[max(0, i - 30) : i + 60],
            ctx_php=php_text[max(0, i - 30) : i + 60],
            lines_only_py=len(only_py),
            lines_only_php=len(only_php),
            sample_only_py=[x[:80] for x in only_py[:4]],
            sample_only_php=[x[:80] for x in only_php[:4]],
            sq_ratio=round(len(_squash(py)) / max(1, len(_squash(php_text))), 4),
        )
        live = _php_live(path)
        if live is not None:  # DB 의 글자가 옛 코드로 뽑은 것일 수 있어, 지금 PHP 코드로 다시 뽑은 것과도 견준다
            res["live_verdict"] = "same" if live == py else ("ws_only" if _squash(live) == _squash(py) else "diff")
            res["live_chars"] = len(live)
    if r.get("parts") is not None:
        pp = php_parts if isinstance(php_parts, list) else (json.loads(php_parts) if php_parts else None)
        if pp != r["parts"]:
            res["parts_differ"] = True
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=os.path.join(ROOT, "tools", "_out", "compare_result.json"))
    ap.add_argument("--shas", default="", help="쉼표로 나눈 sha256 만 견줌")
    a = ap.parse_args()
    att_dir = os.environ.get("GM_ATTACH_DIR", DEFAULT_ATT)

    import psycopg

    with psycopg.connect(**_conn_args()) as c:
        c.read_only = True
        sql = "select distinct sha256, format from grant_attachments where status='EXTRACTED' and sha256 is not null order by sha256"
        rows = c.execute(sql).fetchall()
    if a.only:
        want = set(a.only.split(","))
        rows = [r for r in rows if r[1] in want]
    if a.shas:
        want = set(a.shas.split(","))
        rows = [r for r in rows if r[0] in want]
    if a.limit:
        rows = rows[: a.limit]
    print(f"견줄 첨부(sha256 기준): {len(rows)}개, 작업자 {a.workers}", flush=True)

    results: list[dict] = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker) as ex:
        futs = {ex.submit(_compare_one, sha, att_dir): sha for sha, _ in rows}
        for n, f in enumerate(as_completed(futs), 1):
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                results.append({"sha": futs[f], "verdict": "py_fail", "error": "WORKER " + type(e).__name__, "php_format": "?"})
            if n % 200 == 0:
                print(f"  {n}/{len(rows)}  {time.time() - t0:.0f}초", flush=True)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    # 요약
    by: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by[r.get("php_format", "?")][r["verdict"]] += 1
    verdicts = ["same", "ws_only", "diff", "py_fail", "missing"]
    print("\n형식별 (같음 / 공백만 다름 / 차이 있음 / Python 만 실패 / 파일 없음)")
    tot = Counter()
    for fmt in sorted(by):
        c = by[fmt]
        tot.update(c)
        print(f"  {fmt:6s} 계 {sum(c.values()):5d} | " + " / ".join(str(c[v]) for v in verdicts))
    n = sum(tot.values())
    ok = tot["same"] + tot["ws_only"]
    base = n - tot["missing"]
    print(f"  전체   계 {n:5d} | " + " / ".join(str(tot[v]) for v in verdicts))
    print(f"공백 정규화 후 같음: {ok}/{base} = {100 * ok / max(1, base):.2f}%  (글자 그대로 같음 {tot['same']})")
    fm = [r for r in results if r.get("format_mismatch")]
    print(f"형식 판별이 PHP 와 다름: {len(fm)}개; 묶음 parts 가 다름: {sum(1 for r in results if r.get('parts_differ'))}개")

    live = Counter(r.get("live_verdict", "-") for r in results if r["verdict"] == "diff")
    if live:
        print(f"\n(차이 있음 {tot['diff']}개를 지금 PHP 코드로 다시 뽑은 것과 견주면: {dict(live)})")
    diffs = sorted((r for r in results if r["verdict"] == "diff"), key=lambda r: -abs(r["py_chars"] - r["php_chars"]))
    print("\n차이 큰 예 (글자 수 차이 순)")
    for r in diffs[:5]:
        print(f"  {r['sha'][:16]}… {r['php_format']} PHP {r['php_chars']}자 / Py {r['py_chars']}자, 처음 다른 위치 {r['first_diff']}, PHP 에만 줄 {r['lines_only_php']}·Py 에만 줄 {r['lines_only_py']}")
        print(f"     PHP: {r['ctx_php'][:90]!r}")
        print(f"     Py : {r['ctx_py'][:90]!r}")
    fails = [r for r in results if r["verdict"] == "py_fail"]
    if fails:
        print("\nPython 쪽만 실패한 첨부:")
        for r in fails[:15]:
            print(f"  {r['sha'][:16]}… {r['php_format']}: {r.get('error')}")
        print("  이유별:", Counter(re.sub(r"[:(].*", "", r.get("error", "")) for r in fails).most_common(8))
    slow = sorted((r for r in results if "secs" in r), key=lambda r: -r["secs"])[:5]
    print("\n가장 오래 걸린 파일:", ", ".join(f"{r['sha'][:12]}… {r['php_format']} {r['secs']}초" for r in slow))
    print(f"전체 걸린 시간 {time.time() - t0:.0f}초 · 결과 파일 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
