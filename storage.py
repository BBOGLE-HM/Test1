"""저장소 계층: 공통 인터페이스 + JsonStorage(로컬) + SheetsStorage(배포).

PRD 8장 동시 수정 규칙
1. 모든 쓰기는 id로 해당 항목만 바꾼다. 전체 목록을 통째로 저장하는 함수는 두지 않는다.
2. 쓰기 직전에 저장소를 새로 읽어(캐시를 쓰지 않음) 그 결과에서 id로 위치를 찾는다.
   Sheets의 삭제는 지우기 직전에 id 열을 한 번 더 읽어 행 번호가 그대로인지 확인한다.
3. 체크는 반전이 아니라 명시적인 값으로 저장한다.
4. 내용 수정 시 expected_updated_at이 현재 값과 다르면 ConflictError.
5. 없는 항목: 수정·체크는 ItemNotFound, 삭제는 조용히 무시(False 반환).
6. (Sheets) 조회 결과는 짧은 TTL로 캐시한다. 쓰기에 성공하면 방금 읽은 목록에 같은 변경을
   반영해 캐시를 갱신하고(= 다음 화면에서 시트를 또 읽지 않음), 실패하면 캐시를 비운다.

updated_at은 '내용(이름·수량·카테고리·메모)'이 바뀐 시각이다. 체크는 충돌 감지 대상이
아니므로 updated_at을 바꾸지 않고, update_item은 checked를 건드리지 않는다.
그래서 한 사람이 수정하는 사이 다른 사람이 체크해도 서로 덮어쓰지 않는다.

Streamlit의 모든 세션은 한 프로세스의 스레드이고 저장소 객체는 st.cache_resource로 공유되므로,
저장소 내부 잠금이 가족 구성원 간 쓰기를 직렬화한다. (시트를 사람이 직접 고치는 경우는 예외)

오류: 화면에 보여줄 문장은 StorageError.user_message, 기술적인 원문은 str(e)(서버 로그용).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from models import (
    EDITABLE_FIELDS,
    FIELDS,
    ValidationError,
    desanitize_cell,
    next_timestamp,
    parse_item,
    sanitize_cell,
    validate_item_input,
)

log = logging.getLogger("shopping.storage")

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_FILE = PROJECT_DIR / "data" / "items.json"
DATA_FILE_ENV = "SHOPPING_DATA_FILE"
STORAGE_ENV = "SHOPPING_STORAGE"  # "json"이면 로컬 JSON 저장소를 일부러 쓰는 것 (경고 대신 안내만 표시)
SECRETS_SECTION = "gsheets"

_DEFAULT_USER_MESSAGES = {
    "corrupt": "저장된 목록 파일이 손상되어 읽을 수 없어요. 파일을 확인하거나 백업에서 복구해 주세요.",
    "permission": "저장소에 접근할 권한이 없어요. 파일 또는 시트의 편집 권한을 확인해 주세요.",
    "network": "인터넷 연결을 확인해 주세요.",
    "busy": "요청이 한꺼번에 몰렸어요. 잠시 후 다시 시도해 주세요.",
    "config": "저장소 설정에 문제가 있어요. 설정(secrets)을 확인해 주세요.",
    "other": "저장소에서 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
}


class StorageError(Exception):
    """저장소 읽기/쓰기 실패 (파일 손상, 네트워크, 권한 등).

    str(e)는 기술적인 원문(경로·API 응답 포함 가능) — 화면에는 user_message만 보여준다.
    kind: corrupt | permission | network | busy | config | other
    """

    def __init__(self, message: str, kind: str = "other", user_message: str | None = None):
        super().__init__(message)
        self.kind = kind
        self._user_message = user_message

    @property
    def user_message(self) -> str:
        return self._user_message or _DEFAULT_USER_MESSAGES.get(self.kind, _DEFAULT_USER_MESSAGES["other"])


class ItemNotFound(StorageError):
    """해당 id의 항목이 없음 (다른 사람이 이미 삭제한 경우 등)."""

    def __init__(self, item_id: str):
        super().__init__(f"항목을 찾을 수 없습니다: {item_id}")
        self.item_id = item_id


class ConflictError(StorageError):
    """편집을 시작한 뒤 다른 사람이 먼저 내용을 수정함. current에 최신 항목."""

    def __init__(self, current: dict):
        super().__init__("다른 사용자가 먼저 수정했습니다.")
        self.current = current


@dataclass
class LoadResult:
    items: list[dict] = field(default_factory=list)
    skipped: int = 0  # 형식이 잘못되어 건너뛴 행 수

    def copy(self) -> "LoadResult":
        return LoadResult([dict(it) for it in self.items], self.skipped)


class Storage(ABC):
    """저장소 공통 인터페이스 (PRD 7장)."""

    #: False면 앱이 재시작·재배포될 때 데이터가 사라질 수 있는 저장소 (화면에서 경고)
    persistent: bool = True
    label: str = ""

    @abstractmethod
    def load(self) -> LoadResult:
        """전체 목록과 건너뛴 손상 행 수."""

    def list_items(self) -> list[dict]:
        return self.load().items

    def invalidate_cache(self) -> None:
        """조회 캐시를 비운다 (새로고침 버튼). 캐시가 없는 저장소는 아무것도 하지 않음."""

    @abstractmethod
    def add_item(self, item: dict) -> None: ...

    @abstractmethod
    def update_item(self, item_id: str, fields: dict, expected_updated_at: str | None = None) -> dict:
        """내용 필드(name, quantity, category, memo)만 수정하고 수정된 항목을 반환.

        fields에 없는 필드는 저장소의 현재 값을 그대로 둔다.
        """

    @abstractmethod
    def set_checked(self, item_id: str, checked: bool) -> None: ...

    @abstractmethod
    def delete_item(self, item_id: str) -> bool:
        """삭제했으면 True, 이미 없으면 False."""

    @abstractmethod
    def delete_checked(self, item_ids: Iterable[str] | None = None) -> int:
        """체크된 항목을 삭제하고 삭제 건수를 반환.

        item_ids를 주면 그 id 중 **지금도 체크되어 있는** 항목만 지운다. 확인창을 띄운 뒤
        다른 가족이 새로 체크한 항목이나, 그 사이 체크를 해제한 항목은 건드리지 않는다.
        None이면 체크된 항목 전체.
        """

    @abstractmethod
    def delete_items(self, item_ids: Iterable[str]) -> int:
        """주어진 id의 항목만 삭제하고 삭제 건수를 반환 (전체 초기화용).

        '화면에서 본 항목'만 지우므로, 그 사이 다른 가족이 추가한 항목은 남는다.
        """


def apply_update(current: dict, fields: dict, expected_updated_at: str | None) -> dict:
    """update_item 공통 로직: 충돌 확인 → 검증 → 새 항목 반환. 저장은 호출한 쪽에서."""
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"수정할 수 없는 필드: {sorted(unknown)}")
    if expected_updated_at is not None and current["updated_at"] != expected_updated_at:
        raise ConflictError(current)
    merged = {k: fields.get(k, current[k]) for k in EDITABLE_FIELDS}
    cleaned, errors = validate_item_input(**merged)
    if errors:
        raise ValidationError(errors)
    return {**current, **cleaned, "updated_at": next_timestamp(current["updated_at"])}


def _check_bool(checked: Any) -> None:
    if not isinstance(checked, bool):
        raise TypeError("checked는 bool이어야 합니다.")


def _collect(rows: Iterable[Any]) -> LoadResult:
    """원시 행 → LoadResult. 형식 오류·중복 id는 건너뛰고 개수를 센다."""
    result = LoadResult()
    seen: set[str] = set()
    for raw in rows:
        item = parse_item(raw)
        if item is None or item["id"] in seen:
            result.skipped += 1
            continue
        seen.add(item["id"])
        result.items.append(item)
    return result


# ---------------------------------------------------------------------------
# JsonStorage
# ---------------------------------------------------------------------------

_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """같은 파일을 쓰는 모든 인스턴스(= 같은 프로세스의 모든 Streamlit 세션)가 공유하는 잠금."""
    key = os.path.normcase(str(path.resolve()))
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.Lock())


def default_data_file() -> Path:
    env = os.environ.get(DATA_FILE_ENV, "").strip()
    return Path(env) if env else DEFAULT_DATA_FILE


def _os_error(prefix: str, e: OSError) -> StorageError:
    kind = "permission" if isinstance(e, PermissionError) else "other"
    user = (
        "저장 파일에 쓸 권한이 없어요. 파일이 읽기 전용이거나 다른 프로그램이 사용 중인지 확인해 주세요."
        if kind == "permission"
        else "저장 파일을 읽거나 쓰지 못했어요. 디스크 공간과 파일 경로를 확인해 주세요."
    )
    return StorageError(f"{prefix}: {e}", kind=kind, user_message=user)


class JsonStorage(Storage):
    """로컬 개발·테스트용. 파일 형식: {"version": 1, "items": [ {...}, ... ]}

    매 조작마다 파일을 다시 읽고 해당 id의 항목만 바꾼 뒤 임시 파일 → 교체로 원자적으로 저장한다.
    손상된 행은 화면에서 건너뛰지만 파일에서는 지우지 않고 그대로 보존한다.
    파일 자체가 깨졌으면 읽기·쓰기 모두 StorageError (자동으로 덮어써서 데이터를 잃지 않도록).
    """

    VERSION = 1
    persistent = False
    label = "로컬 파일"

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_data_file()
        self._lock = _lock_for(self.path)

    # --- 파일 입출력 ---
    def _read_raw(self) -> list[Any]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise _os_error("데이터 파일을 읽을 수 없습니다", e) from e
        if not text.strip():
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise StorageError(
                f"데이터 파일 형식이 깨졌습니다 ({self.path.name}, {e.lineno}행).", kind="corrupt"
            ) from e
        if isinstance(data, list):  # 예전/수동 작성 형식 허용
            return data
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        raise StorageError(f"데이터 파일 형식이 올바르지 않습니다 ({self.path.name}).", kind="corrupt")

    def _write_raw(self, rows: list[Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".items-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"version": self.VERSION, "items": rows}, f, ensure_ascii=False, indent=2)
                self._replace(tmp)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as e:
            raise _os_error("데이터를 저장하지 못했습니다", e) from e

    def _replace(self, tmp: str) -> None:
        # Windows에서 다른 프로세스(백신, 편집기)가 잠깐 파일을 잡고 있으면 실패할 수 있어 몇 번 재시도
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    @staticmethod
    def _find(rows: list[Any], item_id: str) -> tuple[int, dict] | None:
        """id가 같고 형식이 올바른 첫 행의 (위치, 파싱된 항목)."""
        for i, raw in enumerate(rows):
            if isinstance(raw, dict) and raw.get("id") == item_id:
                parsed = parse_item(raw)
                if parsed is not None:
                    return i, parsed
        return None

    def _delete_where(self, predicate: Callable[[dict], bool]) -> int:
        """형식이 올바르고 predicate가 참인 항목을 삭제한다. 삭제한 항목 수를 돌려준다.

        - 손상 행(id가 다른 것)은 보존한다.
        - 지우는 항목과 id가 같은 다른 행(중복 id)도 함께 지운다. 안 그러면 가려져 있던 중복 행이
          원본을 지운 뒤에 목록에 나타난다.
        """
        with self._lock:
            rows = self._read_raw()
            removed_ids: set[str] = set()
            for raw in rows:
                parsed = parse_item(raw)
                if parsed is not None and parsed["id"] not in removed_ids and predicate(parsed):
                    removed_ids.add(parsed["id"])
            if not removed_ids:
                return 0
            keep = [raw for raw in rows if not (isinstance(raw, dict) and raw.get("id") in removed_ids)]
            self._write_raw(keep)
            return len(removed_ids)

    # --- 인터페이스 ---
    def load(self) -> LoadResult:
        with self._lock:
            rows = self._read_raw()
        return _collect(rows)

    def add_item(self, item: dict) -> None:
        parsed = parse_item(item)
        if parsed is None:
            raise ValueError("올바르지 않은 항목입니다.")
        with self._lock:
            rows = self._read_raw()
            if self._find(rows, parsed["id"]) is not None:
                raise ValueError(f"이미 있는 id입니다: {parsed['id']}")
            rows.append(parsed)
            self._write_raw(rows)

    def update_item(self, item_id: str, fields: dict, expected_updated_at: str | None = None) -> dict:
        with self._lock:
            rows = self._read_raw()
            found = self._find(rows, item_id)
            if found is None:
                raise ItemNotFound(item_id)
            index, current = found
            updated = apply_update(current, fields, expected_updated_at)
            rows[index] = updated
            self._write_raw(rows)
            return updated

    def set_checked(self, item_id: str, checked: bool) -> None:
        _check_bool(checked)
        with self._lock:
            rows = self._read_raw()
            found = self._find(rows, item_id)
            if found is None:
                raise ItemNotFound(item_id)
            index, current = found
            if current["checked"] == checked:
                return  # 이미 원하는 상태 (멱등)
            rows[index] = {**current, "checked": checked}
            self._write_raw(rows)

    def delete_item(self, item_id: str) -> bool:
        return self._delete_where(lambda it: it["id"] == item_id) > 0

    def delete_checked(self, item_ids: Iterable[str] | None = None) -> int:
        ids = None if item_ids is None else set(item_ids)
        if ids is not None and not ids:
            return 0
        return self._delete_where(lambda it: it["checked"] and (ids is None or it["id"] in ids))

    def delete_items(self, item_ids: Iterable[str]) -> int:
        ids = set(item_ids)
        if not ids:
            return 0
        return self._delete_where(lambda it: it["id"] in ids)


# ---------------------------------------------------------------------------
# SheetsStorage
# ---------------------------------------------------------------------------

_LAST_COL = chr(ord("A") + len(FIELDS) - 1)
_CHECKED_INDEX = FIELDS.index("checked")
_CHECKED_COL = chr(ord("A") + _CHECKED_INDEX)
_TEXT_FIELDS = ("name", "memo")  # 사용자 입력 텍스트 → 수식 주입 방지 대상


def item_to_row(item: dict) -> list[Any]:
    """Item → 시트 한 행 (FIELDS 순서). 사용자 텍스트는 sanitize_cell 적용."""
    return [sanitize_cell(item[f]) if f in _TEXT_FIELDS else item[f] for f in FIELDS]


def row_to_raw(row: list[Any]) -> dict:
    """시트 한 행 → parse_item에 넘길 dict. 빈 칸은 ''로 채운다."""
    cells = list(row) + [""] * (len(FIELDS) - len(row))
    raw = {}
    for f, value in zip(FIELDS, cells):
        if isinstance(value, str):
            value = value.strip() if f not in _TEXT_FIELDS else desanitize_cell(value)
        raw[f] = value
    return raw


def _is_blank(row: list[Any]) -> bool:
    return all(str(c).strip() == "" for c in row)


def _cell_text(value: Any) -> str:
    """시트가 돌려주는 것과 같은 문자열 표현 (방금 쓴 값을 목록에 반영할 때 사용)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return "" if value is None else str(value)


def _status_code(e: BaseException) -> int | None:
    """gspread APIError 등에서 HTTP 상태 코드 (없으면 None)."""
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return code
    status = getattr(getattr(e, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _sheets_error(e: BaseException) -> StorageError:
    """gspread·네트워크 예외 → 종류가 구분된 StorageError."""
    status = _status_code(e)
    name = type(e).__name__
    detail = f"Google Sheets 오류: {name}: {e}"
    if status == 429:
        return StorageError(detail, kind="busy")
    if status in (401, 403):
        return StorageError(
            detail,
            kind="permission",
            user_message="시트에 접근할 권한이 없어요. 서비스 계정에 시트 편집 권한이 있는지 확인해 주세요.",
        )
    if status == 404 or name in ("SpreadsheetNotFound", "NoValidUrlKeyFound"):
        return StorageError(
            detail,
            kind="config",
            user_message="시트를 찾을 수 없어요. spreadsheet_id가 맞는지, 시트를 서비스 계정에 공유했는지 확인해 주세요.",
        )
    if isinstance(e, (ConnectionError, TimeoutError)) or (isinstance(e, OSError) and status is None):
        return StorageError(detail, kind="network")
    return StorageError(detail, kind="other")


class SheetsStorage(Storage):
    """Google Sheets 저장소. 1행 헤더(models.FIELDS 순서), 2행부터 항목 1건 = 1행.

    - 값은 RAW로 쓴다(수식 해석 안 함). 사용자 텍스트는 추가로 sanitize_cell 처리.
    - 동작 1번 = 시트 읽기 1번(전체 읽기) + 쓰기 1번. 읽은 결과에서 id로 행을 찾아 그 행만 쓰고,
      같은 변경을 읽어 둔 목록에도 반영해 캐시로 보관한다. (API 호출량을 줄이기 위함)
      삭제만 지우기 직전에 id 열을 한 번 더 읽어 행 번호가 그대로인지 확인한다.
    - 조회는 cache_ttl초 캐시. 쓰기 실패·invalidate_cache() 후에는 즉시 다시 읽는다.
    - 한도 초과(429)는 잠시 기다렸다 다시 시도하고(읽기는 5xx도), 그래도 안 되면 StorageError.
    - gspread/네트워크 오류는 종류가 구분된 StorageError로 바꿔서 던진다.
    - 테스트에서는 worksheet에 가짜 객체를 넣는다 (get_all_values, row_values, col_values,
      update, append_row, delete_rows 만 사용).
    """

    persistent = True
    label = "Google Sheets"
    FIND_RETRIES = 3
    API_RETRIES = 3
    RETRY_DELAYS = (0.5, 1.5)

    def __init__(
        self,
        config: dict | None = None,
        worksheet: Any = None,
        cache_ttl: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = dict(config or {})
        self._worksheet = worksheet
        ttl = cache_ttl if cache_ttl is not None else self.config.get("cache_ttl", 10)
        self.cache_ttl = float(ttl)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._cache: tuple[float, LoadResult] | None = None
        self._header_ok = False

    # --- API 호출 (재시도) ---
    def _call(self, fn: Callable, *args: Any, retry_statuses: tuple[int, ...], **kwargs: Any) -> Any:
        for attempt in range(self.API_RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_try = attempt == self.API_RETRIES - 1
                if last_try or _status_code(e) not in retry_statuses:
                    raise
                delay = self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)]
                log.warning("Sheets API %s 응답, %.1f초 뒤 재시도", _status_code(e), delay)
                self._sleep(delay)

    def _read(self, fn: Callable, *args: Any) -> Any:
        return self._call(fn, *args, retry_statuses=(429, 500, 502, 503, 504))

    def _write(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        # 쓰기는 '적용되지 않았음이 확실한' 429만 재시도 (5xx 재시도는 중복 행을 만들 수 있음)
        return self._call(fn, *args, retry_statuses=(429,), **kwargs)

    # --- 연결 ---
    def _open_worksheet(self) -> Any:
        import gspread

        account = self.config.get("service_account")
        sheet_id = self.config.get("spreadsheet_id")
        if not account or not sheet_id:
            raise StorageError(
                "Google Sheets 설정(spreadsheet_id, service_account)이 비어 있습니다.",
                kind="config",
                user_message="Google Sheets 설정(spreadsheet_id, service_account)이 비어 있어요. secrets 설정을 확인해 주세요.",
            )
        client = gspread.service_account_from_dict(dict(account))
        spreadsheet = client.open_by_key(sheet_id)
        name = self.config.get("worksheet", "items")
        try:
            return spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            return spreadsheet.add_worksheet(title=name, rows=200, cols=len(FIELDS))

    def _ws(self) -> Any:
        if self._worksheet is None:
            self._worksheet = self._open_worksheet()
        ws = self._worksheet
        if not self._header_ok:
            first = [str(c).strip() for c in self._read(ws.row_values, 1)]
            if not any(first):
                self._write(ws.update, values=[list(FIELDS)], range_name=f"A1:{_LAST_COL}1", value_input_option="RAW")
            elif first[: len(FIELDS)] != list(FIELDS):
                raise StorageError(
                    f"시트 1행 머리글이 다릅니다: {first}",
                    kind="config",
                    user_message=f"시트 1행의 머리글이 형식과 달라요. A1부터 {', '.join(FIELDS)} 순서여야 해요.",
                )
            self._header_ok = True
        return ws

    @contextmanager
    def _guard(self):
        """잠금 + 오류 변환. 실패하면 캐시를 비운다 (시트의 실제 상태를 모르므로)."""
        with self._lock:
            try:
                yield
            except (StorageError, ValueError, TypeError):
                self._cache = None
                raise
            except Exception as e:  # gspread APIError, 네트워크 오류 등
                self._cache = None
                raise _sheets_error(e) from e

    def _snapshot(self) -> list[list[str]]:
        """시트 전체를 새로 읽는다 (캐시 미사용). 첫 행은 헤더."""
        ws = self._ws()
        return self._read(ws.get_all_values)

    @staticmethod
    def _parse(values: list[list[str]]) -> LoadResult:
        return _collect(row_to_raw(r) for r in values[1:] if not _is_blank(r))

    def _remember(self, values: list[list[str]]) -> None:
        """방금 읽고(+우리가 바꾼) 목록을 캐시로 보관."""
        self._cache = (self._clock(), self._parse(values))

    @staticmethod
    def _locate(values: list[list[str]], item_id: str) -> tuple[int, dict] | None:
        """읽은 목록에서 id의 (시트 행 번호, 항목). 없거나 손상 행이면 None."""
        for row_no, row in enumerate(values[1:], start=2):
            if not row or str(row[0]).strip() != item_id:
                continue
            parsed = parse_item(row_to_raw(row))
            if parsed is not None:
                return row_no, parsed
        return None

    def _delete_rows(self, row_numbers: Iterable[int]) -> None:
        """행 번호들을 아래에서부터, 연속 구간은 한 번에 삭제 (위쪽 행 번호가 밀리지 않게)."""
        ws = self._worksheet
        nums = sorted(set(row_numbers), reverse=True)
        i = 0
        while i < len(nums):
            end = start = nums[i]
            while i + 1 < len(nums) and nums[i + 1] == start - 1:
                i += 1
                start = nums[i]
            self._write(ws.delete_rows, start, end)
            i += 1

    # --- 인터페이스 ---
    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache = None

    def load(self) -> LoadResult:
        with self._guard():
            now = self._clock()
            if self._cache is not None and now - self._cache[0] < self.cache_ttl:
                return self._cache[1].copy()
            values = self._snapshot()
            result = self._parse(values)
            self._cache = (now, result)
            return result.copy()

    def add_item(self, item: dict) -> None:
        parsed = parse_item(item)
        if parsed is None:
            raise ValueError("올바르지 않은 항목입니다.")
        with self._guard():
            values = self._snapshot()
            if parsed["id"] in {str(r[0]).strip() for r in values[1:] if r}:
                raise ValueError(f"이미 있는 id입니다: {parsed['id']}")
            row = item_to_row(parsed)
            self._write(self._worksheet.append_row, row, value_input_option="RAW", table_range="A1")
            values.append([_cell_text(v) for v in row])
            self._remember(values)

    def update_item(self, item_id: str, fields: dict, expected_updated_at: str | None = None) -> dict:
        with self._guard():
            values = self._snapshot()
            found = self._locate(values, item_id)
            if found is None:
                raise ItemNotFound(item_id)
            row_no, current = found
            updated = apply_update(current, fields, expected_updated_at)
            row = item_to_row(updated)
            self._write(
                self._worksheet.update,
                values=[row],
                range_name=f"A{row_no}:{_LAST_COL}{row_no}",
                value_input_option="RAW",
            )
            values[row_no - 1] = [_cell_text(v) for v in row]
            self._remember(values)
            return updated

    def set_checked(self, item_id: str, checked: bool) -> None:
        _check_bool(checked)
        with self._guard():
            values = self._snapshot()
            found = self._locate(values, item_id)
            if found is None:
                raise ItemNotFound(item_id)
            row_no, current = found
            if current["checked"] == checked:
                self._remember(values)
                return
            self._write(
                self._worksheet.update,
                values=[[checked]],
                range_name=f"{_CHECKED_COL}{row_no}",
                value_input_option="RAW",
            )
            row = list(values[row_no - 1])
            row += [""] * (len(FIELDS) - len(row))
            row[_CHECKED_INDEX] = _cell_text(checked)
            values[row_no - 1] = row
            self._remember(values)

    def _delete_where(self, predicate: Callable[[dict], bool]) -> int:
        """predicate가 참인 항목(과 같은 id의 중복 행)을 삭제하고 항목 수를 돌려준다.

        지우기 직전에 id 열을 다시 읽어, 방금 찾은 행 번호의 id가 그대로인지 확인한다.
        (앱 밖에서 시트의 행을 지우거나 끼워 넣어 번호가 밀렸다면 다른 행을 지우지 않고 처음부터 다시 찾는다)
        """
        with self._guard():
            for _ in range(self.FIND_RETRIES):
                values = self._snapshot()
                removed_ids: set[str] = set()
                for row in values[1:]:
                    parsed = None if _is_blank(row) else parse_item(row_to_raw(row))
                    if parsed is not None and parsed["id"] not in removed_ids and predicate(parsed):
                        removed_ids.add(parsed["id"])
                targets = [
                    (row_no, str(row[0]).strip())
                    for row_no, row in enumerate(values[1:], start=2)
                    if row and str(row[0]).strip() in removed_ids
                ]
                if not targets:
                    self._remember(values)
                    return 0
                ids_now = [str(v).strip() for v in self._read(self._worksheet.col_values, 1)]
                if all(row_no - 1 < len(ids_now) and ids_now[row_no - 1] == rid for row_no, rid in targets):
                    self._delete_rows(row_no for row_no, _ in targets)
                    drop = {row_no for row_no, _ in targets}
                    self._remember([row for n, row in enumerate(values, start=1) if n not in drop])
                    return len(removed_ids)
                # 두 번 읽는 사이 시트가 바뀜 → 다시 찾기
            raise StorageError(
                "시트가 동시에 바뀌어 삭제 대상 행을 확정하지 못했습니다.",
                kind="busy",
                user_message="목록이 동시에 바뀌고 있어요. 잠시 후 다시 시도해 주세요.",
            )

    def delete_item(self, item_id: str) -> bool:
        return self._delete_where(lambda it: it["id"] == item_id) > 0

    def delete_checked(self, item_ids: Iterable[str] | None = None) -> int:
        ids = None if item_ids is None else set(item_ids)
        if ids is not None and not ids:
            return 0
        return self._delete_where(lambda it: it["checked"] and (ids is None or it["id"] in ids))

    def delete_items(self, item_ids: Iterable[str]) -> int:
        ids = set(item_ids)
        if not ids:
            return 0
        return self._delete_where(lambda it: it["id"] in ids)


# ---------------------------------------------------------------------------


def _secrets_file_exists() -> bool:
    """Streamlit이 secrets를 찾는 두 위치(작업 폴더, 홈 폴더)에 파일이 있는지."""
    candidates = (Path.cwd() / ".streamlit" / "secrets.toml", Path.home() / ".streamlit" / "secrets.toml")
    return any(p.is_file() for p in candidates)


def _sheets_config() -> dict | None:
    """st.secrets의 [gsheets] 섹션.

    - secrets가 아예 없거나 [gsheets] 섹션이 없으면 None (= 로컬 JSON을 쓰겠다는 뜻)
    - secrets가 있는데 읽을 수 없으면(문법 오류, BOM 등) JSON으로 조용히 넘어가지 않고 StorageError.
      주의: Streamlit은 secrets 파일의 **문법 오류도 StreamlitSecretNotFoundError**(FileNotFoundError의
      하위 클래스)로 던진다. 그래서 '파일이 실제로 있는데 못 읽었는지'를 따로 확인한다.
    """
    import streamlit as st

    try:
        if SECRETS_SECTION not in st.secrets:
            return None
        return dict(st.secrets[SECRETS_SECTION])
    except FileNotFoundError as e:
        if "pars" in str(e).lower() or _secrets_file_exists():
            raise StorageError(
                f"secrets를 해석할 수 없습니다: {e}",
                kind="config",
                user_message=(
                    "secrets 설정의 문법이 잘못됐어요. .streamlit/secrets.toml(또는 Cloud의 Secrets)을 "
                    "TOML 형식, BOM 없는 UTF-8로 저장했는지 확인해 주세요."
                ),
            ) from e
        return None
    except Exception as e:
        raise StorageError(
            f"secrets를 읽을 수 없습니다: {type(e).__name__}: {e}",
            kind="config",
            user_message="secrets 설정을 읽을 수 없어요. .streamlit/secrets.toml(또는 Cloud의 Secrets) 형식을 확인해 주세요.",
        ) from e


def get_storage() -> Storage:
    """[gsheets] 설정이 있으면 SheetsStorage, 없으면 JsonStorage.

    설정이 있는데 잘못된 경우에는 JSON으로 대체하지 않는다 (연결 오류는 화면에 표시됨).
    JSON으로 일부러 쓰려면 환경변수 SHOPPING_STORAGE=json (경고 대신 안내만 표시).
    """
    config = _sheets_config()
    if config:
        return SheetsStorage(config=config)
    return JsonStorage()


def json_is_intentional() -> bool:
    return os.environ.get(STORAGE_ENV, "").strip().lower() == "json"
