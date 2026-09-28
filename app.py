import hmac
import logging
import os
import threading

from flask import Flask, jsonify, Response, request
from monitor import run_monitor

app = Flask(__name__)
_run_lock = threading.Lock()


def _authorized() -> bool:
    """RUN_TOKEN과 일치하는 토큰이 있어야 /run 실행 (미설정 시 거부)."""
    expected = os.environ.get("RUN_TOKEN", "")
    if not expected:
        logging.error("RUN_TOKEN is not set; refusing /run")
        return False
    auth = request.headers.get("Authorization", "")
    given = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-Run-Token", "")
    return hmac.compare_digest(given.encode(), expected.encode())


@app.get("/")
def health():
    return jsonify(status="ok")


@app.route("/run", methods=["GET", "POST"])
def run():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    # 스케줄러 재시도 등으로 동시에 들어오면 중복 발송 방지
    if not _run_lock.acquire(blocking=False):
        return jsonify(error="already running"), 409
    try:
        notify = request.args.get("notify", "1") != "0"
        text = run_monitor(notify=notify)
    finally:
        _run_lock.release()
    return Response(text, status=200, mimetype="text/plain; charset=utf-8")


if __name__ == "__main__":
    # 로컬 개발용. 운영(Docker)은 gunicorn으로 실행
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
