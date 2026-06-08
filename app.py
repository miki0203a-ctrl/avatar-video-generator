"""
Avatar Video Generator - Web App (Phase 1)
Flask + HeyGen + ElevenLabs
"""

from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session, send_file, flash)
from functools import wraps
import sqlite3, os, json, time, threading, uuid, urllib.request, urllib.error
import urllib.parse
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production-!@#$")

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(BASE_DIR, "data.db")
UPLOAD_DIR  = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_CHARS   = 3000

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── プラン定義 ────────────────────────────────────────────────
PLANS = {
    "light":    {"name": "ライト",       "minutes": 10,  "price": 980},
    "standard": {"name": "スタンダード", "minutes": 30,  "price": 2980},
    "heavy":    {"name": "ヘビー",       "minutes": 150, "price": 12800},
    "admin":    {"name": "管理者",       "minutes": 9999,"price": 0},
}

# ── DB初期化 ──────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT UNIQUE NOT NULL,
            password    TEXT NOT NULL,
            plan        TEXT NOT NULL DEFAULT 'light',
            is_admin    INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            note        TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS usage_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            seconds     REAL NOT NULL,
            video_id    TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id          TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            video_url   TEXT,
            error       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # 管理者が居なければ作成
        admin = db.execute("SELECT id FROM users WHERE is_admin=1").fetchone()
        if not admin:
            db.execute(
                "INSERT INTO users (username,password,plan,is_admin,active) VALUES (?,?,?,1,1)",
                ("admin", generate_password_hash("admin1234"), "admin")
            )
            db.commit()
            print("[INIT] 管理者アカウント作成: admin / admin1234  ← 必ず変更してください")

init_db()


# ── ヘルパー ──────────────────────────────────────────────────
def get_setting(key, default=""):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
        db.commit()

def get_used_minutes(user_id):
    """今月の使用分数を返す"""
    month_start = datetime.now().strftime("%Y-%m-01")
    with get_db() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(seconds),0) as total FROM usage_log "
            "WHERE user_id=? AND created_at >= ?",
            (user_id, month_start)
        ).fetchone()
    return row["total"] / 60.0

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session or not session.get("is_admin"):
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if "user_id" not in session:
        return None
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()


# ── API関数（main.pyから流用） ────────────────────────────────
def call_elevenlabs(api_key, voice_id, text):
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
           "?output_format=mp3_44100_64")
    payload = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
          headers={"Accept": "audio/mpeg",
                   "Content-Type": "application/json",
                   "xi-api-key": api_key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def upload_audio_public(mp3_bytes):
    boundary = "CatboxUpload1234"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="reqtype"\r\n\r\n'
        f"fileupload\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="fileToUpload"; filename="audio.mp3"\r\n'
        f"Content-Type: audio/mpeg\r\n\r\n"
    ).encode() + mp3_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://catbox.moe/user/api.php",
        data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        url = r.read().decode().strip()
        if not url.startswith("https://"):
            raise RuntimeError(f"catbox.moe error: {url}")
        return url

def heygen_create_video(api_key, avatar_id, audio_url):
    payload = json.dumps({
        "video_inputs": [{
            "character": {"type": "avatar", "avatar_id": avatar_id, "avatar_style": "normal"},
            "voice": {"type": "audio", "audio_url": audio_url},
        }],
        "dimension": {"width": 720, "height": 1280},
        "aspect_ratio": "9:16",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.heygen.com/v2/video/generate",
        data=payload, method="POST",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
        return resp["data"]["video_id"]

def heygen_poll_video(api_key, video_id, timeout=300, interval=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(
            f"https://api.heygen.com/v1/video_status.get?video_id={video_id}",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        status = data.get("data", {}).get("status", "")
        if status == "completed":
            return data["data"]["video_url"], data["data"].get("duration", 0)
        if status == "failed":
            raise RuntimeError(f"HeyGen生成失敗: {data}")
        time.sleep(interval)
    raise RuntimeError("HeyGen: タイムアウト（5分）")

def heygen_list_avatars(api_key):
    req = urllib.request.Request(
        "https://api.heygen.com/v2/avatars",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
        return resp.get("data", {}).get("avatars", [])


# ── バックグラウンドワーカー ───────────────────────────────────
def video_worker(job_id, user_id, text, avatar_path):
    el_key    = get_setting("elevenlabs_api_key")
    el_voice  = get_setting("elevenlabs_voice_id")
    hg_key    = get_setting("heygen_api_key")
    hg_avatar = get_setting("heygen_avatar_id")

    # ユーザー固有アバターが設定されていれば優先
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    user_avatar = None
    if user and user["note"]:  # noteフィールドにアバターIDを保存
        try:
            info = json.loads(user["note"])
            user_avatar = info.get("avatar_id") or hg_avatar
        except Exception:
            user_avatar = hg_avatar
    else:
        user_avatar = hg_avatar

    def update_status(status, **kwargs):
        with get_db() as db:
            sets = ["status=?"]
            vals = [status]
            for k, v in kwargs.items():
                sets.append(f"{k}=?")
                vals.append(v)
            vals.append(job_id)
            db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)
            db.commit()

    try:
        update_status("generating_audio")
        mp3 = call_elevenlabs(el_key, el_voice, text)

        update_status("uploading_audio")
        audio_url = upload_audio_public(mp3)

        update_status("generating_video")
        video_id = heygen_create_video(hg_key, user_avatar, audio_url)

        update_status("waiting")
        video_url, duration_sec = heygen_poll_video(hg_key, video_id)

        # 使用量を記録
        with get_db() as db:
            db.execute(
                "INSERT INTO usage_log (user_id, seconds, video_id) VALUES (?,?,?)",
                (user_id, duration_sec or len(text) * 0.06, video_id)
            )
            db.commit()

        update_status("completed", video_url=video_url)

    except Exception as e:
        update_status("failed", error=str(e))


# ── ルート ────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        with get_db() as db:
            user = db.execute("SELECT * FROM users WHERE username=? AND active=1",
                              (username,)).fetchone()
        if user and check_password_hash(user["password"], password):
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            session["plan"]     = user["plan"]
            return redirect(url_for("dashboard"))
        flash("ユーザー名またはパスワードが違います")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    plan_info = PLANS.get(user["plan"], PLANS["light"])
    used_min  = get_used_minutes(user["id"])
    limit_min = plan_info["minutes"]
    remaining = max(0, limit_min - used_min)

    # 最近のジョブ
    with get_db() as db:
        jobs = db.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
            (user["id"],)
        ).fetchall()

    # ユーザーのアバターID
    user_avatar_id = get_setting("heygen_avatar_id")
    try:
        info = json.loads(user["note"] or "{}")
        user_avatar_id = info.get("avatar_id") or user_avatar_id
    except Exception:
        pass

    return render_template("dashboard.html",
        user=user, plan=plan_info,
        used_min=round(used_min, 1),
        limit_min=limit_min,
        remaining=round(remaining, 1),
        percent=min(100, int(used_min / max(limit_min,1) * 100)),
        jobs=jobs,
        max_chars=MAX_CHARS,
        user_avatar_id=user_avatar_id,
    )

@app.route("/generate", methods=["POST"])
@login_required
def generate():
    user = get_current_user()
    plan_info = PLANS.get(user["plan"], PLANS["light"])
    used_min  = get_used_minutes(user["id"])
    remaining = plan_info["minutes"] - used_min

    if remaining <= 0:
        return jsonify({"error": "今月の使用上限に達しています。プランのアップグレードをご検討ください。"}), 403

    text = request.form.get("text","").strip()
    if not text:
        return jsonify({"error": "テキストを入力してください"}), 400
    if len(text) > MAX_CHARS:
        return jsonify({"error": f"文字数が上限（{MAX_CHARS}文字）を超えています"}), 400

    # API設定チェック
    if not get_setting("elevenlabs_api_key") or not get_setting("heygen_api_key"):
        return jsonify({"error": "管理者がAPIキーを設定していません"}), 500

    job_id = str(uuid.uuid4())
    with get_db() as db:
        db.execute(
            "INSERT INTO jobs (id, user_id, status) VALUES (?,?,?)",
            (job_id, user["id"], "pending")
        )
        db.commit()

    threading.Thread(
        target=video_worker,
        args=(job_id, user["id"], text, None),
        daemon=True
    ).start()

    return jsonify({"job_id": job_id})

@app.route("/job/<job_id>")
@login_required
def job_status(job_id):
    with get_db() as db:
        job = db.execute(
            "SELECT * FROM jobs WHERE id=? AND user_id=?",
            (job_id, session["user_id"])
        ).fetchone()
    if not job:
        return jsonify({"error": "not found"}), 404

    STATUS_MSG = {
        "pending":          "準備中...",
        "generating_audio": "① 音声を生成中...",
        "uploading_audio":  "② 音声をアップロード中...",
        "generating_video": "③ 動画を生成中...",
        "waiting":          "④ 動画の完成を待っています...",
        "completed":        "✅ 完成！",
        "failed":           "❌ エラーが発生しました",
    }

    return jsonify({
        "status":   job["status"],
        "message":  STATUS_MSG.get(job["status"], job["status"]),
        "video_url": job["video_url"],
        "error":    job["error"],
    })

@app.route("/avatar/upload", methods=["POST"])
@login_required
def avatar_upload():
    """ユーザーがアバター画像をアップロード（将来的にHeyGenカスタムアバター用）"""
    if "file" not in request.files:
        return jsonify({"error": "ファイルがありません"}), 400
    f = request.files["file"]
    if not f.filename or not allowed_file(f.filename):
        return jsonify({"error": "PNG/JPG/WEBPのみ対応しています"}), 400
    filename = secure_filename(f"{session['user_id']}_{int(time.time())}_{f.filename}")
    save_path = os.path.join(UPLOAD_DIR, filename)
    f.save(save_path)
    return jsonify({"message": "アップロード完了", "filename": filename})


# ── 管理者画面 ─────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    with get_db() as db:
        users = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    settings = {
        "elevenlabs_api_key": get_setting("elevenlabs_api_key"),
        "elevenlabs_voice_id": get_setting("elevenlabs_voice_id"),
        "heygen_api_key": get_setting("heygen_api_key"),
        "heygen_avatar_id": get_setting("heygen_avatar_id"),
    }
    return render_template("admin.html", users=users, settings=settings, plans=PLANS)

@app.route("/admin/user/add", methods=["POST"])
@admin_required
def admin_add_user():
    username = request.form.get("username","").strip()
    password = request.form.get("password","").strip()
    plan     = request.form.get("plan","light")
    if not username or not password:
        flash("ユーザー名とパスワードは必須です")
        return redirect(url_for("admin"))
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO users (username,password,plan) VALUES (?,?,?)",
                (username, generate_password_hash(password), plan)
            )
            db.commit()
        flash(f"ユーザー「{username}」を追加しました")
    except sqlite3.IntegrityError:
        flash("そのユーザー名は既に使われています")
    return redirect(url_for("admin"))

@app.route("/admin/user/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    with get_db() as db:
        db.execute("UPDATE users SET active = 1-active WHERE id=?", (user_id,))
        db.commit()
    return redirect(url_for("admin"))

@app.route("/admin/user/<int:user_id>/plan", methods=["POST"])
@admin_required
def admin_change_plan(user_id):
    plan = request.form.get("plan","light")
    with get_db() as db:
        db.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
        db.commit()
    return redirect(url_for("admin"))

@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_save_settings():
    for key in ["elevenlabs_api_key","elevenlabs_voice_id","heygen_api_key","heygen_avatar_id"]:
        val = request.form.get(key,"").strip()
        if val:
            set_setting(key, val)
    flash("設定を保存しました")
    return redirect(url_for("admin"))

@app.route("/admin/usage")
@admin_required
def admin_usage():
    with get_db() as db:
        rows = db.execute("""
            SELECT u.username, u.plan,
                   COALESCE(SUM(l.seconds),0)/60.0 as used_min,
                   COUNT(l.id) as count
            FROM users u
            LEFT JOIN usage_log l ON l.user_id=u.id
                AND l.created_at >= date('now','start of month')
            GROUP BY u.id
            ORDER BY used_min DESC
        """).fetchall()
    return render_template("usage.html", rows=rows, plans=PLANS)


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=8080)
