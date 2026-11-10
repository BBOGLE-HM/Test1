"""쇼핑리스트 아이템 모델: 생성, 입력 검증, 저장소 값 해석.

화면(Streamlit)과 저장소에 의존하지 않는 순수 함수만 둔다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

# PRD 4장: 고정 카테고리, 표시 순서 = 마트 동선
CATEGORIES: tuple[str, ...] = (
    "채소·과일",
    "정육·수산",
    "유제품·계란",
    "가공식품",
    "음료·간식",
    "생활용품",
    "기타",
)
DEFAULT_CATEGORY = "기타"

NAME_MAX = 50
MEMO_MAX = 100
QTY_MIN = 1
QTY_MAX = 999

# 저장소에 쓰는 필드 순서 (Google Sheets 헤더 순서와 동일, PRD 6장)
FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "quantity",
    "category",
    "memo",
    "checked",
    "created_at",
    "updated_at",
)
# 사용자가 수정할 수 있는 내용 필드. checked는 set_checked로만 바꾼다.
EDITABLE_FIELDS: tuple[str, ...] = ("name", "quantity", "category", "memo")


class ValidationError(ValueError):
    """입력 검증 실패. errors에 사용자에게 보여줄 메시지 목록."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def next_timestamp(previous: str) -> str:
    """previous보다 반드시 뒤인 시각. updated_at은 충돌 감지 토큰이므로 값이 바뀌어야 한다.

    Windows는 시계 해상도가 거칠어 연속 호출 시 같은 값이 나올 수 있어 1µs씩 밀어 준다.
    """
    ts = now_iso()
    try:
        prev = datetime.fromisoformat(previous)
    except (TypeError, ValueError):
        return ts
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    if datetime.fromisoformat(ts) <= prev:
        ts = (prev + timedelta(microseconds=1)).astimezone(timezone.utc).isoformat(timespec="microseconds")
    return ts


def _to_int(value: Any) -> int | None:
    """정수로 해석되는 값만 int로. bool, 소수, 빈 값은 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def normalize_category(value: Any) -> str:
    """고정 목록에 없는 카테고리는 '기타'로 (PRD 4장)."""
    if isinstance(value, str) and value.strip() in CATEGORIES:
        return value.strip()
    return DEFAULT_CATEGORY


def validate_item_input(
    name: Any,
    quantity: Any = 1,
    category: Any = DEFAULT_CATEGORY,
    memo: Any = "",
) -> tuple[dict, list[str]]:
    """사용자 입력을 검증하고 정리한다.

    반환: (정리된 값 dict, 에러 메시지 목록). 에러가 없을 때만 정리된 값을 쓴다.
    """
    errors: list[str] = []

    clean_name = name.strip() if isinstance(name, str) else ""
    if not clean_name:
        errors.append("아이템 이름을 입력해 주세요.")
    elif len(clean_name) > NAME_MAX:
        errors.append(f"이름은 {NAME_MAX}자 이하로 입력해 주세요.")

    if quantity is None or (isinstance(quantity, str) and not quantity.strip()):
        clean_qty: int | None = 1
    else:
        clean_qty = _to_int(quantity)
    if clean_qty is None or not (QTY_MIN <= clean_qty <= QTY_MAX):
        errors.append(f"수량은 {QTY_MIN}~{QTY_MAX} 사이의 정수로 입력해 주세요.")

    clean_memo = memo.strip() if isinstance(memo, str) else ""
    if len(clean_memo) > MEMO_MAX:
        errors.append(f"메모는 {MEMO_MAX}자 이하로 입력해 주세요.")

    cleaned = {
        "name": clean_name,
        "quantity": clean_qty if clean_qty is not None else 1,
        "category": normalize_category(category),
        "memo": clean_memo,
    }
    return cleaned, errors


def new_item(
    name: Any,
    quantity: Any = 1,
    category: Any = DEFAULT_CATEGORY,
    memo: Any = "",
) -> dict:
    """새 아이템을 만든다. 입력이 잘못되면 ValidationError."""
    cleaned, errors = validate_item_input(name, quantity, category, memo)
    if errors:
        raise ValidationError(errors)
    ts = now_iso()
    return {
        "id": str(uuid.uuid4()),
        **cleaned,
        "checked": False,
        "created_at": ts,
        "updated_at": ts,
    }


_TRUE_STRINGS = {"true", "1", "yes", "y"}
_FALSE_STRINGS = {"false", "0", "no", "n", ""}


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return None


def _to_text(value: Any) -> str | None:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def parse_item(raw: Any) -> dict | None:
    """저장소에서 읽은 값을 Item으로 변환한다. 형식이 틀리면 None (예외를 던지지 않음).

    JSON 값뿐 아니라 Google Sheets에서 오는 문자열("2", "TRUE")도 해석한다.
    카테고리는 모르는 값이면 '기타'로 보정하고, 시각 값이 없으면 빈 문자열로 둔다.
    """
    if not isinstance(raw, dict):
        return None

    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        return None

    name = _to_text(raw.get("name"))
    if name is None or not name.strip():
        return None

    quantity = _to_int(raw.get("quantity", 1))
    if quantity is None or not (QTY_MIN <= quantity <= QTY_MAX):
        return None

    memo = _to_text(raw.get("memo"))
    if memo is None:
        return None

    checked = _to_bool(raw.get("checked", False))
    if checked is None:
        return None

    created_at = _to_text(raw.get("created_at"))
    updated_at = _to_text(raw.get("updated_at"))
    if created_at is None or updated_at is None:
        return None

    return {
        "id": item_id.strip(),
        "name": name.strip(),
        "quantity": quantity,
        "category": normalize_category(raw.get("category")),
        "memo": memo.strip(),
        "checked": checked,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def find_open_duplicate(name: str, items: list[dict], exclude_id: str | None = None) -> dict | None:
    """이름이 같은(대소문자·앞뒤 공백 무시) 미완료 항목을 찾는다. 중복 경고용 (PRD 4장)."""
    key = name.strip().casefold()
    for item in items:
        if item["id"] == exclude_id or item["checked"]:
            continue
        if item["name"].strip().casefold() == key:
            return item
    return None


# --- 중복 제출 방지 (더블 탭, Enter 연타) ---
REPEAT_WINDOW_SECONDS = 2.0


def is_repeat_submission(
    last: tuple[Any, float] | None,
    signature: Any,
    now: float,
    window: float = REPEAT_WINDOW_SECONDS,
) -> bool:
    """직전 제출과 같은 내용이 window초 안에 다시 들어왔는지.

    Streamlit은 첫 제출의 입력 초기화가 화면에 닿기 전에 두 번째 제출을 서버로 보낼 수 있다.
    """
    if last is None:
        return False
    last_signature, last_time = last
    return last_signature == signature and 0 <= now - last_time < window


# --- 수정 폼: 실제로 바꾼 필드만 ---
def changed_fields(original: dict, current: dict) -> dict:
    """수정 폼을 열었을 때 값(original)과 지금 입력(current)을 비교해 바뀐 내용 필드만 돌려준다.

    충돌 뒤 다시 저장할 때 내가 건드리지 않은 필드가 오래된 값으로 남의 수정을 덮어쓰지 않게 한다.
    """
    changed = {}
    for key in EDITABLE_FIELDS:
        if key not in current:
            continue
        before, after = original.get(key), current[key]
        if isinstance(before, str) and isinstance(after, str):
            before, after = before.strip(), after.strip()
        if before != after:
            changed[key] = current[key]
    return changed


# --- 스프레드시트 수식 주입 방지 (PRD 9장) ---
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _needs_escape(text: str) -> bool:
    """(앞에 작은따옴표가 몇 개 있든) 수식 기호로 시작하는지."""
    return text.lstrip("'").startswith(_FORMULA_PREFIXES)


def sanitize_cell(text: str) -> str:
    """=, +, -, @ 등으로 시작하는 값 앞에 '를 붙여 수식으로 해석되지 않게 한다.

    사용자가 직접 입력한 작은따옴표 뒤에 수식 기호가 오는 값("'=1")도 '를 한 번 더 붙인다.
    그래야 읽을 때 '를 하나만 떼어도 원래 값으로 돌아온다.
    Google Sheets에 쓸 때만 적용하고, 읽을 때 desanitize_cell로 되돌린다.
    """
    if isinstance(text, str) and _needs_escape(text):
        return "'" + text
    return text


def desanitize_cell(text: str) -> str:
    """sanitize_cell이 붙인 '를 하나 제거한다."""
    if isinstance(text, str) and text.startswith("'") and _needs_escape(text[1:]):
        return text[1:]
    return text
