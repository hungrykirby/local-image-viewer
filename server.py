"""
iPad/Android画像ビュアー サーバー v4
使い方:
  1. pip install -r requirements.txt
  2. .env.example を .env にコピーし、VIEWER_UPLOAD_TOKEN を設定（Nightdropから受信する場合）
  3. python server.py
  4. ブラウザで http://<PCのIPアドレス>:5000 を開く
"""

import hmac
import json
import os
import threading

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from receiver import STATUS_HTTP, Receiver, UploadError, is_within

APP_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================================================
#  設定（.env → 環境変数。環境変数が優先）
# ============================================================

def load_dotenv(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resolve_dir(value):
    return os.path.abspath(os.path.join(APP_DIR, value))


load_dotenv(os.path.join(APP_DIR, ".env"))

HOST              = os.environ.get("VIEWER_HOST", "0.0.0.0")
PORT              = int(os.environ.get("VIEWER_PORT", "5000"))
UPLOAD_TOKEN      = os.environ.get("VIEWER_UPLOAD_TOKEN", "")
LIBRARY_DIR       = resolve_dir(os.environ.get("VIEWER_LIBRARY_DIR", "library"))
DATA_DIR          = resolve_dir(os.environ.get("VIEWER_DATA_DIR", "data"))
MAX_UPLOAD_BYTES  = int(os.environ.get("VIEWER_MAX_UPLOAD_MB", "50")) * 1024 * 1024
FORM_OVERHEAD_BYTES = 64 * 1024  # バッチ名・ハッシュ値などファイル以外の送信内容の分

FOLDERS_JSON = os.path.join(APP_DIR, "folders.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
LIBRARY_LABEL = "受信ライブラリ（Nightdrop）"

# プロジェクト全体を公開せず、必要なファイルだけを明示的に配信する。
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + FORM_OVERHEAD_BYTES

receiver = Receiver(LIBRARY_DIR, DATA_DIR, MAX_UPLOAD_BYTES, IMAGE_EXTENSIONS)

# 画像はインデックスで指定する。削除した画像は None にしてインデックスをずらさない。
IMAGE_LIST = []
IMAGE_LOCK = threading.Lock()


# ============================================================
#  フォルダ設定
# ============================================================

def load_folders():
    if os.path.isfile(FOLDERS_JSON):
        try:
            with open(FOLDERS_JSON, encoding="utf-8") as f:
                return json.load(f).get("folders", [])
        except Exception:
            pass
    return []


def save_folders(folders):
    with open(FOLDERS_JSON, "w", encoding="utf-8") as f:
        json.dump({"folders": folders}, f, ensure_ascii=False, indent=2)


# ============================================================
#  画像スキャン
# ============================================================

def scan_images(folders):
    images = []
    for folder in folders:
        if not os.path.isdir(folder):
            print(f"[警告] フォルダが見つかりません: {folder}")
            continue
        for root, dirs, files in os.walk(folder):
            # .trash（削除済み）や .incoming（受信途中）は表示しない
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                if os.path.splitext(file)[1].lower() in IMAGE_EXTENSIONS:
                    images.append(os.path.join(root, file))
    print(f"[INFO] 画像ファイル数: {len(images)}")
    return images


def rescan(folders):
    global IMAGE_LIST
    images = scan_images(folders)
    with IMAGE_LOCK:
        IMAGE_LIST = images
    return image_summary()


def image_summary():
    with IMAGE_LOCK:
        return {
            "count": len(IMAGE_LIST),
            "indices": [i for i, p in enumerate(IMAGE_LIST) if p is not None],
        }


def add_received_image(path):
    """受信した画像が閲覧対象のフォルダ内なら、再スキャンなしで一覧に加える。"""
    if any(os.path.isdir(f) and is_within(path, f) for f in load_folders()):
        with IMAGE_LOCK:
            IMAGE_LIST.append(path)


IMAGE_LIST = scan_images(load_folders())


# ============================================================
#  ルーティング（ビュアー）
# ============================================================

@app.route("/")
def index():
    return send_file(os.path.join(app.root_path, "index.html"))


@app.route("/api/images")
def api_images():
    return jsonify(image_summary())


@app.route("/api/image/<int:index>")
def api_image(index):
    with IMAGE_LOCK:
        path = IMAGE_LIST[index] if 0 <= index < len(IMAGE_LIST) else None
    if path is None or not os.path.isfile(path):
        abort(404)
    return send_file(path)


@app.route("/api/image/<int:index>/delete", methods=["POST"])
def api_delete_image(index):
    with IMAGE_LOCK:
        path = IMAGE_LIST[index] if 0 <= index < len(IMAGE_LIST) else None
    if path is None or not os.path.isfile(path):
        abort(404)
    dest, sha256 = receiver.trash(path)
    with IMAGE_LOCK:
        if index < len(IMAGE_LIST) and IMAGE_LIST[index] == path:
            IMAGE_LIST[index] = None
    receiver.log("trashed", filename=path, sha256=sha256, remote=request.remote_addr, detail=dest)
    return jsonify({"status": "deleted"})


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    return jsonify(rescan(load_folders()))


@app.route("/api/folders", methods=["GET"])
def api_get_folders():
    return jsonify({"folders": load_folders()})


@app.route("/api/folders", methods=["POST"])
def api_set_folders():
    folders = request.get_json().get("folders", [])
    valid = [f for f in folders if os.path.isdir(f)]
    save_folders(valid)
    return jsonify({"folders": valid, **rescan(valid)})


@app.route("/api/browse")
def api_browse():
    req_path = request.args.get("path", "")

    if not req_path:
        if os.name == "nt":
            import string
            drives = []
            for d in string.ascii_uppercase:
                drive = f"{d}:\\"
                if os.path.isdir(drive):
                    drives.append({"name": drive, "path": drive, "hasChildren": True})
            return jsonify({"path": "", "entries": drives})
        else:
            req_path = "/"

    req_path = os.path.normpath(req_path)
    if not os.path.isdir(req_path):
        return jsonify({"error": "not a directory"}), 400

    entries = []
    try:
        for name in sorted(os.listdir(req_path)):
            full = os.path.join(req_path, name)
            if os.path.isdir(full) and not name.startswith("."):
                try:
                    has_children = any(
                        os.path.isdir(os.path.join(full, c))
                        for c in os.listdir(full)
                        if not c.startswith(".")
                    )
                except PermissionError:
                    has_children = False
                entries.append({"name": name, "path": full, "hasChildren": has_children})
    except PermissionError:
        return jsonify({"error": "permission denied"}), 403

    return jsonify({"path": req_path, "entries": entries})


@app.route("/api/special-folders")
def api_special_folders():
    """受信ライブラリと、Windowsの特殊フォルダ（ピクチャ・ダウンロードなど）を返す"""
    specials = [{"name": LIBRARY_LABEL, "path": LIBRARY_DIR, "hasChildren": True}]
    if os.name == "nt":
        userprofile = os.environ.get("USERPROFILE", "")
        candidates = [
            ("ピクチャ",     os.path.join(userprofile, "Pictures")),
            ("ダウンロード", os.path.join(userprofile, "Downloads")),
        ]
        for label, path in candidates:
            if os.path.isdir(path):
                specials.append({"name": label, "path": path, "hasChildren": True})
    return jsonify({"specials": specials})


# ============================================================
#  ルーティング（Nightdropからの受信）
# ============================================================

def upload_response(status, message="", **fields):
    return jsonify({"status": status, "message": message, **fields}), STATUS_HTTP[status]


def check_token():
    """共有トークンを確認し、拒否する場合はレスポンスを返す。"""
    if not UPLOAD_TOKEN:
        return jsonify({"status": "error", "message": "VIEWER_UPLOAD_TOKEN が未設定のため受信できません"}), 503
    header = request.headers.get("Authorization", "")
    token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(token.encode("utf-8"), UPLOAD_TOKEN.encode("utf-8")):
        receiver.log("unauthorized", remote=request.remote_addr, detail=request.path)
        return upload_response("unauthorized", "トークンが違います")
    return None


@app.route("/api/health")
def api_health():
    denied = check_token()
    if denied:
        return denied
    return jsonify({"status": "ok"})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    denied = check_token()
    if denied:
        return denied

    remote = request.remote_addr
    try:
        form = request.form
        file = request.files.get("file")
    except BadRequest as e:
        # 通信が途中で切れた場合など
        receiver.log("corrupted", remote=remote, detail=e.description)
        return upload_response("corrupted", "送信内容を読み取れませんでした")

    batch    = form.get("batch", "")
    sha256   = form.get("sha256", "")
    filename = form.get("filename") or (file.filename if file else "")
    log_info = {"batch": batch, "filename": filename, "sha256": sha256, "remote": remote}

    if file is None:
        receiver.log("invalid", **log_info, detail="file がありません")
        return upload_response("invalid", "file がありません")

    try:
        status, dest = receiver.receive(
            file.stream, batch, sha256, filename, form.get("modified_at"),
            on_saved=add_received_image)
    except UploadError as e:
        receiver.log(e.status, **log_info, detail=e.message)
        return upload_response(e.status, e.message)
    except Exception as e:
        receiver.log("error", **log_info, detail=repr(e))
        return upload_response("error", "サーバーで保存に失敗しました")

    rel = os.path.relpath(dest, LIBRARY_DIR) if dest else None
    receiver.log(status, **log_info, detail=rel or "")
    return upload_response(status, path=rel)


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(e):
    receiver.log("too_large", remote=request.remote_addr, detail=request.content_length)
    return upload_response("too_large", "ファイルサイズが上限を超えています")


# ============================================================

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


if __name__ == "__main__":
    print("=" * 45)
    print("画像ビュアー サーバー v4 起動中...")
    print(f"画像枚数: {len(IMAGE_LIST)}")
    print(f"受信ライブラリ: {LIBRARY_DIR}")
    if not UPLOAD_TOKEN:
        print("※VIEWER_UPLOAD_TOKEN が未設定のため、Nightdropからの受信は無効です")
    print("ブラウザで以下のURLを開いてください:")
    print(f"  http://<このPCのIPアドレス>:{PORT}")
    print("  ※IPは「ipconfig」コマンドで確認")
    print("=" * 45)
    try:
        from waitress import serve
    except ImportError:
        app.run(host=HOST, port=PORT, debug=False)
    else:
        # waitress 側の受信上限（初期値1GB）は Flask の上限より大きいままにする。
        # 同じ値にすると、上限超過時に waitress が送信途中で接続を切り、
        # アプリは too_large を受け取れず通信エラーとしてバッチを中断してしまう。
        serve(app, host=HOST, port=PORT)
