"""측정 결과 공유 (몇프로).

왜 필요한가: 광고로 들어온 4,571명 중 측정까지 간 사람은 75명, 다시 돌아온 사람은 0명이었다.
결과를 남에게 보여줄 방법이 없어서 한 번 보고 끝났다. 이 모듈은 결과마다 주소와 카드 이미지를
만들고, 링크를 받은 사람이 가입 없이 한 번의 탭으로 참여(투표)하게 한다.

  /r/{id}             결과 공유 페이지 — 사연 앞 100자 + AI 예측 + 친구 투표
  /og/{id}.png        공유 카드 이미지 (메신저·SNS 미리보기, 1200x630)
  /api/r/{id}/votes   투표 현황
  /api/r/{id}/vote    투표 (로그인 없음, IP 해시로 1인 1표, 다시 누르면 변경)

공개 범위: 히스토리에 이미 저장되는 '사연 앞 100자 + 결과'만 쓴다. 민감한 내용으로 분류된
측정은 공유 주소를 만들지 않는다(404). 공유 페이지는 검색엔진에 색인시키지 않는다(noindex).
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

import seo

KST = timezone(timedelta(hours=9))
router = APIRouter()

_find_entry: Callable[[str], dict | None] = lambda hid: None  # noqa: E731
_ip_hash: Callable[[Request], str] = lambda request: ""  # noqa: E731
_data_dir: Path | None = None
_votes_lock = threading.Lock()


def configure(*, find_entry, ip_hash, data_dir: Path) -> None:
    global _find_entry, _ip_hash, _data_dir
    _find_entry, _ip_hash, _data_dir = find_entry, ip_hash, data_dir
    (data_dir / "og").mkdir(parents=True, exist_ok=True)


# ---------- 결과 등급 라벨 ----------
# 숫자만으로는 대화 소재가 되지 않는다. 결과에 이름을 붙여 공유할 거리를 만든다.

def tier(pct: int) -> str:
    if pct >= 90:
        return "국민 정서 그 자체"
    if pct >= 70:
        return "다수가 내 편"
    if pct >= 55:
        return "살짝 우세"
    if pct >= 45:
        return "반반 전쟁"
    if pct >= 30:
        return "소수 의견"
    if pct >= 10:
        return "외로운 싸움"
    return "혼자만의 세계"


# ---------- 공개 적합성 ----------
# 새 측정은 모델이 sensitive 값을 준다. 그 전에 쌓인 기록에는 값이 없어 어휘로 걸러낸다.

_SENSITIVE = re.compile(
    "강간|성폭|성추행|성희롱|야동|자위|성기|보지|섹스|성관계|따먹|꼴리|꼴린|몸캠|불법촬영|몰카|"
    "죽이고|죽일|죽여|살인|살의|자살|자해|마약|성매매|아청|갈보|창녀|식민지배|조건만남|원나잇|"
    "거시기|갈색이면")


def is_sensitive(entry: dict) -> bool:
    if "sensitive" in entry:
        return bool(entry.get("sensitive"))
    text = f"{entry.get('story', '')} {entry.get('label', '')}"
    return bool(_SENSITIVE.search(text))


def is_showable(entry: dict) -> bool:
    """홈 피드·히스토리에 내보낼 만한 기록인지 (민감하지 않고, 뜻이 있는 입력)."""
    story = str(entry.get("story") or "").strip()
    if len(story) < 8 or sum(1 for ch in story if "가" <= ch <= "힣") < 5:
        return False
    return not is_sensitive(entry)


# ---------- 친구 투표 ----------

def _votes_file() -> Path:
    return (_data_dir or Path(".")) / "share_votes.jsonl"


def _votes_for(hid: str) -> dict[str, str]:
    """{ip해시: 'a'|'b'} — 같은 사람이 다시 누르면 마지막 선택만 남는다."""
    out: dict[str, str] = {}
    try:
        with _votes_lock:
            f = _votes_file()
            if not f.exists():
                return out
            lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            v = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if v.get("hid") == hid and v.get("side") in ("a", "b"):
            out[str(v.get("ip", ""))] = v["side"]
    return out


def vote_summary(hid: str, ip: str = "") -> dict:
    votes = _votes_for(hid)
    a = sum(1 for s in votes.values() if s == "a")
    total = len(votes)
    return {"a": a, "b": total - a, "total": total,
            "percent": round(a * 100 / total) if total else None,
            "mine": votes.get(ip) if ip else None}


def all_vote_count() -> int:
    try:
        with _votes_lock:
            f = _votes_file()
            return len(f.read_text(encoding="utf-8").splitlines()) if f.exists() else 0
    except OSError:
        return 0


class ShareVoteIn(BaseModel):
    side: str


def _entry(hid: str) -> dict | None:
    if not (4 <= len(hid) <= 20 and hid.isalnum()):
        return None
    it = _find_entry(hid)
    if not it or not it.get("result") or is_sensitive(it):
        return None
    return it


@router.get("/api/r/{hid}/votes")
def share_votes(hid: str, request: Request):
    if not _entry(hid):
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return vote_summary(hid, _ip_hash(request))


@router.post("/api/r/{hid}/vote")
def share_vote(hid: str, body: ShareVoteIn, request: Request):
    if body.side not in ("a", "b"):
        return JSONResponse(status_code=400, content={"error": "bad_side"})
    if not _entry(hid):
        return JSONResponse(status_code=404, content={"error": "not_found"})
    ip = _ip_hash(request)
    try:
        with _votes_lock:
            with _votes_file().open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "hid": hid, "ip": ip, "side": body.side}) + "\n")
    except OSError:
        return JSONResponse(status_code=500, content={"error": "write_failed"})
    return vote_summary(hid, ip)


# ---------- 공유 카드 이미지 ----------

_FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts", r"C:\Windows\Fonts"]
_FONT_NAMES = {
    True: ["NanumGothicBold.ttf", "NanumBarunGothicBold.ttf", "NanumSquareB.ttf", "malgunbd.ttf"],
    False: ["NanumGothic.ttf", "NanumBarunGothic.ttf", "NanumSquareR.ttf", "malgun.ttf"],
}
_font_paths: dict[bool, str | None] = {}


def _font_path(bold: bool) -> str | None:
    if bold in _font_paths:
        return _font_paths[bold]
    found = os.environ.get("OG_FONT_BOLD" if bold else "OG_FONT_REGULAR") or None
    if not (found and Path(found).exists()):
        found = None
        for d in _FONT_DIRS:
            base = Path(d)
            if not base.exists():
                continue
            for name in _FONT_NAMES[bold]:
                hit = next(iter(base.rglob(name)), None)
                if hit:
                    found = str(hit)
                    break
            if found:
                break
    _font_paths[bold] = found
    return found


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=font) > width and ch not in "”’.,!?…)":
            lines.append(cur)
            cur = ch.lstrip()
        else:                                # 닫는 문장부호는 혼자 다음 줄로 넘기지 않는다
            cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def render_card(entry: dict) -> bytes | None:
    """1200x630 PNG. 한글 글꼴을 못 찾으면 None(호출부가 기본 이미지로 돌린다)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    fb, fr = _font_path(True), _font_path(False)
    if not fb or not fr:
        return None

    W, H = 1200, 630
    BG, CARD, INK, SOFT, FAINT = (244, 245, 242), (255, 255, 255), (34, 37, 31), (92, 97, 86), (139, 144, 131)
    HOT, COLD, LINE = (216, 69, 47), (47, 111, 216), (221, 224, 216)
    pct = max(0, min(100, int(entry.get("percent") or 0)))
    color = HOT if pct >= 55 else (COLD if pct < 45 else INK)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([36, 36, W - 36, H - 36], radius=40, fill=CARD, outline=LINE, width=2)

    def font(bold: bool, size: int):
        return ImageFont.truetype(fb if bold else fr, size)

    # 머리줄: 브랜드 + 설명
    f_brand = font(True, 40)
    d.text((84, 74), "몇프로", font=f_brand, fill=INK)
    d.text((84 + d.textlength("몇프로", font=f_brand), 74), "%", font=f_brand, fill=(232, 80, 58))
    f_tag = font(False, 26)
    tag = "AI 대중 공감 온도계"
    d.text((W - 84 - d.textlength(tag, font=f_tag), 84), tag, font=f_tag, fill=FAINT)

    # 사연 (최대 3줄)
    story = str(entry.get("story") or "").strip().replace("\r", "")
    if len(story) >= 100:
        story += "…"
    f_story = font(True, 42)
    lines = _wrap(d, "“" + story + "”", f_story, W - 168, 3)
    y = 150
    for ln in lines:
        d.text((84, y), ln, font=f_story, fill=INK)
        y += 58

    # 공감률 + 등급
    f_pct, f_unit = font(True, 170), font(True, 80)
    py = 322
    num = str(pct)
    d.text((80, py), num, font=f_pct, fill=color)
    nx = 80 + d.textlength(num, font=f_pct)
    d.text((nx + 6, py + 82), "%", font=f_unit, fill=color)
    tx = nx + 6 + d.textlength("%", font=f_unit) + 40
    f_tier, f_ver = font(True, 54), font(False, 32)
    d.text((tx, py + 44), tier(pct), font=f_tier, fill=INK)
    verdict = str(entry.get("verdict") or (entry.get("result") or {}).get("verdict") or "")
    if verdict:
        d.text((tx, py + 116), _wrap(d, verdict, f_ver, W - 84 - int(tx), 1)[0], font=f_ver, fill=SOFT)

    # 온도 막대 (파랑→빨강) + 현재 위치
    bx0, bx1, by, bh = 84, W - 84, 522, 16
    grad = Image.new("RGB", (bx1 - bx0, bh))
    gd = ImageDraw.Draw(grad)
    for x in range(bx1 - bx0):
        t = x / (bx1 - bx0 - 1)
        gd.line([(x, 0), (x, bh)], fill=tuple(int(COLD[i] + (HOT[i] - COLD[i]) * t) for i in range(3)))
    mask = Image.new("L", (bx1 - bx0, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bx1 - bx0 - 1, bh - 1], radius=8, fill=255)
    img.paste(grad, (bx0, by), mask)
    mx = bx0 + (bx1 - bx0) * pct / 100
    d.ellipse([mx - 17, by + bh / 2 - 17, mx + 17, by + bh / 2 + 17], fill=CARD, outline=INK, width=5)

    f_foot = font(False, 24)
    d.text((84, 556), "whatpercent.co.kr", font=font(True, 24), fill=SOFT)
    right = "내 생각은 대중 몇 %가 공감할까?"
    d.text((W - 84 - d.textlength(right, font=f_foot), 556), right, font=f_foot, fill=FAINT)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@router.get("/og/{name}")
def og_image(name: str):
    hid = name[:-4] if name.endswith(".png") else name
    it = _entry(hid)
    if not it or not _data_dir:
        return RedirectResponse("/static/og-image.png", status_code=302)
    cache = _data_dir / "og" / f"{hid}.png"
    try:
        if cache.exists():
            data = cache.read_bytes()
        else:
            data = render_card(it)
            if data:
                cache.write_bytes(data)
    except OSError:
        data = render_card(it)
    if not data:
        return RedirectResponse("/static/og-image.png", status_code=302)
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------- 공유 페이지 ----------

_PAGE_CSS = """
.sh-story{font-size:1.32rem;line-height:1.5;margin:14px 0 8px;font-weight:800;letter-spacing:-.02em}
.sh-score{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:22px 0 2px}
.sh-pct{font-size:3.4rem;font-weight:800;letter-spacing:-.04em;line-height:1;font-variant-numeric:tabular-nums}
.sh-pct small{font-size:1.5rem}
.sh-tier{font-size:1.15rem;font-weight:800}
.sh-verdict{font-size:.88rem;color:var(--ink-soft);margin:4px 0 0}
.sh-vote{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;margin:26px 0}
.sh-vote h2{margin:0 0 4px;font-size:1.08rem}
.sh-vote p{margin:0 0 14px;font-size:.88rem;color:var(--ink-soft)}
.sh-btns{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sh-btns button{border:1.5px solid var(--line);background:var(--bg);border-radius:12px;padding:15px 8px;
 font-size:.98rem;font-weight:700;cursor:pointer;color:var(--ink);font-family:inherit}
.sh-btns button:hover{border-color:var(--ink-faint)}
.sh-btns button.sel-a{background:var(--hot);border-color:var(--hot);color:#fff}
.sh-btns button.sel-b{background:var(--cold);border-color:var(--cold);color:#fff}
.sh-res{margin-top:16px;display:none}
.sh-cmp{display:grid;grid-template-columns:1fr 1fr;gap:10px;text-align:center}
.sh-cmp div{background:var(--bg);border-radius:12px;padding:12px 6px}
.sh-cmp b{display:block;font-size:1.6rem;font-variant-numeric:tabular-nums}
.sh-cmp span{font-size:.76rem;color:var(--ink-faint)}
.sh-gap{font-size:.86rem;color:var(--ink-soft);margin:12px 0 0;text-align:center}
.sh-share{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 0}
.sh-share button{flex:1;min-width:120px;border:1px solid var(--line);background:var(--surface);border-radius:99px;
 padding:10px 12px;font-size:.84rem;cursor:pointer;color:var(--ink-soft);font-family:inherit}
"""


def _fmt_ts(ts: int) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), KST)
        return f"{dt.year}년 {dt.month}월 {dt.day}일"
    except (ValueError, OSError, TypeError):
        return ""


@router.get("/r/{hid}")
def share_page(hid: str):
    it = _entry(hid)
    e = seo.e
    if not it:
        return seo.page(
            title="결과를 찾을 수 없습니다 | 몇프로", desc="공유된 측정 결과가 없거나 공개되지 않는 결과입니다.",
            canonical=f"{seo.SITE}/", status=404, robots="noindex, follow",
            body='<h1>결과를 찾을 수 없습니다</h1><p class="lede">주소가 잘못됐거나 공개되지 않는 결과입니다.</p>'
                 + seo._cta())
    res = it.get("result") or {}
    pct = max(0, min(100, int(it.get("percent") or res.get("percent") or 0)))
    story = str(it.get("story") or "").strip()
    shown = story + ("…" if len(story) >= 100 else "")
    color = "var(--hot)" if pct >= 55 else ("var(--cold)" if pct < 45 else "var(--ink)")
    groups = res.get("groups") or []
    rows = "".join(f'<tr><td>{e(g.get("label"))}</td><td>{int(g.get("percent") or 0)}%</td></tr>' for g in groups)
    body = [
        '<p class="crumb">몇프로 공감률 측정 결과</p>',
        f'<h1 class="sh-story">“{e(shown)}”</h1>',
        f'<p class="meta">{e(it.get("label") or "")}{" · " if it.get("label") else ""}{_fmt_ts(it.get("ts") or 0)} 측정</p>',
        f'<div class="sh-score"><span class="sh-pct" style="color:{color}">{pct}<small>%</small></span>'
        f'<span class="sh-tier">{e(tier(pct))}</span></div>',
        f'<div class="bar"><i style="width:{pct}%"></i></div>',
        f'<p class="sh-verdict">AI가 추정한 대중 공감률 · {e(res.get("verdict") or it.get("verdict") or "")}</p>',
        f'''<section class="sh-vote" id="shVote" data-hid="{e(hid)}" data-ai="{pct}">
<h2>당신 생각은 어때요?</h2>
<p>글쓴이에게 공감하시나요? 누르면 AI 예측과 실제 투표를 비교해 드려요.</p>
<div class="sh-btns"><button type="button" data-side="a">🔥 공감해요</button><button type="button" data-side="b">🧊 공감 안 돼요</button></div>
<div class="sh-res" id="shRes"></div>
<div class="sh-share"><button type="button" id="shShare">📤 다른 친구에게도 물어보기</button><button type="button" id="shCopy">🔗 링크 복사</button></div>
</section>''',
    ]
    if res.get("summary"):
        body.append(f'<h2>AI는 왜 이렇게 봤나</h2><p>{e(res.get("summary"))}</p>')
    if rows:
        body.append('<h2>집단별 예측 공감률</h2><table class="groups"><thead><tr><th>집단</th><th>공감률</th></tr>'
                    f'</thead><tbody>{rows}</tbody></table>')
    body.append('<a class="cta" href="/?from=share" id="shCta"><b>내 고민은 몇 %가 공감할까?</b>'
                '<span>사연을 적으면 AI가 맞춤 질문을 만들고 대중 공감률(%)을 예측합니다. 무료, 가입 없이.</span></a>')
    body.append('<p class="note">이 수치는 여론조사가 아니라 AI 모델의 추정입니다. 투표 결과는 이 링크를 본 사람들의 응답입니다.</p>')
    body.append(_PAGE_JS)
    title = f"“{story[:34]}{'…' if len(story) > 34 else ''}” 공감률 {pct}% — {tier(pct)} | 몇프로"
    return seo.page(
        title=title,
        desc=f"AI가 추정한 대중 공감률 {pct}%. 당신은 공감하시나요? 투표하고 AI 예측과 비교해 보세요.",
        canonical=f"{seo.SITE}/r/{hid}", body="".join(body), robots="noindex, follow",
        og_image=f"{seo.SITE}/og/{hid}.png", extra_head=f"<style>{_PAGE_CSS}</style>")


_PAGE_JS = """<script>
(function(){
  var box=document.getElementById('shVote'); if(!box) return;
  var hid=box.dataset.hid, ai=+box.dataset.ai, res=document.getElementById('shRes');
  var vid='anon'; try{ vid=localStorage.getItem('mp_vid'); if(!vid){ vid=Math.random().toString(36).slice(2)+Date.now().toString(36); localStorage.setItem('mp_vid',vid);} }catch(e){}
  var sid=Math.random().toString(36).slice(2), t0=Date.now();
  function track(ev, meta){
    try{ fetch('/api/track',{method:'POST',headers:{'Content-Type':'application/json'},keepalive:true,
      body:JSON.stringify({vid:vid,sid:sid,event:ev,meta:meta||hid,ref:document.referrer||'',dur:Math.round((Date.now()-t0)/1000)})}).catch(function(){}); }catch(e){}
  }
  track('share_view');
  function render(d){
    box.querySelectorAll('.sh-btns button').forEach(function(b){
      b.classList.remove('sel-a','sel-b');
      if(d.mine && b.dataset.side===d.mine) b.classList.add('sel-'+d.mine);
    });
    if(!d.mine){ res.style.display='none'; return; }
    var real=d.percent, gap=Math.abs(real-ai);
    res.innerHTML='<div class="sh-cmp"><div><b>'+ai+'%</b><span>AI 예측</span></div>'
      +'<div><b>'+real+'%</b><span>실제 투표 ('+d.total+'명)</span></div></div>'
      +'<p class="sh-gap">'+(d.total<5?'아직 표가 적어요. 친구들에게 물어보면 더 정확해져요.'
        :(gap<=10?'AI 예측이 거의 맞았네요 (차이 '+gap+'%p).':'AI 예측과 '+gap+'%p 차이가 나요.'))+'</p>';
    res.style.display='block';
  }
  fetch('/api/r/'+hid+'/votes').then(function(r){return r.json();}).then(render).catch(function(){});
  box.querySelectorAll('.sh-btns button').forEach(function(b){
    b.addEventListener('click', function(){
      fetch('/api/r/'+hid+'/vote',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({side:b.dataset.side})}).then(function(r){return r.json();}).then(function(d){
          render(d); track('share_vote');
        }).catch(function(){});
    });
  });
  var url=location.origin+'/r/'+hid;
  function copy(btn){
    var done=function(){ var o=btn.textContent; btn.textContent='복사했어요 ✓'; setTimeout(function(){btn.textContent=o;},1600); };
    var legacy=function(){
      try{ var ta=document.createElement('textarea'); ta.value=url; ta.style.position='fixed'; ta.style.opacity='0';
        document.body.appendChild(ta); ta.select(); var ok=document.execCommand('copy'); document.body.removeChild(ta);
        if(ok){ done(); return; } }catch(e){}
      btn.textContent=url;
    };
    if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(url).then(done).catch(legacy); }
    else { legacy(); }
  }
  document.getElementById('shCopy').addEventListener('click', function(){ copy(this); track('share','copy_from_page'); });
  document.getElementById('shShare').addEventListener('click', function(){
    var self=this;
    if(navigator.share){ navigator.share({title:document.title, text:'이 사연, 너는 공감해?', url:url}).then(function(){ track('share','native_from_page'); }).catch(function(){}); }
    else { copy(self); track('share','copy_from_page'); }
  });
  var cta=document.getElementById('shCta'); if(cta) cta.addEventListener('click', function(){ track('share_cta'); });
})();
</script>"""
