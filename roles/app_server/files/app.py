"""Demo app deployed by linux-server-setup-kit.

GET /        -> page showing the environment and a visit counter stored in MariaDB
GET /health  -> JSON health check (app + database)
"""
import os

import pymysql
from flask import Flask, jsonify

app = Flask(__name__)


def db():
    return pymysql.connect(
        host="127.0.0.1",
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        autocommit=True,
        connect_timeout=3,
    )


def init():
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS visits ("
            " id INT AUTO_INCREMENT PRIMARY KEY,"
            " at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )


@app.route("/")
def index():
    init()
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO visits () VALUES ()")
        cur.execute("SELECT COUNT(*) FROM visits")
        count = cur.fetchone()[0]
    env = os.environ.get("APP_ENV", "production")
    return (
        "<h1>It works!</h1>"
        f"<p>Environment: <b>{env}</b></p>"
        f"<p>Visits stored in MariaDB: <b>{count}</b></p>"
        "<p>Deployed with linux-server-setup-kit</p>"
    )


@app.route("/health")
def health():
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify(status="ok", database="ok", env=os.environ.get("APP_ENV")), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify(status="error", database=str(exc)), 503
