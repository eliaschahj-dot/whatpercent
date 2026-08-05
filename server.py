"""몇프로 — 대중 공감 온도계: 실제 AI 백엔드 데모.

POST /api/questions  사연 → AI가 그 사연 맞춤 추가 질문 3~5개 생성
POST /api/predict    사연 + 답변 → AI가 공감률 예측 + 근거
GET  /api/health     자격증명 확인
GET  /                프론트엔드
"""

import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

MODEL = "claude-opus-5"
BASE = Path(__file__).parent

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


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


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
