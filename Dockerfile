FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY app.py monitor.py ./
ENV PORT=8080
EXPOSE 8080
# worker 1개(중복 실행 방지 락이 프로세스 단위), 리포트 생성이 길어서 gunicorn 타임아웃 해제.
# 요청 제한시간은 Cloud Run 쪽 --timeout 으로 관리.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 0 app:app
