FROM python:3.11-slim

# OS 패키지 (lxml/bs4 속도/빌드 안정)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc g++ libxml2 libxslt1.1 libxslt1-dev libxml2-dev tzdata \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    TZ=Asia/Seoul

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app.py monitor.py ./
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
