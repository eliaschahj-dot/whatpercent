FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py index.html admin.html terms.html privacy.html favicon.ico ./
COPY static ./static
# Render/Railway 등은 PORT 환경변수를 주입한다
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
