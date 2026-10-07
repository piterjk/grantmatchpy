"""PHP strnatcmp 와 같은 "자연 순서" 비교 (ZipBundle 의 파일 정렬용). 대소문자 구분, 바이트(코드) 순."""

from __future__ import annotations

import functools


def _isdigit(c: str) -> bool:
    return "0" <= c <= "9"


def _compare_right(a: str, ai: int, b: str, bi: int) -> tuple[int, int, int]:
    bias = 0
    la, lb = len(a), len(b)
    while True:
        da = ai < la and _isdigit(a[ai])
        db = bi < lb and _isdigit(b[bi])
        if not da and not db:
            return bias, ai, bi
        if not da:
            return -1, ai, bi
        if not db:
            return 1, ai, bi
        if bias == 0:
            if a[ai] < b[bi]:
                bias = -1
            elif a[ai] > b[bi]:
                bias = 1
        ai += 1
        bi += 1


def _compare_left(a: str, ai: int, b: str, bi: int) -> tuple[int, int, int]:
    la, lb = len(a), len(b)
    while True:
        da = ai < la and _isdigit(a[ai])
        db = bi < lb and _isdigit(b[bi])
        if not da and not db:
            return 0, ai, bi
        if not da:
            return -1, ai, bi
        if not db:
            return 1, ai, bi
        if a[ai] < b[bi]:
            return -1, ai, bi
        if a[ai] > b[bi]:
            return 1, ai, bi
        ai += 1
        bi += 1


def strnatcmp(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return (la > lb) - (la < lb)
    ai = bi = 0
    # 맨 앞의 0 건너뛰기
    while ai < la - 1 and a[ai] == "0" and _isdigit(a[ai + 1]):
        ai += 1
    while bi < lb - 1 and b[bi] == "0" and _isdigit(b[bi + 1]):
        bi += 1
    while True:
        ca = a[ai] if ai < la else ""
        cb = b[bi] if bi < lb else ""
        while ca != "" and ca.isspace() and ca.isascii():
            ai += 1
            ca = a[ai] if ai < la else ""
        while cb != "" and cb.isspace() and cb.isascii():
            bi += 1
            cb = b[bi] if bi < lb else ""
        if ca != "" and cb != "" and _isdigit(ca) and _isdigit(cb):
            fractional = ca == "0" or cb == "0"
            if fractional:
                r, ai, bi = _compare_left(a, ai, b, bi)
            else:
                r, ai, bi = _compare_right(a, ai, b, bi)
            if r != 0:
                return r
            if ai >= la and bi >= lb:
                return 0
            if ai >= la:
                return -1
            if bi >= lb:
                return 1
            ca = a[ai]
            cb = b[bi]
        if ca < cb:
            return -1
        if ca > cb:
            return 1
        ai += 1
        bi += 1
        if ai >= la and bi >= lb:
            return 0
        if ai >= la:
            return -1
        if bi >= lb:
            return 1


natkey = functools.cmp_to_key(strnatcmp)
