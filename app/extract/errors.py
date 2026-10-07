"""추출 실패. 메시지는 관리자 화면에 그대로 보여도 되는 한국어 (PHP ExtractException 과 같은 문구)."""


class ExtractError(Exception):
    """파일을 읽을 수 없음(형식 아님·손상·암호·크기 초과)."""
