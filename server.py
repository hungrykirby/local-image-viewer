"""
iPad/Android画像ビュアー サーバー v3
使い方:
  1. pip install flask
  2. python server.py
  3. ブラウザで http://<WindowsのIPアドレス>:5000 を開く
"""

from flask import Flask, jsonify, send_file, abort, request
from flask.wrappers import Response
import os
import json

# プロジェクト全体を公開せず、必要なファイルだけを明示的に配信する。
app = Flask(__name__, static_folder=None)

FOLDERS_JSON = os.path.join(os.path.dirname(__file__), "folders.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
IMAGE_LIST = []


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
        for root, _, files in os.walk(folder):
            for file in files:
                if os.path.splitext(file)[1].lower() in IMAGE_EXTENSIONS:
                    images.append(os.path.join(root, file))
    print(f"[INFO] 画像ファイル数: {len(images)}")
    return images


IMAGE_LIST = scan_images(load_folders())


# ============================================================
#  ルーティング
# ============================================================

@app.route("/")
def index():
    return send_file(os.path.join(app.root_path, "index.html"))


@app.route("/api/images")
def api_images():
    return jsonify({"count": len(IMAGE_LIST)})


@app.route("/api/image/<int:index>")
def api_image(index):
    if index < 0 or index >= len(IMAGE_LIST):
        abort(404)
    path = IMAGE_LIST[index]
    if not os.path.isfile(path):
        abort(404)
    return send_file(path)


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    global IMAGE_LIST
    IMAGE_LIST = scan_images(load_folders())
    return jsonify({"count": len(IMAGE_LIST)})


@app.route("/api/folders", methods=["GET"])
def api_get_folders():
    return jsonify({"folders": load_folders()})


@app.route("/api/folders", methods=["POST"])
def api_set_folders():
    global IMAGE_LIST
    folders = request.get_json().get("folders", [])
    valid = [f for f in folders if os.path.isdir(f)]
    save_folders(valid)
    IMAGE_LIST = scan_images(valid)
    return jsonify({"folders": valid, "count": len(IMAGE_LIST)})


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
    """Windowsの特殊フォルダ（ピクチャ・ダウンロードなど）を返す"""
    specials = []
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

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


if __name__ == "__main__":
    print("=" * 45)
    print("画像ビュアー サーバー v3 起動中...")
    print(f"画像枚数: {len(IMAGE_LIST)}")
    print("ブラウザで以下のURLを開いてください:")
    print("  http://<このPCのIPアドレス>:5000")
    print("  ※IPは「ipconfig」コマンドで確認")
    print("=" * 45)
    app.run(host="0.0.0.0", port=5000, debug=False)
