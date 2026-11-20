FROM python:3.12-slim
WORKDIR /app
# 공유 카드 이미지(/og/*.png)에 한글을 그리려면 글꼴이 필요하다
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 새 .py 파일을 추가하면 이 줄에도 넣어야 한다 (빠지면 import 실패로 배포가 실패한다)
COPY server.py seo.py share.py index.html admin.html terms.html privacy.html favicon.ico ./
COPY static ./static
# Render/Railway 등은 PORT 환경변수를 주입한다
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
