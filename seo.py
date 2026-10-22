"""검색 유입용 서버 렌더 페이지 (몇프로 SEO 레이어).

왜 필요한가: '오늘의 논쟁'은 매일 5건씩 쌓여 이미 상당한 분량이지만, 전부
index.html의 자바스크립트 탭 안에서만 그려졌다. 검색엔진 크롤러는 그 내용을
보지 못하고 사이트맵에도 홈 URL 하나만 있었다. 이 모듈은 같은 데이터를
자바스크립트 없이 읽히는 HTML로 다시 내보내고, 사이트맵·RSS까지 만든다.

구성
  /debates                  날짜별 아카이브 허브 (페이지네이션)
  /debates/{날짜}            그날의 논쟁 5건
  /debates/{날짜}/{번호}      논쟁 1건 상세 (롱테일 검색 대응)
  /topics                   주제 색인
  /topics/{주제}             주제별 논쟁 모음
  /guide/{주제}              주제별 데이터 리포트(우리 측정값 집계 = 독자 콘텐츠)
  /sitemap.xml, /rss.xml    전체 URL 색인 / 네이버 RSS 제출용
"""

from __future__ import annotations

import html
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

SITE = "https://whatpercent.co.kr"
KST = timezone(timedelta(hours=9))
PER_PAGE = 20

router = APIRouter()
_daily_dir: Path | None = None


def configure(daily_dir: Path) -> None:
    global _daily_dir
    _daily_dir = daily_dir


# ---------- 주제 분류 ----------
# 검색 수요가 몰리는 묶음으로 나눈다. 제목+요약의 키워드 적중 수가 가장 많은 주제로 배정.

TOPICS: dict[str, dict] = {
    "wedding": {
        "name": "축의금·결혼식",
        "desc": "축의금 액수, 하객 예절, 청첩장과 예식 비용을 둘러싼 논쟁",
        "kw": ["축의금", "결혼식", "예식", "하객", "청첩장", "신혼", "웨딩", "식대", "결혼 준비", "답례"],
    },
    "workplace": {
        "name": "직장·회사",
        "desc": "상사와 동료, 점심시간과 연차, 회식과 단톡방을 둘러싼 논쟁",
        "kw": ["회사", "직장", "상사", "동료", "팀장", "점심시간", "연차", "퇴근", "출근",
               "회식", "신입", "사무실", "업무", "월급", "야근", "휴가"],
    },
    "love": {
        "name": "연애",
        "desc": "연인 사이의 예민한 선택과 다툼을 둘러싼 논쟁",
        "kw": ["연애", "남친", "여친", "남자친구", "여자친구", "애인", "소개팅", "이별",
               "데이트", "썸", "고백", "전 애인"],
    },
    "family": {
        "name": "가족·명절",
        "desc": "시댁과 처가, 명절과 가사분담, 부모와 형제 사이의 논쟁",
        "kw": ["시댁", "처가", "명절", "부모", "가사", "집안일", "육아", "아이", "형제",
               "시어머니", "장모", "용돈", "상속", "제사", "며느리", "사위"],
    },
    "neighbor": {
        "name": "이웃·아파트",
        "desc": "층간소음과 주차, 관리비와 입주민 시설을 둘러싼 논쟁",
        "kw": ["이웃", "아파트", "입주민", "층간소음", "주차", "관리비", "엘리베이터",
               "커뮤니티 시설", "경비", "빌라", "공동현관", "분리수거"],
    },
    "money": {
        "name": "돈·비용",
        "desc": "더치페이와 계산, 물가와 비용 분담을 둘러싼 논쟁",
        "kw": ["더치페이", "계산", "영수증", "비용", "물가", "배달비", "환불", "할부",
               "회비", "보증금", "대출", "돈 빌려", "n분의 1", "밥값"],
    },
    "online": {
        "name": "온라인·SNS",
        "desc": "커뮤니티와 SNS, 단톡방과 댓글 문화를 둘러싼 논쟁",
        "kw": ["커뮤니티", "SNS", "단톡", "댓글", "인스타", "유튜브", "게시글", "익명",
               "온라인", "박제", "좌표", "DM"],
    },
    "life": {
        "name": "생활·예절",
        "desc": "대중교통과 식당, 반려동물과 공공장소에서의 예절 논쟁",
        "kw": ["지하철", "버스", "택시", "식당", "카페", "반려", "강아지", "고양이",
               "노키즈", "흡연", "줄", "공공장소", "엘베", "민원"],
    },
}
TOPIC_ORDER = list(TOPICS.keys())
ETC = "etc"
ETC_META = {"name": "그 밖의 논쟁", "desc": "어느 묶음에도 들어가지 않는 그날의 화제", "kw": []}


def topic_meta(slug: str) -> dict:
    return TOPICS.get(slug, ETC_META)


def topic_of(item: dict) -> str:
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('side_a', '')} {item.get('side_b', '')}"
    best, best_hits = ETC, 0
    for slug in TOPIC_ORDER:
        hits = sum(1 for k in TOPICS[slug]["kw"] if k in text)
        if hits > best_hits:
            best, best_hits = slug, hits
    return best


# ---------- 데이터 적재 (mtime 기반 캐시) ----------

_cache: dict = {"at": 0.0, "days": [], "items": []}
_CACHE_TTL = 300


def _load() -> tuple[list[dict], list[dict]]:
    """(날짜 목록, 개별 논쟁 목록). 날짜는 최신순."""
    now = time.time()
    if _cache["days"] and now - _cache["at"] < _CACHE_TTL:
        return _cache["days"], _cache["items"]
    days: list[dict] = []
    items: list[dict] = []
    if _daily_dir and _daily_dir.exists():
        for p in sorted(_daily_dir.glob("????-??-??.json"), reverse=True):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            its = doc.get("items") or []
            if not its:
                continue
            days.append({"date": p.stem, "generated_at": doc.get("generated_at", ""), "items": its})
            for i, it in enumerate(its, start=1):
                items.append({"date": p.stem, "idx": i, "item": it, "topic": topic_of(it)})
    _cache.update(at=now, days=days, items=items)
    return days, items


def _day(date: str) -> dict | None:
    days, _ = _load()
    for d in days:
        if d["date"] == date:
            return d
    return None


def _valid_date(s: str) -> bool:
    return len(s) == 10 and s[4] == "-" and s[7] == "-" and s.replace("-", "").isdigit()


# ---------- HTML 틀 ----------

def e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _fmt_date(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt.year}년 {dt.month}월 {dt.day}일"
    except ValueError:
        return d


_CSS = """
:root{--bg:#F4F5F2;--surface:#fff;--ink:#22251F;--ink-soft:#5C6156;--ink-faint:#8B9083;
 --line:#DDE0D8;--brand:#E8503A;--brand-soft:#FDEDEA;--hot:#D8452F;--cold:#2F6FD8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Segoe UI',sans-serif;
 line-height:1.68;-webkit-text-size-adjust:100%}
a{color:inherit}
.wrap{max-width:760px;margin:0 auto;padding:0 18px 56px}
header.site{border-bottom:1px solid var(--line);background:var(--surface)}
header.site .wrap{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px}
.brand{font-weight:800;font-size:1.05rem;text-decoration:none;letter-spacing:-.02em}
.brand span{color:var(--brand)}
nav.site a{display:inline-block;margin-left:14px;font-size:.84rem;color:var(--ink-soft);text-decoration:none}
nav.site a:hover{color:var(--brand)}
h1{font-size:1.5rem;line-height:1.38;margin:26px 0 10px;letter-spacing:-.02em}
h2{font-size:1.1rem;margin:30px 0 10px;letter-spacing:-.01em}
.lede{color:var(--ink-soft);font-size:.95rem;margin:0 0 20px}
.crumb{font-size:.78rem;color:var(--ink-faint);margin:18px 0 0}
.crumb a{text-decoration:none}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px;margin:0 0 12px}
.card h3{margin:0 0 7px;font-size:1.02rem;line-height:1.45}
.card h3 a{text-decoration:none}
.card p{margin:0;color:var(--ink-soft);font-size:.9rem}
.meta{font-size:.76rem;color:var(--ink-faint);margin:0 0 7px}
.pct{display:inline-flex;align-items:baseline;gap:4px;font-weight:800;color:var(--brand);
 font-size:1.02rem;font-variant-numeric:tabular-nums}
.bar{height:8px;border-radius:99px;background:#E7E9E3;overflow:hidden;margin:12px 0 6px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--cold),var(--brand))}
.sides{display:grid;gap:10px;margin:16px 0}
.side{border-left:3px solid var(--line);padding:2px 0 2px 12px;font-size:.92rem}
.side.a{border-color:var(--hot)}
.side.b{border-color:var(--cold)}
.side b{display:block;font-size:.76rem;color:var(--ink-faint);font-weight:700;margin-bottom:2px}
table.groups{width:100%;border-collapse:collapse;margin:14px 0;font-size:.88rem}
table.groups th,table.groups td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
table.groups td:last-child{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 20px}
.chips a{border:1px solid var(--line);background:var(--surface);border-radius:99px;
 padding:6px 13px;font-size:.82rem;text-decoration:none;color:var(--ink-soft)}
.chips a:hover,.chips a.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.cta{display:block;background:var(--brand-soft);border:1px solid #F6CFC7;border-radius:14px;
 padding:18px;margin:26px 0;text-decoration:none}
.cta b{display:block;font-size:1rem;margin-bottom:4px}
.cta span{font-size:.88rem;color:var(--ink-soft)}
.pager{display:flex;justify-content:space-between;gap:10px;margin:24px 0}
.pager a,.pager span{font-size:.86rem;text-decoration:none;color:var(--ink-soft)}
.pager span{color:var(--ink-faint)}
footer.site{border-top:1px solid var(--line);margin-top:40px;padding:22px 0 50px;
 font-size:.78rem;color:var(--ink-faint)}
footer.site a{text-decoration:none;margin-right:12px}
.note{font-size:.78rem;color:var(--ink-faint);margin:18px 0 0}
@media(max-width:560px){h1{font-size:1.28rem}header.site .wrap{flex-direction:column;align-items:flex-start;gap:6px}
 nav.site a{margin:0 14px 0 0}}
"""


def page(*, title: str, desc: str, canonical: str, body: str, jsonld: list[dict] | None = None,
         extra_head: str = "", status: int = 200) -> HTMLResponse:
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps(j, ensure_ascii=False)}</script>'
        for j in (jsonld or []))
    doc = f"""<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(canonical)}">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta name="theme-color" content="#E8503A">
<meta property="og:type" content="article"><meta property="og:site_name" content="몇프로">
<meta property="og:locale" content="ko_KR">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(canonical)}">
<meta property="og:image" content="{SITE}/static/og-image.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="alternate" type="application/rss+xml" title="몇프로 오늘의 논쟁" href="{SITE}/rss.xml">
{extra_head}{blocks}
<style>{_CSS}</style></head><body>
<header class="site"><div class="wrap">
<a class="brand" href="/">몇프로<span>%</span></a>
<nav class="site"><a href="/">공감률 재보기</a><a href="/debates">논쟁 아카이브</a><a href="/topics">주제별</a></nav>
</div></header>
<main class="wrap">{body}</main>
<footer class="site"><div class="wrap">
<a href="/">홈</a><a href="/debates">아카이브</a><a href="/topics">주제별</a>
<a href="/terms">이용약관</a><a href="/privacy">개인정보처리방침</a>
<p>공감률은 AI가 공개된 여론 흐름을 근거로 추정한 값이며 통계 조사 결과가 아닙니다.</p>
</div></footer></body></html>"""
    return HTMLResponse(doc, status_code=status)


def _cta(label: str = "내 사연은 몇 %가 공감할까?") -> str:
    return (f'<a class="cta" href="/"><b>{e(label)}</b>'
            '<span>사연을 입력하면 AI가 맞춤 질문을 만들고 대중 공감률(%)을 예측합니다. 30초면 됩니다.</span></a>')


def _item_card(row: dict, *, show_date: bool = True) -> str:
    it, date, idx = row["item"], row["date"], row["idx"]
    tm = topic_meta(row["topic"])
    meta = " · ".join(x for x in [
        _fmt_date(date) if show_date else "", tm["name"]] if x)
    return (f'<article class="card"><p class="meta">{e(meta)}</p>'
            f'<h3><a href="/debates/{date}/{idx}">{e(it.get("title"))}</a></h3>'
            f'<p>{e(str(it.get("summary", ""))[:150])}…</p>'
            f'<div class="bar"><i style="width:{int(it.get("percent") or 0)}%"></i></div>'
            f'<p class="meta">공감률 <span class="pct">{int(it.get("percent") or 0)}%</span> · {e(it.get("verdict"))}</p>'
            '</article>')


# ---------- 라우트 ----------

@router.get("/debates")
def debates_hub(page_no: int = 1):
    days, items = _load()
    chips = "".join(f'<a href="/topics/{s}">{e(TOPICS[s]["name"])}</a>' for s in TOPIC_ORDER)
    total = max(1, -(-len(days) // 7))
    page_no = max(1, min(page_no, total))
    sel = days[(page_no - 1) * 7: page_no * 7]
    body = [f'<h1>오늘의 논쟁 아카이브</h1>',
            f'<p class="lede">AI가 매일 화제 논쟁 5건을 골라 양측 입장과 대중 공감률(%)을 추정합니다. '
            f'{_fmt_date(days[-1]["date"]) if days else ""}부터 지금까지 {len(items)}건을 모았습니다.</p>',
            f'<div class="chips">{chips}</div>']
    for d in sel:
        body.append(f'<h2><a href="/debates/{d["date"]}" style="text-decoration:none">{_fmt_date(d["date"])} 논쟁</a></h2>')
        for i, it in enumerate(d["items"], start=1):
            body.append(_item_card({"date": d["date"], "idx": i, "item": it, "topic": topic_of(it)},
                                   show_date=False))
    nav = []
    nav.append(f'<a href="/debates?page_no={page_no - 1}">‹ 최근 날짜</a>' if page_no > 1 else '<span>‹ 최근 날짜</span>')
    nav.append(f'<span>{page_no} / {total}</span>')
    nav.append(f'<a href="/debates?page_no={page_no + 1}">이전 날짜 ›</a>' if page_no < total else '<span>이전 날짜 ›</span>')
    body.append(f'<div class="pager">{"".join(nav)}</div>')
    body.append(_cta())
    return page(
        title=f"오늘의 논쟁 아카이브 — 대중 공감률 데이터 {len(items)}건 | 몇프로"
              + (f" ({page_no}페이지)" if page_no > 1 else ""),
        desc=f"축의금·직장·연애·이웃 갈등 등 매일 화제가 된 논쟁 {len(items)}건과 각 사안의 대중 공감률(%) 추정치를 날짜별로 모았습니다.",
        canonical=f"{SITE}/debates" + (f"?page_no={page_no}" if page_no > 1 else ""),
        body="".join(body),
        jsonld=[{"@context": "https://schema.org", "@type": "CollectionPage",
                 "name": "오늘의 논쟁 아카이브", "url": f"{SITE}/debates",
                 "inLanguage": "ko", "isPartOf": {"@type": "WebSite", "name": "몇프로", "url": SITE + "/"}}])


@router.get("/debates/{date}")
def debates_day(date: str):
    if not _valid_date(date):
        return page(title="찾을 수 없는 날짜 | 몇프로", desc="요청한 날짜 형식이 올바르지 않습니다.",
                    canonical=f"{SITE}/debates", body="<h1>찾을 수 없는 날짜입니다</h1>"
                    '<p class="lede"><a href="/debates">아카이브 전체 보기</a></p>',
                    extra_head='<meta name="robots" content="noindex, follow">', status=404)
    d = _day(date)
    if not d:
        return page(title="기록이 없는 날짜 | 몇프로", desc="해당 날짜의 논쟁 기록이 없습니다.",
                    canonical=f"{SITE}/debates", body="<h1>이 날짜의 기록이 없습니다</h1>"
                    '<p class="lede"><a href="/debates">아카이브 전체 보기</a></p>',
                    extra_head='<meta name="robots" content="noindex, follow">', status=404)
    days, _ = _load()
    pos = [x["date"] for x in days].index(date)
    newer = days[pos - 1]["date"] if pos > 0 else None
    older = days[pos + 1]["date"] if pos + 1 < len(days) else None
    titles = [str(it.get("title", "")) for it in d["items"]]
    body = [f'<p class="crumb"><a href="/debates">오늘의 논쟁 아카이브</a></p>',
            f'<h1>{_fmt_date(date)}의 논쟁 {len(d["items"])}건</h1>',
            f'<p class="lede">이날 커뮤니티에서 화제가 된 논쟁과 각 사안에 대한 대중 공감률(%) 추정치입니다.</p>']
    for i, it in enumerate(d["items"], start=1):
        body.append(_item_card({"date": date, "idx": i, "item": it, "topic": topic_of(it)}, show_date=False))
    body.append(_cta())
    nav = []
    nav.append(f'<a href="/debates/{newer}">‹ {_fmt_date(newer)}</a>' if newer else '<span>‹ 최근</span>')
    nav.append('<a href="/debates">아카이브</a>')
    nav.append(f'<a href="/debates/{older}">{_fmt_date(older)} ›</a>' if older else '<span>이전 ›</span>')
    body.append(f'<div class="pager">{"".join(nav)}</div>')
    return page(
        title=f"{_fmt_date(date)} 오늘의 논쟁 — {titles[0][:28]} 외 {len(titles) - 1}건 | 몇프로",
        desc=f"{_fmt_date(date)} 화제 논쟁: " + ", ".join(t[:22] for t in titles[:3]) + f" 등 {len(titles)}건의 대중 공감률 추정치.",
        canonical=f"{SITE}/debates/{date}",
        body="".join(body),
        jsonld=[{"@context": "https://schema.org", "@type": "CollectionPage",
                 "name": f"{_fmt_date(date)}의 논쟁", "url": f"{SITE}/debates/{date}",
                 "datePublished": date, "inLanguage": "ko",
                 "isPartOf": {"@type": "WebSite", "name": "몇프로", "url": SITE + "/"}}])


@router.get("/debates/{date}/{idx}")
def debate_detail(date: str, idx: int):
    d = _day(date) if _valid_date(date) else None
    if not d or idx < 1 or idx > len(d["items"]):
        return page(title="논쟁을 찾을 수 없습니다 | 몇프로", desc="요청한 논쟁 기록이 없습니다.",
                    canonical=f"{SITE}/debates", body="<h1>논쟁을 찾을 수 없습니다</h1>"
                    '<p class="lede"><a href="/debates">아카이브 전체 보기</a></p>',
                    extra_head='<meta name="robots" content="noindex, follow">', status=404)
    it = d["items"][idx - 1]
    slug = topic_of(it)
    tm = topic_meta(slug)
    pct = int(it.get("percent") or 0)
    groups = it.get("groups") or []
    body = [f'<p class="crumb"><a href="/debates">아카이브</a> › <a href="/debates/{date}">{_fmt_date(date)}</a>'
            f' › <a href="/topics/{slug}">{e(tm["name"])}</a></p>',
            f'<h1>{e(it.get("title"))}</h1>',
            f'<p class="meta">{_fmt_date(date)} · 화제 출처: {e(it.get("source"))}</p>',
            f'<div class="bar"><i style="width:{pct}%"></i></div>',
            f'<p>대중 공감률 추정 <span class="pct">{pct}%</span> — {e(it.get("verdict"))}</p>',
            f'<h2>무엇이 쟁점인가</h2><p>{e(it.get("summary"))}</p>',
            '<h2>양측 입장</h2><div class="sides">',
            f'<div class="side a"><b>공감하는 쪽</b>{e(it.get("side_a"))}</div>',
            f'<div class="side b"><b>공감하지 않는 쪽</b>{e(it.get("side_b"))}</div></div>']
    if groups:
        rows = "".join(f'<tr><td>{e(g.get("label"))}</td><td>{int(g.get("percent") or 0)}%</td></tr>'
                       for g in groups)
        body.append('<h2>집단별 예측 공감률</h2>'
                    f'<table class="groups"><thead><tr><th>집단</th><th>공감률</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table>')
    body.append(_cta("이 논쟁, 내 입장은 몇 %가 공감할까?"))

    same_day = [f'<li><a href="/debates/{date}/{i}">{e(x.get("title"))}</a></li>'
                for i, x in enumerate(d["items"], start=1) if i != idx]
    if same_day:
        body.append(f'<h2>{_fmt_date(date)}의 다른 논쟁</h2><ul>{"".join(same_day)}</ul>')
    _, items = _load()
    related = [r for r in items if r["topic"] == slug and not (r["date"] == date and r["idx"] == idx)][:6]
    if related:
        links = "".join(f'<li><a href="/debates/{r["date"]}/{r["idx"]}">{e(r["item"].get("title"))}</a>'
                        f' <span class="meta">{_fmt_date(r["date"])} · {int(r["item"].get("percent") or 0)}%</span></li>'
                        for r in related)
        body.append(f'<h2>{e(tm["name"])} 주제의 다른 논쟁</h2><ul>{links}</ul>'
                    f'<p class="note"><a href="/topics/{slug}">{e(tm["name"])} 논쟁 모두 보기</a> · '
                    f'<a href="/guide/{slug}">{e(tm["name"])} 공감률 데이터 리포트</a></p>')
    desc = f"{str(it.get('summary', ''))[:120]} 대중 공감률 추정 {pct}%."
    return page(
        title=f"{it.get('title')} — 공감률 {pct}% | 몇프로",
        desc=desc,
        canonical=f"{SITE}/debates/{date}/{idx}",
        body="".join(body),
        jsonld=[{"@context": "https://schema.org", "@type": "Article",
                 "headline": str(it.get("title"))[:110], "description": desc,
                 "datePublished": date, "dateModified": date, "inLanguage": "ko",
                 "articleSection": tm["name"],
                 "author": {"@type": "Organization", "name": "몇프로"},
                 "publisher": {"@type": "Organization", "name": "몇프로",
                               "logo": {"@type": "ImageObject", "url": f"{SITE}/static/og-image.png"}},
                 "mainEntityOfPage": {"@type": "WebPage", "@id": f"{SITE}/debates/{date}/{idx}"}}])


@router.get("/topics")
def topics_index():
    _, items = _load()
    body = ['<h1>주제별 논쟁 모음</h1>',
            '<p class="lede">같은 갈등은 반복됩니다. 주제별로 묶어 보면 어떤 쟁점에서 여론이 갈리는지 한눈에 보입니다.</p>']
    for slug in TOPIC_ORDER + [ETC]:
        rows = [r for r in items if r["topic"] == slug]
        if not rows:
            continue
        tm = topic_meta(slug)
        avg = round(sum(int(r["item"].get("percent") or 0) for r in rows) / len(rows))
        body.append(f'<article class="card"><h3><a href="/topics/{slug}">{e(tm["name"])}</a></h3>'
                    f'<p>{e(tm["desc"])}</p>'
                    f'<p class="meta" style="margin-top:8px">논쟁 {len(rows)}건 · 평균 공감률 '
                    f'<span class="pct">{avg}%</span></p></article>')
    body.append(_cta())
    return page(title="주제별 논쟁 모음 — 축의금·직장·연애·이웃 갈등 | 몇프로",
                desc="축의금과 결혼식, 직장과 회식, 연애와 가족, 이웃과 아파트 등 주제별로 모은 논쟁과 대중 공감률 데이터.",
                canonical=f"{SITE}/topics", body="".join(body),
                jsonld=[{"@context": "https://schema.org", "@type": "CollectionPage",
                         "name": "주제별 논쟁 모음", "url": f"{SITE}/topics", "inLanguage": "ko"}])


@router.get("/topics/{slug}")
def topic_page(slug: str, page_no: int = 1):
    _, items = _load()
    rows = [r for r in items if r["topic"] == slug]
    if not rows:
        return page(title="주제를 찾을 수 없습니다 | 몇프로", desc="해당 주제의 논쟁 기록이 없습니다.",
                    canonical=f"{SITE}/topics", body='<h1>주제를 찾을 수 없습니다</h1>'
                    '<p class="lede"><a href="/topics">주제 목록 보기</a></p>',
                    extra_head='<meta name="robots" content="noindex, follow">', status=404)
    tm = topic_meta(slug)
    total = max(1, -(-len(rows) // PER_PAGE))
    page_no = max(1, min(page_no, total))
    sel = rows[(page_no - 1) * PER_PAGE: page_no * PER_PAGE]
    avg = round(sum(int(r["item"].get("percent") or 0) for r in rows) / len(rows))
    chips = "".join(f'<a href="/topics/{s}" class="{"on" if s == slug else ""}">{e(TOPICS[s]["name"])}</a>'
                    for s in TOPIC_ORDER)
    body = [f'<p class="crumb"><a href="/topics">주제별 논쟁</a></p>',
            f'<h1>{e(tm["name"])} 논쟁 {len(rows)}건</h1>',
            f'<p class="lede">{e(tm["desc"])}. 이 주제의 평균 공감률은 <span class="pct">{avg}%</span>입니다. '
            f'<a href="/guide/{slug}">데이터 리포트 보기</a></p>',
            f'<div class="chips">{chips}</div>']
    body += [_item_card(r) for r in sel]
    nav = []
    nav.append(f'<a href="/topics/{slug}?page_no={page_no - 1}">‹ 이전</a>' if page_no > 1 else '<span>‹ 이전</span>')
    nav.append(f'<span>{page_no} / {total}</span>')
    nav.append(f'<a href="/topics/{slug}?page_no={page_no + 1}">다음 ›</a>' if page_no < total else '<span>다음 ›</span>')
    body.append(f'<div class="pager">{"".join(nav)}</div>')
    body.append(_cta())
    return page(title=f"{tm['name']} 논쟁 {len(rows)}건 — 평균 공감률 {avg}% | 몇프로"
                      + (f" ({page_no}페이지)" if page_no > 1 else ""),
                desc=f"{tm['desc']}. 논쟁 {len(rows)}건의 쟁점과 양측 입장, 대중 공감률(%) 추정치를 모았습니다.",
                canonical=f"{SITE}/topics/{slug}" + (f"?page_no={page_no}" if page_no > 1 else ""),
                body="".join(body),
                jsonld=[{"@context": "https://schema.org", "@type": "CollectionPage",
                         "name": f"{tm['name']} 논쟁", "url": f"{SITE}/topics/{slug}", "inLanguage": "ko"}])


@router.get("/guide/{slug}")
def guide_page(slug: str):
    _, items = _load()
    rows = [r for r in items if r["topic"] == slug]
    if not rows or slug not in TOPICS:
        return page(title="리포트를 찾을 수 없습니다 | 몇프로", desc="해당 주제의 데이터가 없습니다.",
                    canonical=f"{SITE}/topics", body='<h1>리포트를 찾을 수 없습니다</h1>'
                    '<p class="lede"><a href="/topics">주제 목록 보기</a></p>',
                    extra_head='<meta name="robots" content="noindex, follow">', status=404)
    tm = TOPICS[slug]
    pcts = [int(r["item"].get("percent") or 0) for r in rows]
    avg = round(sum(pcts) / len(pcts))
    high = sorted(rows, key=lambda r: -int(r["item"].get("percent") or 0))[:5]
    low = sorted(rows, key=lambda r: int(r["item"].get("percent") or 0))[:5]
    split = [r for r in rows if 40 <= int(r["item"].get("percent") or 0) <= 60]
    gmap: dict[str, list[int]] = {}
    for r in rows:
        for g in (r["item"].get("groups") or []):
            gmap.setdefault(str(g.get("label", "")).strip(), []).append(int(g.get("percent") or 0))

    def lst(rs):
        return "".join(f'<li><a href="/debates/{r["date"]}/{r["idx"]}">{e(r["item"].get("title"))}</a> '
                       f'<span class="meta">{int(r["item"].get("percent") or 0)}% · {_fmt_date(r["date"])}</span></li>'
                       for r in rs)

    body = [f'<p class="crumb"><a href="/topics">주제별 논쟁</a> › <a href="/topics/{slug}">{e(tm["name"])}</a></p>',
            f'<h1>{e(tm["name"])} 논쟁, 대중은 몇 %가 공감했나</h1>',
            f'<p class="lede">몇프로가 수집·분석한 {e(tm["name"])} 논쟁 {len(rows)}건의 공감률 데이터입니다. '
            f'평균 공감률은 <span class="pct">{avg}%</span>, 의견이 팽팽하게 갈린 사안은 {len(split)}건이었습니다.</p>',
            '<h2>한눈에 보는 수치</h2>',
            f'<table class="groups"><tbody>'
            f'<tr><td>분석한 논쟁</td><td>{len(rows)}건</td></tr>'
            f'<tr><td>평균 공감률</td><td>{avg}%</td></tr>'
            f'<tr><td>가장 높은 공감률</td><td>{max(pcts)}%</td></tr>'
            f'<tr><td>가장 낮은 공감률</td><td>{min(pcts)}%</td></tr>'
            f'<tr><td>팽팽한 사안(40~60%)</td><td>{len(split)}건</td></tr></tbody></table>',
            f'<h2>공감을 가장 많이 받은 입장</h2><ul>{lst(high)}</ul>',
            f'<h2>공감을 가장 적게 받은 입장</h2><ul>{lst(low)}</ul>']
    if split:
        body.append(f'<h2>여론이 팽팽하게 갈린 논쟁</h2><ul>{lst(split[:5])}</ul>')
    tops = sorted(((k, round(sum(v) / len(v)), len(v)) for k, v in gmap.items() if len(v) >= 2),
                  key=lambda x: -x[1])[:8]
    if tops:
        rows_html = "".join(f'<tr><td>{e(k)}</td><td>{a}%</td></tr>' for k, a, _ in tops)
        body.append('<h2>어떤 집단이 더 공감했나</h2>'
                    '<p>같은 사안을 두고도 세대와 처지에 따라 공감률이 갈립니다. 2건 이상 등장한 집단의 평균입니다.</p>'
                    f'<table class="groups"><thead><tr><th>집단</th><th>평균 공감률</th></tr></thead>'
                    f'<tbody>{rows_html}</tbody></table>')
    body.append(_cta())
    body.append(f'<p class="note"><a href="/topics/{slug}">{e(tm["name"])} 논쟁 {len(rows)}건 모두 보기</a></p>')
    return page(title=f"{tm['name']} 논쟁 데이터 — 평균 공감률 {avg}%, {len(rows)}건 분석 | 몇프로",
                desc=f"{tm['name']} 논쟁 {len(rows)}건을 분석한 공감률 리포트. 평균 {avg}%, 공감이 가장 높았던 입장과 낮았던 입장, 집단별 차이까지.",
                canonical=f"{SITE}/guide/{slug}", body="".join(body),
                jsonld=[{"@context": "https://schema.org", "@type": "Article",
                         "headline": f"{tm['name']} 논쟁, 대중은 몇 %가 공감했나",
                         "description": f"{tm['name']} 논쟁 {len(rows)}건의 공감률 데이터 분석",
                         "inLanguage": "ko", "author": {"@type": "Organization", "name": "몇프로"},
                         "publisher": {"@type": "Organization", "name": "몇프로"},
                         "mainEntityOfPage": {"@type": "WebPage", "@id": f"{SITE}/guide/{slug}"}}])


# ---------- 색인 파일 ----------

@router.get("/robots.txt")
def robots():
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        f"Sitemap: {SITE}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap():
    days, items = _load()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    urls: list[tuple[str, str, str, str]] = [
        (f"{SITE}/", today, "daily", "1.0"),
        (f"{SITE}/debates", today, "daily", "0.9"),
        (f"{SITE}/topics", today, "weekly", "0.8"),
    ]
    for slug in TOPIC_ORDER:
        if any(r["topic"] == slug for r in items):
            urls.append((f"{SITE}/topics/{slug}", today, "weekly", "0.7"))
            urls.append((f"{SITE}/guide/{slug}", today, "weekly", "0.7"))
    for d in days:
        urls.append((f"{SITE}/debates/{d['date']}", d["date"], "monthly", "0.6"))
    for r in items:
        urls.append((f"{SITE}/debates/{r['date']}/{r['idx']}", r["date"], "monthly", "0.6"))
    urls += [(f"{SITE}/terms", today, "yearly", "0.2"), (f"{SITE}/privacy", today, "yearly", "0.2")]
    body = "".join(
        f"<url><loc>{loc}</loc><lastmod>{mod}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{pri}</priority></url>"
        for loc, mod, freq, pri in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>")
    return Response(content=xml, media_type="application/xml")


@router.get("/rss.xml")
def rss():
    """네이버 서치어드바이저 RSS 제출용 — 최신 논쟁 50건."""
    _, items = _load()
    now = datetime.now(KST).strftime("%a, %d %b %Y %H:%M:%S +0900")
    entries = []
    for r in items[:50]:
        it = r["item"]
        try:
            pub = datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=KST).strftime(
                "%a, %d %b %Y 09:00:00 +0900")
        except ValueError:
            pub = now
        link = f"{SITE}/debates/{r['date']}/{r['idx']}"
        entries.append(
            f"<item><title>{e(it.get('title'))}</title><link>{link}</link>"
            f"<guid isPermaLink=\"true\">{link}</guid><pubDate>{pub}</pubDate>"
            f"<category>{e(topic_meta(r['topic'])['name'])}</category>"
            f"<description>{e(str(it.get('summary', ''))[:220])}</description></item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<rss version="2.0"><channel>'
           '<title>몇프로 — 오늘의 논쟁</title>'
           f'<link>{SITE}/debates</link>'
           '<description>AI가 매일 고르는 화제 논쟁과 대중 공감률(%) 추정</description>'
           '<language>ko</language>'
           f'<lastBuildDate>{now}</lastBuildDate>' + "".join(entries) + "</channel></rss>")
    return Response(content=xml, media_type="application/rss+xml")
