"""화면 표시용 텍스트 만들기.

Streamlit의 체크박스 라벨, st.toast, st.warning 등은 문자열을 마크다운으로 해석한다.
사용자 입력(이름·메모)을 그대로 넣으면 **굵게**, ~~취소선~~, [링크](...), :red[색], $수식$ 등이
적용되므로, 사용자 텍스트는 반드시 escape_markdown을 거친다.
"""

from __future__ import annotations

# CommonMark에서 백슬래시로 이스케이프할 수 있는 ASCII 문장부호 전부
# (+ Streamlit 확장: ':'는 이모지 단축코드·색상 문법, '$'는 수식)
_MD_SPECIAL = frozenset("\\`*_{}[]()#+-.!|~<>$:&=\"'^%@,;?/")


def escape_markdown(text: object) -> str:
    """사용자 텍스트를 마크다운에서 글자 그대로 보이게 만든다. 줄바꿈은 공백으로."""
    s = str(text).replace("\r", " ").replace("\n", " ")
    return "".join("\\" + c if c in _MD_SPECIAL else c for c in s)


def item_text(item: dict) -> str:
    """'이름 ×수량 · 메모' (마크다운 안전)."""
    text = f"{escape_markdown(item['name'])} ×{item['quantity']}"
    if item.get("memo"):
        text += f" · {escape_markdown(item['memo'])}"
    return text


def item_label(item: dict) -> str:
    """체크박스 라벨. 완료 항목은 취소선."""
    text = item_text(item)
    return f"~~{text}~~" if item["checked"] else text
