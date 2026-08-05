# 몇프로(whatpercent.co.kr) 배포 가이드

FastAPI + Claude API 앱을 Render(호스팅)에 올리고 whatpercent.co.kr 도메인을 연결하는 절차.

## 0. 사전 준비
- GitHub 계정 (코드 저장소)
- Render 계정 (https://render.com — GitHub로 가입 권장)
- Anthropic API 키 (⚠️ 채팅에 노출됐던 키는 폐기하고 **새로 발급한 키**를 사용할 것)

## 1. 코드 저장소
이 폴더(`C:\Users\cha-h\myeongpro-ai`)는 이미 git 저장소로 초기화되어 있음.
GitHub에 비공개(private) 저장소를 만들어 push:

```
git remote add origin https://github.com/<내계정>/whatpercent.git
git push -u origin main
```

`key.txt`는 .gitignore에 있어 절대 올라가지 않음 — 올라갔는지 push 전 `git status`로 확인.

## 2. Render 웹 서비스 생성
1. Render 대시보드 → **New → Web Service** → GitHub 저장소 `whatpercent` 선택
2. Runtime: **Docker** (Dockerfile 자동 인식)
3. Instance Type: Starter($7/월) 권장 — Free는 15분 미사용 시 잠들어 첫 접속이 1분쯤 느림
4. **Environment Variables**에 추가 (본인이 직접 입력):
   - `ANTHROPIC_API_KEY` = 새로 발급한 키
   - (선택) `RATE_PER_HOUR` = IP당 시간당 AI 호출 한도 (기본 12 = 측정 6회)
   - (선택) `DAILY_CAP` = 전체 하루 AI 호출 한도 (기본 400 = 측정 200회, 하루 최대 약 2~4만 원 방어선)
5. Deploy → 완료되면 `https://whatpercent.onrender.com` 같은 임시 주소가 생김 → 접속해 동작 확인

## 3. 도메인 연결 (whatpercent.co.kr)
1. Render 서비스 → **Settings → Custom Domains** → `whatpercent.co.kr` 와 `www.whatpercent.co.kr` 추가
2. Render가 알려주는 DNS 레코드를 도메인 구입처(가비아/후이즈 등) DNS 관리에서 등록:
   - `www` → CNAME → `whatpercent.onrender.com`
   - 루트(`@`) → Render가 안내하는 A 레코드 IP (또는 ALIAS/ANAME 지원 시 CNAME)
3. 전파(수 분~수 시간) 후 Render가 자동으로 HTTPS 인증서(Let's Encrypt)를 발급

## 4. 배포 후 점검
- `https://whatpercent.co.kr/api/health` → `{"ok": true, ...}` 확인
- 측정 1회 실행해 질문 생성·예측 동작 확인
- Anthropic 콘솔에서 사용량/지출 한도(Spend limit) 설정 권장

## 5. 운영 메모
- 코드 수정 후: `git push` 하면 Render가 자동 재배포
- 비용 구조: 호스팅 $7/월 + API 사용량(측정 1회당 약 100~200원, DAILY_CAP으로 상한 방어)
- ⚠️ 공개 전 법적 검토 사항: 화면의 논쟁 카드에는 실제 커뮤니티 게시글·댓글 인용이 포함됨(출처 표기 있음).
  공개 서비스에서의 인용 적법성(저작권·약관)은 운영자 판단으로 유지/제거 결정할 것.
  카드 제거 시 index.html의 해당 `<article class="card">` 블록만 삭제하면 됨.
