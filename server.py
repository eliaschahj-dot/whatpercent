"""몇프로 — 대중 공감 온도계: 실제 AI 백엔드 데모.

POST /api/questions  사연 → AI가 그 사연 맞춤 추가 질문 3~5개 생성
POST /api/predict    사연 + 답변 → AI가 공감률 예측 + 근거 (+히스토리 기록)
GET  /api/history    최근 측정 히스토리
GET  /api/daily      오늘의 논쟁 5건 (매일 AI가 웹 검색으로 선정, 날짜별 보관)
GET  /api/daily/dates 보관된 날짜 목록
GET  /api/health     자격증명 확인
GET  /                프론트엔드
"""

import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
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


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE / "favicon.ico")


@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nAllow: /\nSitemap: https://whatpercent.co.kr/sitemap.xml\n")


@app.get("/sitemap.xml")
def sitemap():
    return Response(content=SITEMAP, media_type="application/xml")


@app.on_event("startup")
def _startup():
    _kick_daily(_kst_today())


@app.get("/api/history")
def history(limit: int = 30):
    return {"items": _read_history(max(1, min(100, limit)))}


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
