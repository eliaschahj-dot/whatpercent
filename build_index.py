# -*- coding: utf-8 -*-
"""scratchpad의 데모 HTML을 복사해 '실제 AI' 버전 index.html을 생성."""
from pathlib import Path

SRC = Path(r"C:\Users\cha-h\AppData\Local\Temp\claude\C--Users-cha-h\d7adb533-d594-4c59-a46a-b0ab3b7d1e27\scratchpad\empathy-thermometer-demo.html")
DST = Path(__file__).parent / "index.html"

html = SRC.read_text(encoding="utf-8")

# 1) 헤더 라벨
html = html.replace("대중 공감 온도계 · 데모 v2", "대중 공감 온도계 · 실제 AI")
HEAD = """<!doctype html><html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>몇프로 — 내 생각, 대중은 몇 %가 공감할까? | AI 대중 공감 온도계</title>
<meta name="description" content="사연이나 의견을 입력하면 AI가 맞춤 질문을 만들고 대중 공감률(%)을 예측합니다. 축의금·가사분담·직장·연애·이웃 갈등, 내 편은 몇 %일까?">
<link rel="canonical" href="https://whatpercent.co.kr/">
<meta name="theme-color" content="#E8503A">
<meta property="og:type" content="website">
<meta property="og:site_name" content="몇프로">
<meta property="og:locale" content="ko_KR">
<meta property="og:title" content="몇프로 — 내 생각, 대중은 몇 %가 공감할까?">
<meta property="og:description" content="사연을 입력하면 AI가 맞춤 질문을 만들고 대중 공감률을 예측하는 공감 온도계.">
<meta property="og:url" content="https://whatpercent.co.kr/">
<meta property="og:image" content="https://whatpercent.co.kr/static/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebApplication","name":"몇프로",
 "alternateName":"whatpercent","url":"https://whatpercent.co.kr/",
 "description":"AI가 사연을 읽고 대중 공감률을 예측하는 공감 온도계",
 "applicationCategory":"LifestyleApplication","inLanguage":"ko",
 "offers":{"@type":"Offer","price":"0","priceCurrency":"KRW"}}
</script>
</head><body>"""
html = html.replace("<title>몇프로 — 대중 공감 온도계 (데모)</title>", HEAD)
html = html.rstrip() + "\n</body></html>\n"

# 1.5) 헤더에 로고 마크 삽입
html = html.replace(
    '<div class="logo"><b>몇프로',
    '<div class="logo"><img src="/static/logo-mark.svg" alt="몇프로 로고" '
    'style="height:38px;width:38px;align-self:center;flex:none"><b>몇프로'
)

# 2) askbox에 상태 배너 + AI 표기 추가
html = html.replace(
    '<label for="askInput">내 생각 온도 재보기</label>',
    '<label for="askInput">내 생각 온도 재보기 <span class="demo-chip">실제 AI · claude-opus-5</span></label>\n'
    '  <div id="healthBanner" class="trust-flag" style="display:none; margin-top:10px"></div>'
)

# 3) 온도 재보기 JS 전체 교체
start_marker = "/* ================= 온도 재보기: 검색 → 추가 질문 → 예측 ================= */"
end_marker = "/* ================= 카드 투표 ================= */"
i = html.index(start_marker)
j = html.index(end_marker)

new_js = r"""/* ================= 온도 재보기 (실제 AI 백엔드) ================= */
var askBtn=document.getElementById('askBtn'), askInput=document.getElementById('askInput'),
    steps=document.getElementById('askSteps'), quiz=document.getElementById('askQuiz'),
    quizIntro=document.getElementById('quizIntro'), quizBlocks=document.getElementById('quizBlocks'),
    quizGo=document.getElementById('quizGo'), askRes=document.getElementById('askResult'),
    healthBanner=document.getElementById('healthBanner');
var curStory='', curQs=[], answers=[];

fetch('/api/health').then(function(r){return r.json();}).then(function(h){
  if(!h.ok){
    healthBanner.style.display='block';
    healthBanner.innerHTML='<b>AI 연결 안 됨</b> — '+esc(h.message||'자격증명을 확인하세요.');
    askBtn.disabled=true;
  }
}).catch(function(){ /* 서버 미기동 등 — 입력 시 오류로 안내 */ });

function resetAsk(){
  steps.style.display='none'; steps.innerHTML='';
  quiz.style.display='none'; quizBlocks.innerHTML=''; quizGo.style.display='none';
  askRes.style.display='none'; askRes.innerHTML='';
  askBtn.disabled=false;
}
function showSteps(lines){
  steps.style.display='block';
  steps.innerHTML=lines.map(function(l){return '<div>'+l+'</div>';}).join('');
  var rows=steps.querySelectorAll('div');
  var reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var d=reduced?0:500;
  rows.forEach(function(r,i){ setTimeout(function(){r.classList.add('on');}, d*(i+1)); });
}
function showError(msg){
  steps.style.display='none';
  askRes.innerHTML='<div class="trust-flag"><b>오류</b> — '+esc(msg)+'</div>'
    +'<button class="again" id="askAgain">다시 시도</button>';
  askRes.style.display='block'; askBtn.disabled=false;
  document.getElementById('askAgain').addEventListener('click', function(){ resetAsk(); runAsk(); });
}

function runAsk(){
  if(askBtn.disabled) return;
  resetAsk(); askBtn.disabled=true;
  curStory=askInput.value.trim()||askInput.placeholder.replace(/^예: /,'');
  showSteps(['① 사연을 AI(claude-opus-5)에게 전송… 사연을 읽고 있습니다',
             '② 유사 논쟁에서 여론이 갈린 조건을 분석 중… (30초 안팎 소요)']);
  fetch('/api/questions',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({story:curStory})})
  .then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
  .then(function(res){
    if(!res.ok){ showError(res.d.message||'요청 실패'); return; }
    var d=res.d;
    curQs=d.questions||[];
    answers=new Array(curQs.length).fill(null);
    steps.style.display='none';
    quizIntro.innerHTML='주제군 <b>'+esc(d.topic_label||'')+'</b> — '+esc(d.analysis||'')
      +'<br>AI가 이 사연의 여론을 가를 조건 <b>'+curQs.length+'가지</b>를 생성했습니다.';
    quizBlocks.innerHTML=curQs.map(function(q,qi){
      return '<div class="qblock"><div class="qt"><span class="qnum">Q'+(qi+1)+'.</span>'+esc(q.text)+'</div>'
        +'<div class="chips">'+q.options.map(function(o,oi){
           return '<button data-q="'+qi+'" data-o="'+oi+'">'+esc(o)+'</button>';
         }).join('')+'</div></div>';
    }).join('');
    quiz.style.display='block';
    quizBlocks.querySelectorAll('button').forEach(function(b){
      b.addEventListener('click', function(){
        var qi=+b.dataset.q;
        quizBlocks.querySelectorAll('button[data-q="'+qi+'"]').forEach(function(x){x.classList.remove('sel');});
        b.classList.add('sel'); answers[qi]=+b.dataset.o;
        if(answers.every(function(a){return a!==null;})) quizGo.style.display='block';
      });
    });
    quiz.scrollIntoView({behavior:'smooth', block:'nearest'});
  })
  .catch(function(){ showError('서버에 연결할 수 없습니다. 서버가 실행 중인지 확인하세요.'); });
}

quizGo.addEventListener('click', function(){
  var qa=curQs.map(function(q,qi){ return {question:q.text, answer:q.options[answers[qi]]}; });
  quiz.style.display='none';
  showSteps(['③ 답변 반영 — AI가 커뮤니티 여론을 종합해 예측을 계산 중… (30초~1분 소요)']);
  fetch('/api/predict',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({story:curStory, answers:qa})})
  .then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
  .then(function(res){
    if(!res.ok){ showError(res.d.message||'예측 실패'); return; }
    var d=res.d;
    var pct=Math.max(0,Math.min(100,d.percent|0));
    var vc = pct>=55?'var(--hot)':(pct>=45?'var(--ink-soft)':'var(--cold)');
    var h='<span class="demo-chip">실제 AI 응답 · claude-opus-5</span>'
     +'<div class="gauge"><div class="gauge-top"><span class="big">'+pct+'<small>%</small></span>'
     +'<span class="verdict" style="color:'+vc+'">'+esc(d.verdict||'')+'</span></div>'
     +'<div class="track"><span class="marker" style="left:'+pct+'%"></span></div>'
     +'<div class="track-cap"><span>0% 냉담</span><span>100% 뜨거운 공감</span></div></div>'
     +'<p class="basis">'+esc(d.summary||'')+'</p>'
     +'<ul class="cond-list">'+(d.factors||[]).map(function(f){
        var imp=String(f.impact||'');
        var cls=imp.indexOf('+')===0?'d-up':(imp.indexOf('-')===0?'d-dn':'d-zero');
        return '<li><span>'+esc(f.label)+'</span><span class="'+cls+'">'+esc(imp)+'</span></li>';
      }).join('')+'</ul>'
     +'<div class="demo-grid" style="margin-top:14px"><h4>집단별 예측</h4>'
     +(d.groups||[]).map(function(g){
        var gp=Math.max(0,Math.min(100,g.percent|0));
        return '<div class="bar-row"><span class="lbl">'+esc(g.label)+'</span>'
          +'<div class="bar"><i class="'+(gp<50?'cool':'')+'" style="width:'+gp+'%"></i></div>'
          +'<span class="val">'+gp+'%</span></div>';
      }).join('')+'</div>'
     +(d.tip?'<div class="ai-note"><b>AI 조언</b> — '+esc(d.tip)+'</div>':'')
     +'<p class="basis">'+esc(d.caveat||'이 수치는 실측 여론조사가 아닌 AI 모델의 추정입니다.')+'</p>'
     +'<button class="again" id="askAgain">다른 사연으로 다시 재보기</button>';
    steps.style.display='none';
    askRes.innerHTML=h; askRes.style.display='block'; askBtn.disabled=false;
    document.getElementById('askAgain').addEventListener('click', function(){ resetAsk(); askInput.focus(); });
  })
  .catch(function(){ showError('서버에 연결할 수 없습니다.'); });
});
askBtn.addEventListener('click', runAsk);
askInput.addEventListener('keydown', function(e){ if(e.key==='Enter') runAsk(); });

"""

html = html[:i] + new_js + html[j:]
DST.write_text(html, encoding="utf-8")
print("written", DST, len(html), "chars")
