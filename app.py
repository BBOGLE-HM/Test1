"""우리집 쇼핑리스트 (3단계: UI 디자인 적용).

구조
- 모든 쓰기는 버튼/체크박스의 콜백(on_click, on_change)에서 storage 인터페이스로만 한다.
  콜백은 화면을 그리기 전에 실행되므로, 결과 메시지는 session_state에 쌓았다가 화면 위쪽에 표시한다.
- 목록의 원본은 항상 storage. session_state에는 화면 상태(수정 중인 id, 필터, 확인 단계)만 둔다.
- 위젯 key는 item id 기반. 체크박스 값은 매 실행마다 저장소 값으로 다시 맞춘다
  (다른 가족이 바꾼 체크 상태가 내 화면의 이전 위젯 상태에 가려지지 않도록).
- 사용자 텍스트는 마크다운으로 해석되는 곳(체크박스 라벨, 알림)에 넣을 때 escape_markdown.
- 꾸미기(CSS, 카테고리 아이콘)는 ui_style.py. 항목 행은 가로 컨테이너라 모바일에서도 세로로 쌓이지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import wraps

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from formatting import escape_markdown as md
from formatting import item_label, item_text
from models import (
    CATEGORIES,
    DEFAULT_CATEGORY,
    MEMO_MAX,
    NAME_MAX,
    REPEAT_WINDOW_SECONDS,
    ValidationError,
    changed_fields,
    find_open_duplicate,
    is_repeat_submission,
    new_item,
    validate_item_input,
)
from storage import (
    ConflictError,
    ItemNotFound,
    Storage,
    StorageError,
    get_storage,
    json_is_intentional,
)
from ui_style import ALL_ICON, CSS, category_title

log = logging.getLogger("shopping")

st.set_page_config(page_title="우리집 쇼핑리스트", page_icon="🛒")

ALL = "전체"


@st.cache_resource
def _storage() -> Storage:
    return get_storage()


@st.cache_resource
def _add_lock() -> threading.Lock:
    """추가 중복 제출 검사용 잠금. 이 파일은 실행(rerun)마다 다시 실행되므로 모듈 변수가 아니라 캐시로 공유한다."""
    return threading.Lock()


@st.cache_resource
def _recent_adds() -> dict:
    """세션 ID → (직전에 추가한 내용, 시각).

    session_state에 두면 안 된다: 더블 탭으로 겹쳐 실행되는 두 스크립트 실행은 서로의 session_state 쓰기를
    보지 못한다(실측). 프로세스 전역이면서 세션별로 구분해, 다른 가족이 같은 이름을 추가한 것은 막지 않는다.
    """
    return {}


def _session_key() -> str:
    ctx = get_script_run_ctx()
    return ctx.session_id if ctx else "-"


try:
    storage = _storage()
except StorageError as _e:  # secrets 설정이 있는데 읽을 수 없는 경우: 조용히 JSON으로 넘어가지 않음
    log.warning("저장소를 준비하지 못했습니다: %s", _e)
    st.error(f"저장소를 준비하지 못했어요. {_e.user_message}")
    st.stop()
ss = st.session_state

# 수정 폼의 위젯 key 접두사 (내용 필드 이름 → key)
_EDIT_KEYS = {"name": "e_name", "quantity": "e_qty", "category": "e_cat", "memo": "e_memo"}

_DEFAULTS = {
    "editing_id": None,  # 수정 중인 항목 id
    "edit_base": None,  # 수정을 시작할 때의 updated_at (충돌 감지용)
    "edit_orig": None,  # 수정 폼을 열었을 때의 내용 필드 값 (바꾼 필드만 저장하기 위함)
    "confirm": None,  # "checked" | "reset" | None
    "confirm_ids": [],  # 일괄 삭제 확인창을 띄울 때 화면에 보이던 완료 항목 id
    "confirm_scope": "",  # 확인창 문구에 쓸 범위 (예: "유제품·계란")
    "notices": [],  # [(level, markdown)] — 다음 화면 위쪽에 1회 표시
    "toasts": [],
    # 카테고리 필터(filter)와 추가 폼 입력칸(add_*)의 기본값은 여기가 아니라 위젯 인자로 지정한다.
    # 오류 화면처럼 폼이 그려지지 않은 실행을 거치면 위젯 상태가 사라지는데, 그때 session_state에
    # 미리 넣어 둔 값에 기대면 수량이 0으로 시작하는 문제가 생겼다 (min_value=1이 가려 주던 문제).
}
for _k, _v in _DEFAULTS.items():
    if _k not in ss:
        ss[_k] = list(_v) if isinstance(_v, list) else _v


def notify(level: str, text: str) -> None:
    """level: error | warning | info | success. text는 마크다운 — 사용자 텍스트는 md()로 감쌀 것."""
    ss.notices.append((level, text))


def toast(text: str) -> None:
    ss.toasts.append(text)


def end_edit() -> None:
    ss.editing_id = None
    ss.edit_base = None
    ss.edit_orig = None


def safe(fn):
    """콜백 공통 오류 처리: 앱이 멈추지 않고 안내만 표시.

    화면에는 종류별 안내 문장(user_message)만 보여주고, 경로·API 응답 같은 원문은 서버 로그에만 남긴다.
    """

    @wraps(fn)
    def wrapper(*args):
        try:
            fn(*args)
        except ItemNotFound as e:
            if ss.editing_id == e.item_id:
                end_edit()
            storage.invalidate_cache()
            notify("warning", "다른 가족이 이미 삭제한 항목이에요. 목록을 새로 불러왔어요.")
        except StorageError as e:
            log.warning("저장 실패(%s): %s", e.kind, e)
            notify("error", f"저장하지 못했어요. {e.user_message}")
        except Exception:
            log.exception("콜백 처리 중 오류")
            notify("error", "예상하지 못한 오류가 생겼어요. 새로고침 후 다시 시도해 주세요.")

    return wrapper


# ---------------------------------------------------------------------------
# 콜백 (화면을 그리기 전에 실행됨)
# ---------------------------------------------------------------------------


def reset_add_inputs() -> None:
    """추가 폼 입력칸 초기화. 카테고리는 이어서 같은 종류를 넣기 쉽게 유지."""
    ss.add_name = ""
    ss.add_qty = 1
    ss.add_memo = ""


def on_add() -> None:
    cleaned, errors = validate_item_input(ss.add_name, ss.add_qty, ss.add_category, ss.add_memo)
    if errors:
        for msg in errors:
            notify("error", msg)
        return  # 입력값 유지
    signature = (cleaned["name"], cleaned["quantity"], cleaned["category"], cleaned["memo"])
    # 더블 탭이면 두 제출의 콜백이 서로 다른 스레드에서 거의 동시에 실행된다 (실측: 3ms 간격, 둘 다 last_add=None).
    # "중복 확인 → 저장 → 기록"을 잠금 하나로 묶어야 한 번만 저장된다.
    with _add_lock():
        recent, session_key, now = _recent_adds(), _session_key(), time.monotonic()
        for key in [k for k, (_, t) in recent.items() if now - t > 10 * REPEAT_WINDOW_SECONDS]:
            del recent[key]  # 오래된 기록 정리 (세션이 쌓여도 메모리가 늘지 않게)
        if is_repeat_submission(recent.get(session_key), signature, now):
            # 두 번째 제출은 무시하되, 이 제출이 입력칸을 다시 채워 놓지 않게 같이 비운다
            reset_add_inputs()
            return
        item = new_item(**cleaned)
        duplicate = find_open_duplicate(item["name"], storage.list_items())
        storage.add_item(item)
        recent[session_key] = (signature, time.monotonic())  # 저장에 성공했을 때만 기록
    if duplicate:
        notify("warning", f"이미 목록에 있는 이름이에요: **{md(item['name'])}** (그래도 추가했어요)")
    toast(f"추가했어요: {md(item['name'])}")
    reset_add_inputs()  # 성공했을 때만 입력칸 초기화
    if ss.get("filter", ALL) not in (ALL, item["category"]):
        ss.filter = ALL  # 필터 때문에 방금 추가한 항목이 안 보이는 일이 없도록


def on_check(item_id: str, key: str) -> None:
    storage.set_checked(item_id, bool(ss[key]))  # 반전이 아니라 체크박스의 현재 값 그대로


def start_edit(item: dict) -> None:
    item_id = item["id"]
    ss.editing_id = item_id
    ss.edit_base = item["updated_at"]
    ss.edit_orig = {field: item[field] for field in _EDIT_KEYS}
    for field, prefix in _EDIT_KEYS.items():
        ss[f"{prefix}_{item_id}"] = item[field]
    ss.confirm = None


def save_edit(item_id: str) -> None:
    current = {field: ss[f"{prefix}_{item_id}"] for field, prefix in _EDIT_KEYS.items()}
    # 내가 실제로 바꾼 필드만 저장 — 안 바꾼 필드가 오래된 값으로 다른 가족의 수정을 덮어쓰지 않게
    fields = changed_fields(ss.edit_orig or {}, current)
    if not fields:
        end_edit()
        toast("바뀐 내용이 없어요.")
        return
    try:
        updated = storage.update_item(item_id, fields, expected_updated_at=ss.edit_base)
    except ValidationError as e:
        for msg in e.errors:
            notify("error", msg)
        return  # 수정 폼 유지
    except ConflictError as e:
        # 다시 저장할 수 있도록 기준을 최신으로 갱신하고, 내가 건드리지 않은 칸은 최신 값으로 바꿔 보여준다
        ss.edit_base = e.current["updated_at"]
        for field, prefix in _EDIT_KEYS.items():
            if field not in fields:
                ss[f"{prefix}_{item_id}"] = e.current[field]
                ss.edit_orig[field] = e.current[field]
        notify(
            "warning",
            "다른 가족이 먼저 수정했어요.  \n"
            f"지금 저장된 내용: **{item_text(e.current)}** ({md(e.current['category'])})  \n"
            "**저장**을 한 번 더 누르면 내가 바꾼 부분만 최신 내용에 합쳐서 저장돼요. 그만두려면 **취소**를 누르세요.",
        )
        return
    end_edit()
    toast(f"수정했어요: {md(updated['name'])}")


def do_delete(item: dict) -> None:
    if ss.editing_id == item["id"]:
        end_edit()
    if storage.delete_item(item["id"]):
        toast(f"삭제했어요: {md(item['name'])}")
    else:
        storage.invalidate_cache()
        notify("info", "이미 삭제된 항목이에요. 목록을 새로 불러왔어요.")


def ask_confirm(kind: str, item_ids: list[str] | None = None, scope: str = "") -> None:
    """확인창을 연다. 일괄 삭제는 지금 화면에 보이는 완료 항목의 id를 함께 기억한다."""
    ss.confirm = kind
    ss.confirm_ids = list(item_ids or [])
    ss.confirm_scope = scope


def cancel_confirm() -> None:
    ss.confirm = None
    ss.confirm_ids = []


def do_delete_checked() -> None:
    # 확인창에서 본 항목 중 '지금도 체크된' 것만 삭제 (그 사이 다른 가족이 새로 체크한 항목은 건드리지 않음)
    ids = list(ss.confirm_ids)
    ss.confirm = None
    ss.confirm_ids = []
    removed = storage.delete_checked(ids)
    if removed:
        toast(f"완료한 항목 {removed}개를 정리했어요.")
    else:
        notify("info", "정리할 완료 항목이 없어요.")


def do_reset(item_ids: list[str]) -> None:
    ss.confirm = None
    end_edit()
    removed = storage.delete_items(item_ids)
    toast(f"항목 {removed}개를 모두 삭제했어요.")


def do_refresh() -> None:
    storage.invalidate_cache()
    toast("최신 목록을 불러왔어요.")


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------

st.html(CSS)

with st.container(horizontal=True, vertical_alignment="center", wrap=False, key="header"):
    st.title("🛒 우리집 쇼핑리스트", anchor=False, width="stretch")
    st.button(
        "새로고침",
        on_click=safe(do_refresh),
        help="다른 가족이 바꾼 내용을 불러와요",
        width="content",
    )

if not storage.persistent:
    if json_is_intentional():
        st.caption(f"🗂️ 개발용 {storage.label} 저장소를 쓰는 중이에요.")
    else:
        st.warning(
            f"**임시 저장소**({storage.label})를 쓰고 있어요. 배포 환경에서는 앱이 다시 시작되면 "
            "목록이 사라질 수 있어요. Google Sheets 설정(secrets)을 확인해 주세요. "
            "(로컬에서 일부러 쓰는 거라면 환경변수 `SHOPPING_STORAGE=json`)"
        )

for _text in ss.toasts:
    st.toast(_text)
ss.toasts = []
for _level, _text in ss.notices:
    getattr(st, _level)(_text)
ss.notices = []

try:
    result = storage.load()
except StorageError as e:
    log.warning("목록 불러오기 실패(%s): %s", e.kind, e)
    st.error(f"목록을 불러오지 못했어요. {e.user_message}")
    st.button("다시 시도", on_click=storage.invalidate_cache)
    st.stop()

items = result.items
ids = {it["id"] for it in items}
open_items = sorted((it for it in items if not it["checked"]), key=lambda x: x["created_at"])
done_items = sorted((it for it in items if it["checked"]), key=lambda x: x["created_at"])
done_ids = {it["id"] for it in done_items}
all_done = bool(items) and not open_items

# --- 진행 상황 ---
if items:
    st.progress(len(done_items) / len(items), text=f"{len(items)}개 중 {len(open_items)}개 남음")
else:
    st.caption("0개 중 0개 남음")

if result.skipped:
    st.warning(f"읽을 수 없는 항목 {result.skipped}개를 건너뛰었어요. 저장된 데이터를 확인해 주세요.")

if ss.editing_id and ss.editing_id not in ids:
    end_edit()
    st.info("수정하던 항목을 다른 가족이 삭제해서 수정을 취소했어요.")
if ss.confirm == "checked" and not (done_ids & set(ss.confirm_ids)) or ss.confirm == "reset" and not items:
    ss.confirm = None
    ss.confirm_ids = []

# --- F1 추가 (PC: 이름·수량·카테고리 한 줄 + 메모·추가 한 줄 / 모바일: ui_style의 CSS로 3줄) ---
# 수량 입력에는 min/max를 두지 않는다: 브라우저가 범위 밖 값을 서버로 보내지 않아 검증 메시지가 사라지고
# 이전 값으로 저장되기 때문. 범위(1~999)는 validate_item_input이 검사해 안내한다.
with st.form("add_form", clear_on_submit=False):
    with st.container(key="addrow1"):
        c_name, c_qty, c_cat = st.columns([3, 1.3, 2])
        c_name.text_input("아이템 이름", key="add_name", max_chars=NAME_MAX, placeholder="예: 우유")
        c_qty.number_input("수량", key="add_qty", value=1, step=1, format="%d")
        c_cat.selectbox(
            "카테고리", CATEGORIES, key="add_category",
            index=CATEGORIES.index(DEFAULT_CATEGORY), format_func=category_title,
        )
    with st.container(key="addrow2"):
        c_memo, c_btn = st.columns([4, 1.3], vertical_alignment="bottom")
        c_memo.text_input("메모 (선택)", key="add_memo", max_chars=MEMO_MAX, placeholder="예: 저지방, 1L")
        c_btn.form_submit_button("추가", type="primary", on_click=safe(on_add), width="stretch")

# --- F8 카테고리 필터 ---
if ss.get("filter") not in (None, ALL, *CATEGORIES):
    del ss["filter"]  # 알 수 없는 값이면 기본값('전체')으로
if items:
    st.pills(
        "카테고리 보기",
        (ALL, *CATEGORIES),
        key="filter",
        default=ALL,
        required=True,
        format_func=lambda c: f"{ALL_ICON} {ALL}" if c == ALL else category_title(c),
        label_visibility="collapsed",
    )
selected = ss.get("filter", ALL)


def visible(it: dict) -> bool:
    return selected == ALL or it["category"] == selected


def render_edit_form(it: dict) -> None:
    item_id = it["id"]
    with st.container(border=True, key="editbox"):
        st.markdown(f"**✏️ 수정 중:** {md(it['name'])}")
        with st.form(f"edit_form_{item_id}", border=False):
            st.text_input("이름", key=f"e_name_{item_id}", max_chars=NAME_MAX)
            with st.container(horizontal=True, wrap=False):  # 모바일에서도 한 줄
                st.number_input("수량", key=f"e_qty_{item_id}", step=1, format="%d", width=130)
                st.selectbox(
                    "카테고리", CATEGORIES, key=f"e_cat_{item_id}", format_func=category_title, width="stretch"
                )
            st.text_input("메모", key=f"e_memo_{item_id}", max_chars=MEMO_MAX)
            with st.container(horizontal=True):
                st.form_submit_button(
                    "저장", type="primary", on_click=safe(save_edit), args=(item_id,), width="stretch"
                )
                st.form_submit_button("취소", on_click=end_edit, width="stretch")


def render_row(it: dict) -> None:
    item_id = it["id"]
    if ss.editing_id == item_id:
        render_edit_form(it)
        return
    key = f"chk_{item_id}"
    ss[key] = it["checked"]  # 저장소 값으로 동기화 (value= 인자는 쓰지 않음)
    state = "done" if it["checked"] else "open"
    with st.container(
        horizontal=True, vertical_alignment="center", gap="small", wrap=False, key=f"row_{state}_{item_id}"
    ):
        st.checkbox(
            item_label(it), key=key, on_change=safe(on_check), args=(item_id, key),
            width="stretch", wrap=True,  # 긴 이름은 잘리지 않고 여러 줄로
        )
        # 아이콘 없이 글자 버튼: 접근 이름이 "수정"/"삭제" 그대로 읽힘 (아이콘 이름이 섞이지 않음)
        st.button(
            "수정", key=f"edit_{item_id}", type="tertiary",
            help="이 항목 수정", on_click=safe(start_edit), args=(it,), width="content",
        )
        st.button(
            "삭제", key=f"del_{item_id}", type="tertiary",
            help="이 항목 삭제", on_click=safe(do_delete), args=(it,), width="content",
        )


# --- F2 목록 ---
shown_open = [it for it in open_items if visible(it)]
shown_done = [it for it in done_items if visible(it)]

if not items:
    with st.container(key="empty"):
        st.markdown("## 🧺", text_alignment="center")
        st.markdown("**아직 추가된 아이템이 없어요**", text_alignment="center")
        st.caption(
            "위 칸에 살 물건을 적고 **추가**를 눌러 보세요. 가족 모두가 이 목록을 함께 써요.",
            text_alignment="center",
        )
elif all_done:
    st.success("🎉 **장보기 완료!** 모두 체크했어요. 맨 아래 **체크된 항목 모두 삭제**로 목록을 비울 수 있어요.")
elif not shown_open and not shown_done:
    st.info(f"{category_title(selected)}에 해당하는 아이템이 없어요.")
elif not shown_open:
    st.info(f"{category_title(selected)}은(는) 다 챙겼어요. 👍")

for n, category in enumerate(CATEGORIES):
    group = [it for it in shown_open if it["category"] == category]
    if not group:
        continue  # 빈 카테고리는 숨김
    with st.container(key=f"cathead_{n}"):
        st.markdown(f"{category_title(category)} · {len(group)}개")
    for it in group:
        render_row(it)

# --- 완료 영역 ---
if shown_done:
    st.divider()
    with st.container(key="cathead_done"):
        st.markdown(f"✅ 완료 · {len(shown_done)}개")
    for it in shown_done:
        render_row(it)

# --- F6 / F11 위험한 동작 (맨 아래, 확인 단계) ---
if items:
    st.divider()
    if ss.confirm == "checked":
        # 확인창을 띄운 시점에 화면에 보이던 항목 중 지금도 체크된 것만 센다 (지워지는 것 = 보여준 것)
        n = len(done_ids & set(ss.confirm_ids))
        scope = f"{ss.confirm_scope}의 " if ss.confirm_scope else ""
        st.warning(f"{scope}완료한 항목 {n}개를 목록에서 지울까요? 되돌릴 수 없어요.")
        with st.container(horizontal=True):
            st.button("완료 항목 삭제", type="primary", key="confirm_checked",
                      on_click=safe(do_delete_checked), width="stretch")
            st.button("취소", key="cancel_checked", on_click=cancel_confirm, width="stretch")
    elif ss.confirm == "reset":
        st.warning(f"목록의 항목 {len(items)}개를 **모두** 지울까요? 되돌릴 수 없어요.")
        with st.container(horizontal=True):
            st.button("모두 삭제", type="primary", key="confirm_reset",
                      on_click=safe(do_reset), args=([it["id"] for it in items],), width="stretch")
            st.button("취소", key="cancel_reset", on_click=cancel_confirm, width="stretch")
    else:
        # 필터를 켠 동안에는 화면에 보이는 완료 항목만 지운다 (안 보이는 항목이 같이 지워지지 않게)
        bulk_ids = [it["id"] for it in shown_done]
        bulk_scope = "" if selected == ALL else category_title(selected)
        bulk_label = (
            f"체크된 항목 모두 삭제 ({len(bulk_ids)})"
            if selected == ALL
            else f"{bulk_scope} 완료 항목 삭제 ({len(bulk_ids)})"
        )
        with st.container(horizontal=True, vertical_alignment="center"):
            st.button(
                bulk_label,
                type="primary" if all_done else "secondary",  # 장보기를 마치면 강조
                disabled=not bulk_ids,
                on_click=ask_confirm,
                args=("checked", bulk_ids, bulk_scope),
                width="stretch",
            )
            st.button("전체 초기화", type="tertiary", on_click=ask_confirm, args=("reset",), width="content")
