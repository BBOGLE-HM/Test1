"""화면 꾸미기 상수: 카테고리 아이콘, 커스텀 CSS.

- CSS는 여기 한 곳에만 두고 app.py에서 1회 주입한다. 사용자 입력은 절대 이 HTML/CSS에 넣지 않는다.
- 색은 .streamlit/config.toml 테마가 담당하고, CSS는 배치·크기·투명도 위주로 최소한만 쓴다.
- 선택자는 Streamlit data-testid와 컨테이너 key가 만드는 클래스(.st-key-<key>)만 사용한다.
  · row_open_<id> / row_done_<id> : 항목 한 줄
  · cathead_<n> : 카테고리 머리글,  editbox : 수정 중인 항목,  empty : 빈 상태
"""

from models import CATEGORIES

CATEGORY_ICONS: dict[str, str] = {
    "채소·과일": "🥬",
    "정육·수산": "🥩",
    "유제품·계란": "🥛",
    "가공식품": "🥫",
    "음료·간식": "🧃",
    "생활용품": "🧻",
    "기타": "🛍️",
}
assert set(CATEGORY_ICONS) == set(CATEGORIES)

ALL_ICON = "🧺"


def category_title(category: str) -> str:
    return f"{CATEGORY_ICONS.get(category, '')} {category}".strip()


CSS = """
<style>
:root { --sl-accent: #C2410C; --sl-accent-soft: rgba(194, 65, 12, 0.07); }
@media (prefers-color-scheme: dark) {
  :root { --sl-accent: #C2410C; --sl-accent-soft: rgba(194, 65, 12, 0.18); }
}

/* 본문 폭: 한 손으로 쓰는 목록이라 PC에서도 너무 넓지 않게 */
[data-testid="stMainBlockContainer"] { max-width: 760px; }

/* ---- 항목 한 줄 ---- */
[class*="st-key-row_"] {
  padding: 0 0.25rem 0 0.5rem;
  border-radius: 0.75rem;
  transition: background-color 120ms;
}
[class*="st-key-row_"]:hover { background: rgba(128, 128, 128, 0.07); }
/* 체크박스 칸이 줄어들 수 있어야 긴 이름이 줄바꿈되고 버튼이 밀려나지 않음 */
[class*="st-key-row_"] > [data-testid="stElementContainer"]:has([data-testid="stCheckbox"]) {
  min-width: 0; flex: 1 1 0;
}
[class*="st-key-row_"] [data-testid="stCheckbox"] { height: auto !important; }
[class*="st-key-row_"] [data-testid="stCheckbox"] label {
  min-height: 48px; height: auto !important; width: 100%; align-items: center; cursor: pointer;
  padding: 0.35rem 0;
}
[class*="st-key-row_"] [data-testid="stCheckbox"] label p {
  font-size: 1.12rem; line-height: 1.4; overflow-wrap: anywhere;
}
/* 완료 항목: 취소선(라벨) + 흐리게. 투명도 0.7 = 글자 대비 라이트 5.5:1 / 다크 8:1 (WCAG 4.5:1 이상) */
[class*="st-key-row_done_"] [data-testid="stCheckbox"] label p { opacity: 0.7; }

/* 항목 행 버튼(수정·삭제): 글자 버튼, 44px 터치 영역 */
[class*="st-key-row_"] [data-testid="stButton"] button {
  min-width: 44px; min-height: 44px; padding: 0 0.6rem; opacity: 0.85;
}
[class*="st-key-row_"] [data-testid="stButton"] button:hover { opacity: 1; }
.st-key-header [data-testid="stButton"] button { min-height: 44px; padding: 0 0.9rem; }
.st-key-header h1 { padding: 0.5rem 0; word-break: keep-all; }

/* 터치 영역 44px: 버튼, 입력칸, 드롭다운, 수량 +/- 버튼, 필터 알약 */
[data-testid="stButton"] button,
[data-testid="stFormSubmitButton"] button { min-height: 44px; }
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input { min-height: 44px; }
[data-testid="stSelectbox"] [data-baseweb="select"] > div { min-height: 44px; }
[data-testid="stNumberInput"] button { min-width: 44px; min-height: 44px; }
.st-key-filter [data-testid="stButtonGroup"] button { min-height: 44px; }

/* ---- 카테고리 머리글 ---- */
[class*="st-key-cathead_"] p {
  font-weight: 700; font-size: 1rem; margin: 0.6rem 0 0 0.25rem; opacity: 0.85;
}

/* ---- 수정 중인 항목 ---- */
.st-key-editbox {
  border: 2px solid var(--sl-accent) !important;
  background: var(--sl-accent-soft);
}

/* ---- 빈 상태 ---- */
.st-key-empty { text-align: center; padding: 1.5rem 1rem 1rem; }
.st-key-empty p { margin-bottom: 0.25rem; }

/* ---- 모바일 ---- */
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding: 1.25rem 1rem 4rem; }
  h1 { font-size: 1.3rem !important; }
  .st-key-header [data-testid="stButton"] button { padding: 0 0.7rem; }

  /* 추가 폼: Streamlit 기본(열 전부 세로로 쌓기) 대신
     1줄 이름 / 2줄 수량+카테고리 / 3줄 메모+추가 → 목록이 화면에 더 빨리 보이게 */
  .st-key-addrow1 [data-testid="stHorizontalBlock"],
  .st-key-addrow2 [data-testid="stHorizontalBlock"] {
    flex-wrap: wrap !important; flex-direction: row !important; gap: 0.5rem 0.75rem !important;
  }
  .st-key-addrow1 [data-testid="stColumn"],
  .st-key-addrow2 [data-testid="stColumn"] { min-width: 0 !important; width: auto !important; }
  .st-key-addrow1 [data-testid="stColumn"]:nth-child(1) { flex: 1 1 100% !important; }
  .st-key-addrow1 [data-testid="stColumn"]:nth-child(2) { flex: 0 0 7.5rem !important; }
  .st-key-addrow1 [data-testid="stColumn"]:nth-child(3) { flex: 1 1 0 !important; }
  .st-key-addrow2 [data-testid="stColumn"]:nth-child(1) { flex: 1 1 0 !important; }
  .st-key-addrow2 [data-testid="stColumn"]:nth-child(2) { flex: 0 0 6rem !important; }

  /* 카테고리 필터: 여러 줄 대신 한 줄 가로 스크롤 (페이지 자체는 가로 스크롤 없음) */
  .st-key-filter [data-testid="stButtonGroup"] > div {
    flex-wrap: nowrap !important; overflow-x: auto; scrollbar-width: none; padding-bottom: 2px;
  }
  .st-key-filter [data-testid="stButtonGroup"] > div::-webkit-scrollbar { display: none; }
  .st-key-filter [data-testid="stButtonGroup"] button { flex: 0 0 auto; white-space: nowrap; }
}
</style>
"""
