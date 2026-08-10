"""몇프로 — 대중 공감 온도계: 실제 AI 백엔드 데모.

POST /api/questions  사연 → AI가 그 사연 맞춤 추가 질문 3~5개 생성
POST /api/predict    사연 + 답변 → AI가 공감률 예측 + 근거 (+히스토리 기록)
GET  /api/history    최근 측정 히스토리
GET  /api/daily      오늘의 논쟁 5건 (매일 AI가 웹 검색으로 선정, 날짜별 보관)
GET  /api/daily/dates 보관된 날짜 목록
GET  /api/health     자격증명 확인
GET  /                프론트엔드
"""

import base64
import hashlib
import hmac
import json
import os
import secrets as pysecrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

MODEL = "claude-opus-5"
BASE = Path(__file__).parent

# 데이터 보관 위치 (Render 재배포 시 초기화됨 — 영구 보관하려면 Persistent Disk를 붙이고 DATA_DIR=/data 설정)
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DAILY_DIR = DATA_DIR / "daily"
HISTORY_FILE = DATA_DIR / "history.jsonl"
DAILY_DIR.mkdir(parents=True, exist_ok=True)

KST = timezone(timedelta(hours=9))
CURATED_DATE = "2026-08-05"  # 창간호(실측 수집분) — 프론트 HTML에 보관


def _kst_today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


# ---------- 구글 로그인 + 자체 세션 토큰 ----------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
SECRET_FILE = DATA_DIR / "secret.txt"


def _session_secret() -> bytes:
    env = os.environ.get("SESSION_SECRET", "").strip()
    if env:
        return env.encode()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip().encode()
    s = pysecrets.token_hex(32)
    SECRET_FILE.write_text(s, encoding="utf-8")
    return s.encode()


SESSION_SECRET = _session_secret()


def _make_token(uid: str, name: str, picture: str) -> str:
    payload = json.dumps(
        {"u": uid, "n": name[:40], "p": picture[:300], "e": int(time.time()) + 30 * 86400},
        ensure_ascii=False,
    )
    b = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SESSION_SECRET, b.encode(), hashlib.sha256).hexdigest()[:32]
    return b + "." + sig


def _auth_user(request: Request) -> dict | None:
    """Authorization: Bearer <token> 검증. 실패 시 None."""
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    tok = h[7:].strip()
    if "." not in tok:
        return None
    b, sig = tok.rsplit(".", 1)
    good = hmac.new(SESSION_SECRET, b.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("e", 0) < time.time():
        return None
    return data

# 자격증명 해석 순서: 프로세스 환경변수 → 프로젝트 key.txt → Windows 사용자 환경변수(setx, 레지스트리)
_key_file = BASE / "key.txt"
if not os.environ.get("ANTHROPIC_API_KEY") and _key_file.exists():
    os.environ["ANTHROPIC_API_KEY"] = _key_file.read_text(encoding="utf-8").strip()
if not os.environ.get("ANTHROPIC_API_KEY"):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as _k:
            _val, _ = winreg.QueryValueEx(_k, "ANTHROPIC_API_KEY")
            if _val:
                os.environ["ANTHROPIC_API_KEY"] = _val.strip()
    except Exception:  # noqa: BLE001 — 리눅스에는 winreg가 없음
        pass

client = anthropic.Anthropic()

# ---------- 공개 운영 보호장치: IP별/전체 사용량 제한 (AI 비용 방어) ----------
RATE_PER_HOUR = int(os.environ.get("RATE_PER_HOUR", "12"))   # IP당 시간당 AI 호출 수 (측정 1회 = 호출 2번)
DAILY_CAP = int(os.environ.get("DAILY_CAP", "400"))          # 서비스 전체 하루 AI 호출 수
MAX_STORY_CHARS = 1200

_rl_lock = threading.Lock()
_rl_hits: dict[str, deque] = defaultdict(deque)
_rl_day = {"date": time.strftime("%Y-%m-%d"), "count": 0}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request):
    """제한 초과 시 JSONResponse 반환, 통과 시 None."""
    now = time.time()
    ip = _client_ip(request)
    with _rl_lock:
        today = time.strftime("%Y-%m-%d")
        if _rl_day["date"] != today:
            _rl_day["date"], _rl_day["count"] = today, 0
        if _rl_day["count"] >= DAILY_CAP:
            return JSONResponse(status_code=429, content={
                "error": "daily_cap",
                "message": "오늘 준비된 무료 측정량이 모두 소진되었습니다. 내일 다시 시도해 주세요."})
        q = _rl_hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE_PER_HOUR:
            return JSONResponse(status_code=429, content={
                "error": "rate_limit",
                "message": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요."})
        q.append(now)
        _rl_day["count"] += 1
    return None
app = FastAPI()


class StoryIn(BaseModel):
    story: str


class Answer(BaseModel):
    question: str
    answer: str


class PredictIn(BaseModel):
    story: str
    answers: list[Answer]
    topic_label: str = ""


class GoogleAuthIn(BaseModel):
    credential: str


class VoteIn(BaseModel):
    date: str
    idx: int
    side: str  # "a"(긍정) | "b"(부정)


class TrackIn(BaseModel):
    vid: str = ""
    sid: str = ""
    event: str = ""
    tab: str = ""
    ref: str = ""
    dur: int = 0


QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_label": {"type": "string", "description": "주제군 한 줄 라벨 (예: 축의금·경조사 갈등)"},
        "analysis": {"type": "string", "description": "사연에서 인식한 핵심 요소 한 문장"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "추가 조건 질문"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "선택지 2~4개",
                    },
                },
                "required": ["text", "options"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topic_label", "analysis", "questions"],
    "additionalProperties": False,
}

PREDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "percent": {"type": "integer", "description": "대중 공감률 예측 0~100 (글쓴이 편 비율)"},
        "verdict": {"type": "string", "description": "8자 내외 한 줄 판정 (예: 당신 편 우세)"},
        "summary": {"type": "string", "description": "2~3문장 근거. 한국 커뮤니티에서 유사 논쟁이 어떻게 갈리는지 구체적으로"},
        "factors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "답변한 조건"},
                    "impact": {"type": "string", "description": "예측에 미친 영향 (예: +8%p, -12%p, 영향 적음)"},
                },
                "required": ["label", "impact"],
                "additionalProperties": False,
            },
        },
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "집단 (예: 2030 여성, 5060, 기혼자)"},
                    "percent": {"type": "integer"},
                },
                "required": ["label", "percent"],
                "additionalProperties": False,
            },
            "description": "이 사연에서 온도차가 가장 큰 집단 2~4개",
        },
        "tip": {"type": "string", "description": "반대편을 설득하거나 갈등을 줄이는 실용 조언 한 문장"},
        "caveat": {"type": "string", "description": "이 수치가 모델 추정이라는 정직한 고지 한 문장"},
    },
    "required": ["percent", "verdict", "summary", "factors", "groups", "tip", "caveat"],
    "additionalProperties": False,
}

SYSTEM_QUESTIONS = """너는 '몇프로' — 한국 대중 공감 온도계 서비스의 분석 AI다.
사용자가 일상 갈등·고민·의견을 입력하면, 한국 온라인 커뮤니티(네이트판, 더쿠, 블라인드, 디시인사이드, 여성시대 등)에서
유사 논쟁의 여론이 어떤 조건에 따라 갈렸는지에 대한 지식을 바탕으로, 예측을 정교화할 추가 조건 질문을 만든다.

규칙:
- 질문 3~5개. 반드시 이 사연에 구체적으로 맞춘 질문만 (어느 사연에나 통하는 일반론 금지).
- 각 질문은 그 답에 따라 실제로 여론이 갈리는 조건이어야 한다.
- 사연에 이미 답이 나와 있는 것은 묻지 않는다.
- 선택지는 2~4개, 짧고 상호배타적으로. 존댓말 사용."""

SYSTEM_PREDICT = """너는 '몇프로' — 한국 대중 공감 온도계 서비스의 예측 AI다.
사연과 추가 조건 답변을 종합해, 한국 대중(전 연령 온라인 인구 기준)이 글쓴이 편을 들 비율을 추정한다.

규칙:
- 한국 커뮤니티(네이트판, 더쿠, 블라인드, 디시 등)에서 유사 논쟁이 실제로 어떻게 갈렸는지를 근거로 추정하되,
  커뮤니티별 성향 편향(여초/남초/연령)을 보정해 '전국 평균' 감각으로 답한다.
- percent는 과감하게 구체적 수치로. 다만 caveat에 이것이 실측 여론조사가 아닌 모델 추정임을 명시한다.
- groups에는 이 사연에서 온도차가 가장 클 것으로 예상되는 집단을 고른다 (세대·성별에 국한하지 말 것 —
  기혼/미혼, 반려인/비반려인, 직장인/자영업자 등 사연에 맞는 축 선택).
- summary는 정서의 '이유'를 설명한다 (누가 왜 공감하고, 누가 왜 반대하는지). 존댓말 사용."""


# ---------- 측정 히스토리 (다른 사람들이 뭘 재봤는지) ----------
_hist_lock = threading.Lock()


def _log_history(entry: dict):
    """예측 1건을 JSONL로 기록. 사연은 100자까지만 저장(개인정보 노출 최소화)."""
    try:
        with _hist_lock:
            with HISTORY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 1,000건 초과 시 최근 500건만 유지
            lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > 1000:
                HISTORY_FILE.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
    except OSError:
        pass  # 기록 실패는 예측 응답을 막지 않는다


def _read_history(limit: int = 30) -> list[dict]:
    try:
        with _hist_lock:
            if not HISTORY_FILE.exists():
                return []
            lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines[-limit * 2:]):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


# ---------- 관리자·통계 데이터 수집 ----------
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
EVENTS_FILE = DATA_DIR / "events.jsonl"  # 초기 버전 파일 — 읽기 시 포함 (이후는 월별 로테이션)
EVENTS_DIR = DATA_DIR / "events"
USERS_FILE = DATA_DIR / "users.jsonl"
MEASURES_FILE = DATA_DIR / "measures.jsonl"
EVENTS_DIR.mkdir(parents=True, exist_ok=True)
_stats_lock = threading.Lock()


def _append_jsonl(path: Path, entry: dict):
    """JSONL append — 데이터는 삭제하지 않고 전부 보관한다."""
    try:
        with _stats_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _events_file_now() -> Path:
    """방문 이벤트는 월별 파일로 로테이션해 영구 보관 (예: events/2026-08.jsonl)."""
    return EVENTS_DIR / (datetime.now(KST).strftime("%Y-%m") + ".jsonl")


def _read_all_events() -> list[dict]:
    out = _read_jsonl(EVENTS_FILE)
    for p in sorted(EVENTS_DIR.glob("????-??.jsonl")):
        out.extend(_read_jsonl(p))
    return out


def _read_jsonl(path: Path) -> list[dict]:
    try:
        with _stats_lock:
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _ip_hash(request: Request) -> str:
    """원본 IP는 저장하지 않고 비밀키 솔트 해시 12자리만 보관."""
    return hashlib.sha256(SESSION_SECRET + _client_ip(request).encode()).hexdigest()[:12]


def _kst_date_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d")


# ---------- 논쟁 투표 (구글 로그인 사용자, 오늘만) ----------
VOTES_FILE = DATA_DIR / "votes.jsonl"
PROFILE_DIR = DATA_DIR / "profiles"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
_votes_lock = threading.Lock()

AXIS_LABELS = ["공감", "원칙", "실리", "전통", "개인 자유", "공동체", "직설", "신중"]


def _append_vote(entry: dict):
    with _votes_lock:
        with VOTES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_all_votes() -> list[dict]:
    with _votes_lock:
        if not VOTES_FILE.exists():
            return []
        lines = VOTES_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _effective_votes(votes: list[dict]) -> dict:
    """(uid, date, idx) → 마지막 투표(재클릭 시 변경 허용)."""
    eff = {}
    for v in votes:
        eff[(v.get("uid"), v.get("date"), v.get("idx"))] = v
    return eff


def _vote_counts(date: str) -> dict:
    """해당 날짜의 항목별 긍정/부정 집계."""
    eff = _effective_votes([v for v in _read_all_votes() if v.get("date") == date])
    counts: dict[int, dict[str, int]] = {}
    for v in eff.values():
        c = counts.setdefault(v["idx"], {"a": 0, "b": 0})
        c[v["side"]] = c.get(v["side"], 0) + 1
    return counts


PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "persona": {"type": "string", "description": "성향 별명 한 줄 (예: 따뜻한 원칙주의자)"},
        "summary": {"type": "string", "description": "투표 이력에서 드러나는 성향 2~3문장, 존댓말"},
        "axes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "score": {"type": "integer", "description": "0~100"},
                },
                "required": ["label", "score"],
                "additionalProperties": False,
            },
            "description": "정확히 8개, 고정 순서: 공감, 원칙, 실리, 전통, 개인 자유, 공동체, 직설, 신중",
        },
    },
    "required": ["persona", "summary", "axes"],
    "additionalProperties": False,
}

SYSTEM_PROFILE = """너는 '몇프로' — 한국 대중 공감 온도계 서비스의 성향 분석 AI다.
사용자가 논쟁들에서 어느 편(긍정=글쓴이/찬성 편, 부정=반대 편)을 골랐는지 이력을 보고,
아래 8개 축 각각을 0~100으로 채점한다 (50이 중간). 축은 반드시 이 순서·이 이름 그대로:
공감(타인 감정 이해), 원칙(규칙·도리 중시), 실리(실용·경제성), 전통(관습·격식 존중),
개인 자유(개인 선택 우선), 공동체(집단·배려 우선), 직설(할 말은 함), 신중(중재·갈등 회피).
persona는 매력적인 별명 한 줄, summary는 근거가 드러나는 요약. 존댓말 사용."""


# ---------- 오늘의 논쟁: 매일 AI가 웹 검색으로 5건 선정 ----------
DAILY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "논쟁 제목 (커뮤니티 글 제목 느낌)"},
                    "source": {"type": "string", "description": "논쟁이 도는 곳 (예: 네이트판, 더쿠, 커뮤니티 전반)"},
                    "summary": {"type": "string", "description": "무엇이 쟁점인지 2~3문장, 존댓말"},
                    "side_a": {"type": "string", "description": "글쓴이/찬성 편 입장 한 줄"},
                    "side_b": {"type": "string", "description": "반대 편 입장 한 줄"},
                    "percent": {"type": "integer", "description": "전국 평균 기준 side_a 공감률 예측 0~100"},
                    "verdict": {"type": "string", "description": "8자 내외 한 줄 판정"},
                    "groups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "percent": {"type": "integer"},
                            },
                            "required": ["label", "percent"],
                            "additionalProperties": False,
                        },
                        "description": "온도차가 큰 집단 2~3개",
                    },
                },
                "required": ["title", "source", "summary", "side_a", "side_b", "percent", "verdict", "groups"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_DAILY_SEARCH = """너는 '몇프로' — 한국 대중 공감 온도계 서비스의 에디터 AI다.
웹 검색으로 지금 한국 온라인 커뮤니티(네이트판, 더쿠, 디시인사이드, 인스티즈, X 등)에서
실제로 화제가 되고 있는 '찬반이 갈리는 생활 논쟁'을 찾아라.
정치·종교·특정인 비방은 제외하고, 일상 갈등(경조사, 가사분담, 직장, 연애, 이웃, 소비 습관 등)을 우선한다.
찾은 논쟁 5건에 대해 각각: 제목, 어디서 도는지, 쟁점 요약, 양측 입장을 정리해라."""

SYSTEM_DAILY_STRUCT = """너는 '몇프로' — 한국 대중 공감 온도계 서비스의 예측 AI다.
아래 조사 내용을 바탕으로 논쟁 5건을 구조화하고, 각 논쟁에 대해
한국 대중(전 연령 온라인 인구, 커뮤니티 편향 보정한 전국 평균)이 side_a에 공감할 비율을 추정하라.
percent는 과감하게 구체적으로, groups에는 그 논쟁에서 온도차가 가장 클 집단을 고른다. 존댓말 사용."""

_daily_lock = threading.Lock()
_daily_state = {"generating": None, "last_error": None, "error_at": 0.0}


def _search_trending() -> str:
    """웹 검색 도구로 오늘의 논쟁을 조사 (pause_turn 재개 처리 포함)."""
    msgs = [{"role": "user", "content": "오늘 한국 커뮤니티에서 화제인 찬반 논쟁 5건을 찾아 정리해라."}]
    resp = None
    for _ in range(6):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=6000,
            system=SYSTEM_DAILY_SEARCH,
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            messages=msgs,
        )
        if resp.stop_reason == "pause_turn":
            msgs = [msgs[0], {"role": "assistant", "content": resp.content}]
            continue
        break
    if resp is None or resp.stop_reason == "refusal":
        raise RuntimeError("검색 요청이 거절되었습니다.")
    return "\n".join(b.text for b in resp.content if b.type == "text")


def _generate_daily(date_str: str):
    """오늘의 논쟁 5건 생성 → data/daily/YYYY-MM-DD.json 저장. 백그라운드 스레드에서 실행."""
    try:
        method = "web_search"
        try:
            research = _search_trending()
        except Exception:  # noqa: BLE001 — 검색 실패 시 모델 지식 기반으로 폴백
            method = "knowledge"
            research = ("웹 검색을 사용할 수 없다. 네가 아는 한국 커뮤니티에서 반복적으로 화제가 되는 "
                        "'찬반이 갈리는 생활 논쟁' 유형 중 요즘 시기에 어울리는 5건을 만들어라.")
        data = _call(SYSTEM_DAILY_STRUCT, f"조사 내용:\n{research}\n\n논쟁 5건을 구조화하라.", DAILY_SCHEMA)
        items = data.get("items", [])[:5]
        for it in items:
            it["percent"] = max(0, min(100, int(it.get("percent", 50))))
        payload = {
            "date": date_str,
            "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "method": method,
            "items": items,
        }
        tmp = DAILY_DIR / f"{date_str}.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DAILY_DIR / f"{date_str}.json")
        with _daily_lock:
            _daily_state["last_error"] = None
    except Exception as e:  # noqa: BLE001
        with _daily_lock:
            _daily_state["last_error"] = str(e)
            _daily_state["error_at"] = time.time()
    finally:
        with _daily_lock:
            _daily_state["generating"] = None


def _kick_daily(date_str: str):
    """오늘자 파일이 없으면 생성 스레드를 1개만 띄운다. 실패 후 10분간 재시도 금지."""
    with _daily_lock:
        if (DAILY_DIR / f"{date_str}.json").exists():
            return
        if _daily_state["generating"] == date_str:
            return
        if _daily_state["last_error"] and time.time() - _daily_state["error_at"] < 600:
            return
        _daily_state["generating"] = date_str
    threading.Thread(target=_generate_daily, args=(date_str,), daemon=True).start()


def _call(system: str, user: str, schema: dict) -> dict:
    """Claude 호출 (구조화 출력). 서버측 폴백 베타를 우선 시도, 미지원 조직이면 일반 경로로 재시도."""
    kwargs = dict(
        model=MODEL,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    try:
        resp = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            **kwargs,
        )
    except anthropic.BadRequestError:
        resp = client.messages.create(
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            **kwargs,
        )
    if resp.stop_reason == "refusal":
        raise RuntimeError("요청이 안전 분류기에 의해 거절되었습니다. 다른 표현으로 시도해 주세요.")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://whatpercent.co.kr/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
</urlset>
"""


# 검색엔진 소유 확인 메타태그: 환경변수만 넣으면 자동 삽입 (코드 수정 불필요)
_index_cache: str | None = None


def _index_html() -> str:
    global _index_cache
    if _index_cache is None:
        html = (BASE / "index.html").read_text(encoding="utf-8")
        tags = ""
        gv = os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip().strip('"')
        nv = os.environ.get("NAVER_SITE_VERIFICATION", "").strip().strip('"')
        if gv:
            tags += f'\n<meta name="google-site-verification" content="{gv}">'
        if nv:
            tags += f'\n<meta name="naver-site-verification" content="{nv}">'
        if tags:
            html = html.replace("</title>", "</title>" + tags, 1)
        _index_cache = html
    return _index_cache


@app.get("/")
def index():
    return HTMLResponse(_index_html())


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE / "favicon.ico")


@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nAllow: /\nDisallow: /admin\nSitemap: https://whatpercent.co.kr/sitemap.xml\n")


@app.get("/sitemap.xml")
def sitemap():
    return Response(content=SITEMAP, media_type="application/xml")


def _daily_scheduler():
    """매일 밤 12시(KST) 직후 오늘의 논쟁을 미리 생성. 실패 시 15분 간격 최대 4회 재시도."""
    while True:
        now = datetime.now(KST)
        nxt = now.replace(hour=0, minute=0, second=30, microsecond=0) + timedelta(days=1)
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        for _ in range(4):
            today = _kst_today()
            if (DAILY_DIR / f"{today}.json").exists():
                break
            _kick_daily(today)
            time.sleep(900)


_scheduler_started = False


@app.on_event("startup")
def _startup():
    global _scheduler_started
    _kick_daily(_kst_today())
    if not _scheduler_started:
        _scheduler_started = True
        threading.Thread(target=_daily_scheduler, daemon=True).start()


@app.get("/api/history")
def history(limit: int = 30):
    return {"items": _read_history(max(1, min(100, limit)))}


@app.get("/api/config")
def config():
    return {"google_client_id": GOOGLE_CLIENT_ID or None}


@app.post("/api/track", status_code=204)
def track(body: TrackIn, request: Request):
    if body.event not in ("view", "tab", "leave", "hb"):
        return Response(status_code=204)
    user = _auth_user(request)
    entry = {
        "ts": int(time.time()),
        "vid": body.vid[:40], "sid": body.sid[:40], "event": body.event,
        "ip": _ip_hash(request),
        "dur": max(0, min(86400, body.dur)),
    }
    if body.event == "view":
        entry["ref"] = body.ref[:200]
        entry["ua"] = request.headers.get("user-agent", "")[:120]
    if body.event == "tab":
        entry["tab"] = body.tab[:20]
    if user:
        entry["uid"] = user["u"]
    _append_jsonl(_events_file_now(), entry)
    return Response(status_code=204)


def _admin_guard(request: Request):
    if not ADMIN_KEY:
        return JSONResponse(status_code=503, content={
            "error": "not_configured",
            "message": "관리자 키가 설정되지 않았습니다. Render 환경변수 ADMIN_KEY를 추가하세요."})
    if not hmac.compare_digest(request.headers.get("x-admin-key", ""), ADMIN_KEY):
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "관리자 키가 올바르지 않습니다."})
    return None


@app.get("/api/admin/dashboard")
def admin_dashboard(request: Request):
    guard = _admin_guard(request)
    if guard:
        return guard

    events = _read_all_events()
    daily: dict[str, dict] = {}
    refs: dict[str, int] = {}
    tabs: dict[str, int] = {}
    sess_dur: dict[str, dict[str, int]] = {}
    for e in events:
        d = _kst_date_of(e["ts"])
        day = daily.setdefault(d, {"views": 0, "uniq": set()})
        if e.get("vid"):
            day["uniq"].add(e["vid"])
        if e["event"] == "view":
            day["views"] += 1
            r = (e.get("ref") or "").strip()
            if r:
                r = r.split("//")[-1].split("/")[0][:60]
            refs[r or "직접 방문"] = refs.get(r or "직접 방문", 0) + 1
        elif e["event"] == "tab":
            tabs[e.get("tab", "?")] = tabs.get(e.get("tab", "?"), 0) + 1
        sd = sess_dur.setdefault(d, {})
        sid = e.get("sid", "")
        if sid:
            sd[sid] = max(sd.get(sid, 0), e.get("dur", 0))
    daily_out = []
    for d in sorted(daily):
        durs = list(sess_dur.get(d, {}).values())
        daily_out.append({
            "date": d, "views": daily[d]["views"], "uniques": len(daily[d]["uniq"]),
            "sessions": len(durs),
            "avg_dur": int(sum(durs) / len(durs)) if durs else 0,
        })

    users_raw = _read_jsonl(USERS_FILE)
    votes_all = _read_all_votes()
    eff_votes = list(_effective_votes(votes_all).values())
    measures = _read_jsonl(MEASURES_FILE)
    vote_by_uid: dict[str, int] = {}
    for v in eff_votes:
        vote_by_uid[v.get("uid", "")] = vote_by_uid.get(v.get("uid", ""), 0) + 1
    meas_by_uid: dict[str, int] = {}
    for m in measures:
        if m.get("uid"):
            meas_by_uid[m["uid"]] = meas_by_uid.get(m["uid"], 0) + 1
    users: dict[str, dict] = {}
    for u in users_raw:
        rec = users.setdefault(u["uid"], {
            "uid": u["uid"], "name": u.get("name", ""), "email": u.get("email", ""),
            "first_login": u["ts"], "last_login": u["ts"], "logins": 0,
        })
        rec["logins"] += 1
        rec["last_login"] = max(rec["last_login"], u["ts"])
        rec["first_login"] = min(rec["first_login"], u["ts"])
        rec["name"] = u.get("name", rec["name"])
    users_out = sorted(users.values(), key=lambda r: -r["last_login"])
    for r in users_out:
        r["votes"] = vote_by_uid.get(r["uid"], 0)
        r["measures"] = meas_by_uid.get(r["uid"], 0)

    name_by_uid = {r["uid"]: r["name"] for r in users_out}
    votes_out = [{
        "ts": v.get("ts", 0), "date": v.get("date", ""), "idx": v.get("idx", 0),
        "side": v.get("side", ""), "title": v.get("title", ""),
        "name": name_by_uid.get(v.get("uid", ""), "(알 수 없음)"),
    } for v in sorted(eff_votes, key=lambda v: -v.get("ts", 0))[:200]]

    measures_out = [{
        "ts": m.get("ts", 0), "story": m.get("story", ""), "label": m.get("label", ""),
        "percent": m.get("percent", 0), "verdict": m.get("verdict", ""),
        "name": m.get("name", ""), "ip": m.get("ip", ""),
        "answers": m.get("answers", []),
    } for m in sorted(measures, key=lambda m: -m.get("ts", 0))[:200]]

    # 페르소나 캐시 보유 여부를 가입자 목록에 표시
    for r in users_out:
        pc = _read_profile_cache(r["uid"])
        r["persona"] = (pc or {}).get("profile", {}).get("persona", "")

    all_view = [e for e in events if e.get("event") == "view"]
    return {
        "daily": daily_out,
        "refs": sorted(refs.items(), key=lambda kv: -kv[1])[:15],
        "tabs": tabs,
        "users": users_out,
        "votes": votes_out,
        "measures": measures_out,
        "totals": {
            "views": len(all_view),
            "visitors": len({e.get("vid") for e in events if e.get("vid")}),
            "users": len(users_out),
            "measures": len(measures),
            "votes": len(eff_votes),
        },
    }


@app.get("/api/admin/user/{uid}")
def admin_user_detail(uid: str, request: Request):
    guard = _admin_guard(request)
    if guard:
        return guard
    users_raw = [u for u in _read_jsonl(USERS_FILE) if u.get("uid") == uid]
    if not users_raw:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    info = {
        "uid": uid,
        "name": users_raw[-1].get("name", ""),
        "email": users_raw[-1].get("email", ""),
        "first_login": min(u["ts"] for u in users_raw),
        "last_login": max(u["ts"] for u in users_raw),
        "logins": len(users_raw),
    }
    votes = sorted(_my_vote_list(uid), key=lambda v: -v.get("ts", 0))
    measures = [m for m in _read_jsonl(MEASURES_FILE) if m.get("uid") == uid]
    measures.sort(key=lambda m: -m.get("ts", 0))
    cached = _read_profile_cache(uid)
    return {
        "user": info,
        "votes": [{"ts": v.get("ts", 0), "date": v.get("date", ""), "title": v.get("title", ""),
                   "side": v.get("side", ""), "side_text": v.get("side_text", "")} for v in votes],
        "measures": [{"ts": m.get("ts", 0), "story": m.get("story", ""),
                      "percent": m.get("percent", 0), "verdict": m.get("verdict", "")} for m in measures[:50]],
        "profile": cached,
    }


@app.post("/api/admin/user/{uid}/analyze")
def admin_user_analyze(uid: str, request: Request):
    guard = _admin_guard(request)
    if guard:
        return guard
    n = len(_my_vote_list(uid))
    if n < 3:
        return JSONResponse(status_code=400, content={
            "error": "not_enough", "message": f"투표가 {n}건뿐입니다. 3건 이상이어야 분석할 수 있습니다."})
    users_raw = [u for u in _read_jsonl(USERS_FILE) if u.get("uid") == uid]
    name = users_raw[-1].get("name", "") if users_raw else ""
    try:
        cached = _compute_profile(uid, name)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": "ai", "message": str(e)})
    return {"status": "ok", "profile": cached}


@app.get("/api/admin/export/{kind}")
def admin_export(kind: str, request: Request):
    guard = _admin_guard(request)
    if guard:
        return guard
    import csv
    import io

    def _iso(ts):
        return datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d %H:%M:%S") if ts else ""

    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "measures":
        w.writerow(["시각", "사용자", "uid", "IP해시", "사연", "추가답변", "주제군", "공감률", "판정"])
        for m in _read_jsonl(MEASURES_FILE):
            qa = " | ".join(f"{a.get('q', '')} → {a.get('a', '')}" for a in m.get("answers", []))
            w.writerow([_iso(m.get("ts", 0)), m.get("name", ""), m.get("uid", ""), m.get("ip", ""),
                        m.get("story", ""), qa, m.get("label", ""), m.get("percent", 0), m.get("verdict", "")])
    elif kind == "votes":
        w.writerow(["시각", "논쟁날짜", "항목", "선택", "논쟁제목", "선택입장", "uid"])
        for v in sorted(_read_all_votes(), key=lambda v: v.get("ts", 0)):
            w.writerow([_iso(v.get("ts", 0)), v.get("date", ""), v.get("idx", 0),
                        "긍정" if v.get("side") == "a" else "부정",
                        v.get("title", ""), v.get("side_text", ""), v.get("uid", "")])
    elif kind == "users":
        w.writerow(["uid", "이름", "이메일", "로그인시각"])
        for u in _read_jsonl(USERS_FILE):
            w.writerow([u.get("uid", ""), u.get("name", ""), u.get("email", ""), _iso(u.get("ts", 0))])
    elif kind == "daily":
        events = _read_all_events()
        daily: dict[str, dict] = {}
        sess: dict[str, dict[str, int]] = {}
        for e in events:
            d = _kst_date_of(e.get("ts", 0))
            day = daily.setdefault(d, {"views": 0, "uniq": set()})
            if e.get("vid"):
                day["uniq"].add(e["vid"])
            if e.get("event") == "view":
                day["views"] += 1
            if e.get("sid"):
                sd = sess.setdefault(d, {})
                sd[e["sid"]] = max(sd.get(e["sid"], 0), e.get("dur", 0))
        w.writerow(["날짜", "페이지뷰", "방문자", "세션", "평균체류(초)"])
        for d in sorted(daily):
            durs = list(sess.get(d, {}).values())
            w.writerow([d, daily[d]["views"], len(daily[d]["uniq"]), len(durs),
                        int(sum(durs) / len(durs)) if durs else 0])
    else:
        return JSONResponse(status_code=404, content={"error": "unknown_kind"})
    return Response(
        content="﻿" + buf.getvalue(),  # BOM — 엑셀 한글 인코딩용
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="whatpercent-{kind}-{_kst_today()}.csv"'},
    )


@app.get("/admin")
def admin_page():
    return FileResponse(BASE / "admin.html")


@app.post("/api/auth/google")
def auth_google(body: GoogleAuthIn):
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(status_code=503, content={
            "error": "not_configured",
            "message": "구글 로그인이 아직 설정되지 않았습니다 (GOOGLE_CLIENT_ID 환경변수 필요)."})
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token as g_id_token
        info = g_id_token.verify_oauth2_token(
            body.credential, g_requests.Request(), GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10,
        )
    except Exception as e:  # noqa: BLE001 — 만료·위조·클라이언트ID 불일치 등
        print(f"[auth] google verify failed: {type(e).__name__}: {e}", flush=True)
        return JSONResponse(status_code=401, content={
            "error": "invalid_token", "message": "구글 토큰 검증에 실패했습니다. 다시 로그인해 주세요."})
    uid = str(info["sub"])
    name = info.get("name") or (info.get("email") or "사용자").split("@")[0]
    picture = info.get("picture", "")
    _append_jsonl(USERS_FILE, {
        "uid": uid, "name": name[:40], "email": (info.get("email") or "")[:80],
        "picture": picture[:300], "ts": int(time.time()),
    })
    return {"token": _make_token(uid, name, picture), "name": name, "picture": picture}


@app.post("/api/vote")
def vote(body: VoteIn, request: Request):
    user = _auth_user(request)
    if not user:
        return JSONResponse(status_code=401, content={
            "error": "login_required", "message": "구글 로그인을 해야 참여할 수 있습니다."})
    today = _kst_today()
    if body.date != today:
        return JSONResponse(status_code=400, content={
            "error": "not_today", "message": "투표는 '오늘'의 논쟁에만 참여할 수 있습니다."})
    if body.side not in ("a", "b"):
        return JSONResponse(status_code=400, content={"error": "bad_side"})
    f = DAILY_DIR / f"{today}.json"
    if not f.exists():
        return JSONResponse(status_code=400, content={"error": "no_daily", "message": "오늘의 논쟁이 아직 준비되지 않았습니다."})
    items = json.loads(f.read_text(encoding="utf-8")).get("items", [])
    if not (0 <= body.idx < len(items)):
        return JSONResponse(status_code=400, content={"error": "bad_idx"})
    it = items[body.idx]
    _append_vote({
        "uid": user["u"], "date": today, "idx": body.idx, "side": body.side,
        "title": it.get("title", "")[:120],
        "side_text": (it.get("side_a") if body.side == "a" else it.get("side_b", ""))[:120],
        "ts": int(time.time()),
    })
    counts = _vote_counts(today).get(body.idx, {"a": 0, "b": 0})
    return {"ok": True, "my": body.side, "counts": counts}


@app.get("/api/votes/me")
def votes_me(request: Request, date: str | None = None):
    user = _auth_user(request)
    if not user:
        return JSONResponse(status_code=401, content={
            "error": "login_required", "message": "구글 로그인을 해야 참여할 수 있습니다."})
    d = (date or _kst_today()).strip()
    eff = _effective_votes([v for v in _read_all_votes() if v.get("date") == d])
    mine = {str(v["idx"]): v["side"] for v in eff.values() if v.get("uid") == user["u"]}
    counts = {str(k): c for k, c in _vote_counts(d).items()}
    total = len(_effective_votes([v for v in _read_all_votes() if v.get("uid") == user["u"]]))
    return {"votes": mine, "counts": counts, "my_total": total}


def _profile_cache_file(uid: str) -> Path:
    return PROFILE_DIR / (hashlib.sha256(uid.encode()).hexdigest()[:24] + ".json")


def _my_vote_list(uid: str) -> list[dict]:
    my = [v for v in _effective_votes(_read_all_votes()).values() if v.get("uid") == uid]
    my.sort(key=lambda v: (v.get("date", ""), v.get("idx", 0)))
    return my


def _read_profile_cache(uid: str) -> dict | None:
    f = _profile_cache_file(uid)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _compute_profile(uid: str, name: str = "") -> dict:
    """투표 이력 → 8축 페르소나 계산 + 캐시 저장. AI 실패 시 예외."""
    my = _my_vote_list(uid)
    n = len(my)
    lines = "\n".join(
        f"- [{v.get('date')}] {v.get('title')} → {'긍정' if v.get('side') == 'a' else '부정'}"
        f" (선택한 입장: {v.get('side_text', '')})"
        for v in my[-40:]
    )
    data = _call(SYSTEM_PROFILE, f"투표 이력 {n}건:\n{lines}\n\n8축 성향을 분석하라.", PROFILE_SCHEMA)
    axes = data.get("axes", [])[:8]
    fixed = []
    for i, lab in enumerate(AXIS_LABELS):
        score = 50
        if i < len(axes):
            try:
                score = max(0, min(100, int(axes[i].get("score", 50))))
            except (TypeError, ValueError):
                score = 50
        fixed.append({"label": lab, "score": score})
    profile = {"persona": str(data.get("persona", ""))[:40],
               "summary": str(data.get("summary", ""))[:400], "axes": fixed}
    cached = {"uid": uid, "name": name[:40], "count": n, "profile": profile,
              "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M")}
    try:
        _profile_cache_file(uid).write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return cached


@app.get("/api/profile/me")
def profile_me(request: Request):
    user = _auth_user(request)
    if not user:
        return JSONResponse(status_code=401, content={
            "error": "login_required", "message": "구글 로그인을 해야 분석을 볼 수 있습니다."})
    n = len(_my_vote_list(user["u"]))
    if n < 3:
        return {"status": "not_enough", "count": n,
                "message": f"지금까지 {n}건 참여했습니다. 3건 이상 투표하면 성향을 분석해 드려요."}
    cached = _read_profile_cache(user["u"])
    if cached and cached.get("count") == n:
        return {"status": "ok", "count": n, "profile": cached["profile"]}
    limited = _rate_limit(request)
    if limited:
        return limited
    try:
        cached = _compute_profile(user["u"], user.get("n", ""))
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": "ai", "message": str(e)})
    return {"status": "ok", "count": cached["count"], "profile": cached["profile"]}


@app.get("/api/daily")
def daily(date: str | None = None):
    today = _kst_today()
    d = (date or today).strip()
    if not (len(d) == 10 and d[4] == "-" and d[7] == "-" and d.replace("-", "").isdigit()):
        return JSONResponse(status_code=400, content={"error": "bad_date"})
    f = DAILY_DIR / f"{d}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return JSONResponse(status_code=500, content={"error": "read_failed"})
    if d == today:
        _kick_daily(today)
        with _daily_lock:
            err = _daily_state["last_error"]
            generating = _daily_state["generating"] == today
        if err and not generating:
            return JSONResponse(status_code=503, content={
                "status": "error", "date": today,
                "message": "오늘의 논쟁 생성에 실패했습니다. 잠시 후 자동으로 재시도합니다."})
        return JSONResponse(status_code=202, content={"status": "generating", "date": today})
    return JSONResponse(status_code=404, content={"error": "not_found", "date": d})


@app.get("/api/daily/dates")
def daily_dates():
    dates = sorted((p.stem for p in DAILY_DIR.glob("????-??-??.json")), reverse=True)
    return {"dates": dates, "curated": CURATED_DATE, "today": _kst_today()}


@app.get("/api/health")
def health():
    try:
        client.messages.count_tokens(
            model=MODEL, messages=[{"role": "user", "content": "ping"}]
        )
        return {"ok": True, "model": MODEL}
    except anthropic.AuthenticationError:
        return JSONResponse(
            status_code=200,
            content={"ok": False, "reason": "auth",
                     "message": "Anthropic API 자격증명이 없습니다. 환경변수 ANTHROPIC_API_KEY를 설정한 뒤 서버를 재시작하세요."},
        )
    except Exception as e:  # noqa: BLE001 — 데모: 사유를 그대로 화면에 노출
        msg = str(e)
        if "Could not resolve authentication" in msg:
            return JSONResponse(status_code=200, content={
                "ok": False, "reason": "auth",
                "message": "Anthropic API 키가 설정되어 있지 않습니다. console.anthropic.com에서 키를 발급받아 "
                           "환경변수 ANTHROPIC_API_KEY로 설정한 뒤 서버를 재시작하세요.",
            })
        return JSONResponse(status_code=200, content={"ok": False, "reason": "error", "message": msg})


@app.post("/api/questions")
def make_questions(body: StoryIn, request: Request):
    if len(body.story) > MAX_STORY_CHARS:
        return JSONResponse(status_code=400, content={"error": "too_long", "message": f"사연은 {MAX_STORY_CHARS}자 이내로 입력해 주세요."})
    limited = _rate_limit(request)
    if limited:
        return limited
    try:
        data = _call(
            SYSTEM_QUESTIONS,
            f"사연:\n{body.story.strip()}\n\n이 사연의 공감률 예측을 정교화할 추가 조건 질문을 만들어라.",
            QUESTIONS_SCHEMA,
        )
        return data
    except anthropic.AuthenticationError:
        return JSONResponse(status_code=401, content={"error": "auth", "message": "API 자격증명이 없습니다. ANTHROPIC_API_KEY를 설정하세요."})
    except anthropic.RateLimitError:
        return JSONResponse(status_code=429, content={"error": "rate_limit", "message": "요청이 몰렸습니다. 잠시 후 다시 시도해 주세요."})
    except anthropic.APIStatusError as e:
        return JSONResponse(status_code=502, content={"error": "api", "message": f"API 오류 ({e.status_code})"})
    except anthropic.APIConnectionError:
        return JSONResponse(status_code=502, content={"error": "network", "message": "네트워크 오류. 인터넷 연결을 확인하세요."})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": "server", "message": str(e)})


@app.post("/api/predict")
def predict(body: PredictIn, request: Request):
    if len(body.story) > MAX_STORY_CHARS or len(body.answers) > 8:
        return JSONResponse(status_code=400, content={"error": "too_long", "message": "입력이 너무 깁니다."})
    limited = _rate_limit(request)
    if limited:
        return limited
    qa = "\n".join(f"- {a.question} → {a.answer}" for a in body.answers)
    try:
        data = _call(
            SYSTEM_PREDICT,
            f"사연:\n{body.story.strip()}\n\n추가 조건 답변:\n{qa}\n\n대중 공감률을 예측하라.",
            PREDICT_SCHEMA,
        )
        data["percent"] = max(0, min(100, int(data["percent"])))
        _log_history({
            "ts": int(time.time()),
            "story": body.story.strip()[:100],
            "label": body.topic_label.strip()[:40],
            "percent": data["percent"],
            "verdict": str(data.get("verdict", ""))[:20],
        })
        _user = _auth_user(request)
        _append_jsonl(MEASURES_FILE, {
            "ts": int(time.time()), "ip": _ip_hash(request),
            "uid": _user["u"] if _user else "", "name": _user["n"] if _user else "",
            "story": body.story.strip(),
            "answers": [{"q": a.question[:200], "a": a.answer[:100]} for a in body.answers[:8]],
            "label": body.topic_label.strip()[:40],
            "percent": data["percent"], "verdict": str(data.get("verdict", ""))[:20],
        })
        return data
    except anthropic.AuthenticationError:
        return JSONResponse(status_code=401, content={"error": "auth", "message": "API 자격증명이 없습니다. ANTHROPIC_API_KEY를 설정하세요."})
    except anthropic.RateLimitError:
        return JSONResponse(status_code=429, content={"error": "rate_limit", "message": "요청이 몰렸습니다. 잠시 후 다시 시도해 주세요."})
    except anthropic.APIStatusError as e:
        return JSONResponse(status_code=502, content={"error": "api", "message": f"API 오류 ({e.status_code})"})
    except anthropic.APIConnectionError:
        return JSONResponse(status_code=502, content={"error": "network", "message": "네트워크 오류. 인터넷 연결을 확인하세요."})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": "server", "message": str(e)})
