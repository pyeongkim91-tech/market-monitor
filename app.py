from flask import Flask, jsonify, Response
from monitor import run_monitor

app = Flask(__name__)

@app.get("/")
def health():
    return jsonify(status="ok")

@app.get("/run")
def run():
    text = run_monitor()
    return Response(text, status=200, mimetype="text/plain; charset=utf-8")

if __name__ == "__main__":
    # Cloud Run은 gunicorn 필요 없음(단일 프로세스) — 플라스크 내장 서버 사용
    app.run(host="0.0.0.0", port=8080)
