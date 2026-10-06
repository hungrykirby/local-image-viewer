"""
Nightdrop（Androidアプリ）からの画像受信
  - バッチ名のフォルダへ1枚ずつ保存する
  - SHA-256 で重複・削除済みを判定する（data/nightdrop.db）
  - 一時ファイルに書いてから移動し、壊れたファイルをライブラリに残さない
  - 受信結果を data/upload.log に記録する
"""

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

# ============================================================
#  定数
# ============================================================

# 返事の種類と HTTP ステータス。saved / duplicate / deleted はアプリ側で送信済み扱い。
STATUS_HTTP = {
    "saved":        201,
    "duplicate":    200,
    "deleted":      200,
    "invalid":      400,
    "unauthorized": 401,
    "too_large":    413,
    "corrupted":    422,
    "error":        500,
}

INCOMING_DIR = ".incoming"   # 受信途中の一時ファイル（ライブラリと同じドライブに置き、移動を一瞬で済ませる）
TRASH_DIR    = ".trash"      # ビュワーで削除した画像の退避先
PART_SUFFIX  = ".part"
DB_NAME      = "nightdrop.db"
LOG_NAME     = "upload.log"
LOG_MAX_BYTES    = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

CHUNK_BYTES        = 1024 * 1024
SNIFF_BYTES        = 32
MAX_NAME_BYTES     = 200
MAX_NAME_ATTEMPTS  = 10000
FALLBACK_STEM      = "image"

BATCH_PATTERN    = re.compile(r"\w[\w\-]{0,63}")
SHA256_PATTERN   = re.compile(r"[0-9a-f]{64}")
FORBIDDEN_CHARS  = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} \
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
AVIF_BRANDS      = {b"avif", b"avis", b"mif1", b"msf1"}


class UploadError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ============================================================
#  入力チェック
# ============================================================

def validate_batch(batch):
    batch = unicodedata.normalize("NFC", (batch or "").strip())
    if not BATCH_PATTERN.fullmatch(batch) or batch.upper() in WINDOWS_RESERVED:
        raise UploadError("invalid", "バッチ名が不正です（英数字・日本語・_・- のみ、64文字以内）")
    return batch


def validate_sha256(value):
    value = (value or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(value):
        raise UploadError("invalid", "sha256 は16進数64文字で指定してください")
    return value


def parse_modified_at(value):
    """更新日時（UNIXエポックのミリ秒）を秒に変換する。未指定なら None。"""
    if value is None or value == "":
        return None
    try:
        millis = int(value)
    except ValueError:
        raise UploadError("invalid", "modified_at はミリ秒の整数で指定してください") from None
    if millis <= 0:
        raise UploadError("invalid", "modified_at が不正です")
    return millis / 1000


def sanitize_filename(name, allowed_extensions):
    """パス成分を捨て、Windowsでも使えない文字を置き換える。日本語はそのまま残す。"""
    name = unicodedata.normalize("NFC", name or "")
    name = re.split(r"[/\\]", name)[-1]
    name = FORBIDDEN_CHARS.sub("_", name).strip().strip(".").strip()
    stem, ext = os.path.splitext(name)
    if ext.lower() not in allowed_extensions:
        raise UploadError("invalid", f"対応していない拡張子です: {ext or '(なし)'}")
    stem = stem.rstrip(" .") or FALLBACK_STEM
    if stem.upper() in WINDOWS_RESERVED:
        stem = f"_{stem}"
    stem = stem.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", errors="ignore")
    return stem + ext


def looks_like_image(head):
    return (
        head.startswith(b"\xff\xd8\xff")                       # JPEG
        or head.startswith(b"\x89PNG\r\n\x1a\n")               # PNG
        or head[:6] in (b"GIF87a", b"GIF89a")                  # GIF
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")     # WebP
        or head.startswith(b"BM")                              # BMP
        or (head[4:8] == b"ftyp" and head[8:12] in AVIF_BRANDS)  # AVIF
    )


def is_within(path, directory):
    path = os.path.normcase(os.path.realpath(path))
    directory = os.path.normcase(os.path.realpath(directory))
    return os.path.commonpath([path, directory]) == directory


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reserve_unique_path(directory, name):
    """同名ファイルがあれば「名前 (2).jpg」のようにずらし、空ファイルで名前を確保する。"""
    stem, ext = os.path.splitext(name)
    for n in range(1, MAX_NAME_ATTEMPTS + 1):
        candidate = os.path.join(directory, name if n == 1 else f"{stem} ({n}){ext}")
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return candidate
        except FileExistsError:
            continue
    raise UploadError("error", "保存先のファイル名を決められませんでした")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
#  受信処理
# ============================================================

class Receiver:
    def __init__(self, library_dir, data_dir, max_bytes, allowed_extensions):
        self.library_dir = library_dir
        self.incoming_dir = os.path.join(library_dir, INCOMING_DIR)
        self.trash_dir = os.path.join(library_dir, TRASH_DIR)
        self.db_path = os.path.join(data_dir, DB_NAME)
        self.max_bytes = max_bytes
        self.allowed_extensions = allowed_extensions
        self._lock = threading.Lock()

        for d in (library_dir, self.incoming_dir, self.trash_dir, data_dir):
            os.makedirs(d, exist_ok=True)
        self._cleanup_incoming()
        self._init_db()
        self.logger = self._init_logger(os.path.join(data_dir, LOG_NAME))

    # ---------- 準備 ----------

    def _cleanup_incoming(self):
        """前回の異常終了で残った受信途中のファイルを消す。"""
        for name in os.listdir(self.incoming_dir):
            if name.endswith(PART_SUFFIX):
                try:
                    os.remove(os.path.join(self.incoming_dir, name))
                except OSError:
                    pass

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    sha256        TEXT PRIMARY KEY,
                    state         TEXT NOT NULL,  -- stored / deleted
                    batch         TEXT,
                    path          TEXT,
                    original_name TEXT,
                    received_at   TEXT,
                    deleted_at    TEXT
                )
            """)

    @staticmethod
    def _init_logger(path):
        logger = logging.getLogger("nightdrop")
        logger.setLevel(logging.INFO)
        logger.propagate = False  # waitress が設定するルートロガーへの二重出力を防ぐ
        if not logger.handlers:
            fmt = logging.Formatter("%(asctime)s\t%(message)s")
            file_handler = RotatingFileHandler(
                path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
            file_handler.setFormatter(fmt)
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            logger.addHandler(file_handler)
            logger.addHandler(console)
        return logger

    def log(self, status, batch="", filename="", sha256="", remote="", detail=""):
        self.logger.info("\t".join(str(v or "-") for v in (status, batch, filename, sha256, remote, detail)))

    # ---------- 重複・削除済み判定 ----------

    def known_status(self, sha256):
        with self._connect() as db:
            row = db.execute("SELECT state FROM images WHERE sha256 = ?", (sha256,)).fetchone()
        if row is None:
            return None
        return "deleted" if row[0] == "deleted" else "duplicate"

    # ---------- 保存 ----------

    def receive(self, stream, batch, sha256, filename, modified_at, on_saved=None):
        """画像を保存し (status, 保存先の絶対パス or None) を返す。失敗時は UploadError。"""
        batch = validate_batch(batch)
        sha256 = validate_sha256(sha256)
        name = sanitize_filename(filename, self.allowed_extensions)
        mtime = parse_modified_at(modified_at)

        known = self.known_status(sha256)
        if known:
            return known, None

        tmp = os.path.join(self.incoming_dir, uuid.uuid4().hex + PART_SUFFIX)
        try:
            digest, size, head = self._write_temp(stream, tmp)
            if size == 0:
                raise UploadError("corrupted", "空のファイルです")
            if digest != sha256:
                raise UploadError("corrupted", "ハッシュ値が一致しません")
            if not looks_like_image(head):
                raise UploadError("corrupted", "画像として認識できません")

            with self._lock:
                # 同じ画像が並行して届いた場合に備えて、確定直前にもう一度確認する
                known = self.known_status(sha256)
                if known:
                    return known, None
                dest = self._place(tmp, batch, name, mtime)
                try:
                    with self._connect() as db:
                        db.execute(
                            "INSERT INTO images (sha256, state, batch, path, original_name, received_at)"
                            " VALUES (?, 'stored', ?, ?, ?, ?)",
                            (sha256, batch, dest, filename, now_iso()))
                except Exception:
                    os.remove(dest)
                    raise
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        if on_saved:
            on_saved(dest)
        return "saved", dest

    def _write_temp(self, stream, tmp):
        digest = hashlib.sha256()
        size = 0
        head = b""
        with open(tmp, "xb") as out:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                size += len(chunk)
                if size > self.max_bytes:
                    raise UploadError("too_large", f"ファイルサイズが上限（{self.max_bytes} バイト）を超えています")
                if len(head) < SNIFF_BYTES:
                    head += chunk[:SNIFF_BYTES - len(head)]
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest(), size, head

    def _place(self, tmp, batch, name, mtime):
        batch_dir = os.path.join(self.library_dir, batch)
        if not is_within(batch_dir, self.library_dir):
            raise UploadError("invalid", "バッチ名が不正です")
        os.makedirs(batch_dir, exist_ok=True)
        dest = reserve_unique_path(batch_dir, name)
        try:
            os.replace(tmp, dest)
            if mtime is not None:
                os.utime(dest, (mtime, mtime))
        except Exception:
            if os.path.exists(dest):
                os.remove(dest)
            raise
        return dest

    # ---------- 削除（ゴミ箱へ移動） ----------

    def trash(self, path):
        """画像をゴミ箱フォルダへ移し、同じ画像が再送されても保存しないよう記録する。"""
        sha256 = hash_file(path)
        with self._lock:
            day_dir = os.path.join(self.trash_dir, datetime.now().strftime("%Y-%m-%d"))
            os.makedirs(day_dir, exist_ok=True)
            dest = reserve_unique_path(day_dir, os.path.basename(path))
            try:
                os.replace(path, dest)
            except OSError:
                # 別ドライブの画像はコピーしてから消す
                shutil.copy2(path, dest)
                os.remove(path)
            with self._connect() as db:
                db.execute(
                    "INSERT INTO images (sha256, state, path, original_name, deleted_at)"
                    " VALUES (?, 'deleted', ?, ?, ?)"
                    " ON CONFLICT(sha256) DO UPDATE SET"
                    "   state = 'deleted', path = excluded.path, deleted_at = excluded.deleted_at",
                    (sha256, dest, os.path.basename(path), now_iso()))
        return dest, sha256
