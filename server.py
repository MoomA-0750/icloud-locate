#!/usr/bin/env python3
"""icloud-locate を常駐させ、HTTP で位置を返す。

iCloud へのセッションを1本だけ保ち、他のアプリからは HTTP で問い合わせる。
CLI と違って毎回ログインし直さないので速く、Apple 側への負荷も小さい。

セッション切れになっても落ちずに 503 (auth_required) を返し続ける。
人間が locate.sh を対話実行して2FAを通せば、次の問い合わせで保存済み
セッションを読み直して復帰する。

既定では 127.0.0.1 だけで待ち受ける。座標は個人データなのでログに出さない。
"""
import argparse
import hmac
import ipaddress
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import environ
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloud2SARequiredException,
    PyiCloudAuthRequiredException,
    PyiCloudFailedLoginException,
    PyiCloudNoStoredPasswordAvailableException,
)

from icloud_locate import (
    AuthRequired, connect, env_file, is_fresh, load_env_file, locate, log, snapshot,
)

AUTH_ERRORS = (
    AuthRequired,
    PyiCloudAuthRequiredException,
    PyiCloudFailedLoginException,
    PyiCloud2FARequiredException,
    PyiCloud2SARequiredException,
    PyiCloudNoStoredPasswordAvailableException,
)

# セッション切れ中に問い合わせが来るたび Apple へログインを試みないよう間隔を空ける
RECONNECT_BACKOFF = 60
MAX_TIMEOUT = 120
# /devices を頻繁に呼ばれても、iCloud へ取り直しに行くのはこの間隔に1回まで
DEVICES_SYNC_MIN = 30


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Locator:
    """iCloud セッションを1本保持し、問い合わせを直列に処理する。

    pyicloud のセッションはスレッド安全ではないので、iCloud に触る処理は
    すべて self.lock の中で行う。
    """

    def __init__(self, apple_id):
        self.apple_id = apple_id
        self.lock = threading.Lock()
        self.api = None
        self.auth_error = None
        self.last_attempt = 0.0
        self.last_sync = None

    def _ensure(self):
        if self.api is not None:
            return self.api.devices
        now = time.time()
        if self.auth_error and now - self.last_attempt < RECONNECT_BACKOFF:
            raise ApiError(503, "auth_required", self.auth_error)
        self.last_attempt = now
        try:
            # pyicloud 内蔵の監視スレッドは止め、更新はこちらで制御する
            self.api = connect(self.apple_id, interactive=False, refresh_interval=None)
            manager = self.api.devices
        except AUTH_ERRORS as exc:
            self._drop(exc)
            raise ApiError(503, "auth_required", self.auth_error)
        except Exception as exc:
            self.api = None
            raise ApiError(502, "upstream_error", f"{type(exc).__name__}: {exc}")
        self.auth_error = None
        self.last_sync = time.time()
        log("→ iCloud に接続しました")
        return manager

    def _drop(self, exc):
        self.api = None
        self.auth_error = str(exc) if isinstance(exc, AuthRequired) else (
            f"セッションが切れました ({type(exc).__name__})。"
            f"locate.sh --apple-id {self.apple_id} を対話実行して2FAを通してください"
        )
        log(f"!! {self.auth_error}")

    def _run(self, fn):
        with self.lock:
            manager = self._ensure()
            try:
                return fn(manager)
            except AUTH_ERRORS as exc:
                self._drop(exc)
                raise ApiError(503, "auth_required", self.auth_error)
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError(502, "upstream_error", f"{type(exc).__name__}: {exc}")

    def sync(self):
        """端末を起こさずに、iCloud 上の最新の状態を取り直す。"""
        def fn(manager):
            manager.refresh(locate=False)
            self.last_sync = time.time()
        self._run(fn)

    def devices(self, max_age):
        def fn(manager):
            if not self.last_sync or time.time() - self.last_sync >= DEVICES_SYNC_MIN:
                manager.refresh(locate=False)
                self.last_sync = time.time()
            out = snapshot(manager, None)
            for d in out:
                d["fresh"] = is_fresh(d, max_age)
            return out
        return self._run(fn)

    def location(self, device, max_age, refresh, timeout, interval):
        def fn(manager):
            if refresh != "always":
                # 手元のキャッシュが古くても iCloud 側には新しい位置があり得る
                manager.refresh(locate=False)
                self.last_sync = time.time()
            out = locate(manager, device, max_age, refresh, timeout, interval, "single")
            if not out:
                raise ApiError(404, "device_not_found", f"該当する端末がありません: {device}")
            return out
        return self._run(fn)

    def status(self):
        # 測位待ちでロックが長く握られていても即答できるよう、ロックは取らない
        if self.api is not None:
            status = "ok"
        elif self.auth_error:
            status = "auth_required"
        else:
            status = "disconnected"
        return {
            "status": status,
            "message": self.auth_error,
            "last_sync": self.last_sync and round(self.last_sync),
        }


def poll_loop(locator, interval, stop):
    while not stop.wait(interval):
        try:
            locator.sync()
        except ApiError as exc:
            if exc.code != "auth_required":
                log(f"   定期同期に失敗: {exc.message}")


def make_handler(locator, args, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "icloud-locate"

        def log_request(self, code="-", size="-"):
            # クエリにはデバイス名しか入らないが、念のためパスだけ出す。
            # 不正なリクエストでは command / path が無いまま呼ばれる
            path = urlparse(getattr(self, "path", "")).path or "-"
            log(f"{self.address_string()} {self.command or '-'} {path} -> {code}")

        def log_message(self, fmt, *a):
            log(f"{self.address_string()} {fmt % a}")

        def _send(self, status, body):
            data = json.dumps(body, ensure_ascii=False, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if token is None:
                return True
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if not self._authorized():
                    raise ApiError(401, "unauthorized", "Authorization: Bearer <token> が必要です")
                if url.path == "/health":
                    st = locator.status()
                    self._send(200 if st["status"] == "ok" else 503, st)
                elif url.path == "/devices":
                    self._send(200, locator.devices(_int(q, "max_age", args.max_age)))
                elif url.path == "/location":
                    self._send(200, self._location(q))
                else:
                    raise ApiError(404, "not_found", "エンドポイントは /health /devices /location です")
            except ApiError as exc:
                self._send(exc.status, {"error": exc.code, "message": exc.message})

        def _location(self, q):
            device = q.get("device") or args.device
            refresh = q.get("refresh", "auto")
            if refresh not in ("auto", "always", "never"):
                raise ApiError(400, "bad_request", "refresh は auto|always|never です")
            if not device:
                # 全端末への測位要求は無関係な端末の電池を減らすので受け付けない
                raise ApiError(400, "bad_request", "device を指定してください（既定端末も未設定です）")
            return locator.location(
                device,
                _int(q, "max_age", args.max_age),
                refresh,
                min(_int(q, "timeout", args.refresh_timeout), MAX_TIMEOUT),
                args.refresh_interval,
            )

    return Handler


def _int(q, key, default):
    if key not in q:
        return default
    try:
        v = int(q[key])
    except ValueError:
        raise ApiError(400, "bad_request", f"{key} は整数で指定してください")
    if v < 0:
        raise ApiError(400, "bad_request", f"{key} は0以上で指定してください")
    return v


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main():
    load_env_file()
    p = argparse.ArgumentParser(description="icloud-locate の常駐HTTP API")
    p.add_argument("--apple-id", default=environ.get("ICLOUD_LOCATE_APPLE_ID"))
    p.add_argument("--device", default=environ.get("ICLOUD_LOCATE_DEVICE"),
                   help="/location で device を省略したときの端末名")
    p.add_argument("--host", default=environ.get("ICLOUD_LOCATE_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(environ.get("ICLOUD_LOCATE_PORT", "8765")))
    p.add_argument("--token-file", default=environ.get("ICLOUD_LOCATE_TOKEN_FILE"),
                   help="Bearerトークンを1行で書いたファイル。127.0.0.1以外で待ち受けるなら必須")
    p.add_argument("--sync-interval", type=int, default=300,
                   help="端末を起こさずにiCloudの状態を取り直す間隔（秒）。0で無効")
    p.add_argument("--max-age", type=int, default=600)
    p.add_argument("--refresh-timeout", type=int, default=45)
    p.add_argument("--refresh-interval", type=int, default=5)
    args = p.parse_args()

    if not args.apple_id:
        p.error(f"--apple-id か ICLOUD_LOCATE_APPLE_ID（{env_file()}）が必要です")

    token = None
    if args.token_file:
        token = Path(args.token_file).expanduser().read_text().strip()
        if not token:
            p.error(f"{args.token_file} が空です")
    elif not _is_loopback(args.host):
        p.error("127.0.0.1 以外で待ち受けるときは --token-file を指定してください")

    locator = Locator(args.apple_id)
    try:
        locator.sync()
    except ApiError as exc:
        log(f"→ 起動時の接続に失敗しました。問い合わせ時に再試行します: {exc.message}")

    stop = threading.Event()
    if args.sync_interval > 0:
        threading.Thread(target=poll_loop, args=(locator, args.sync_interval, stop),
                         daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(locator, args, token))
    log(f"→ http://{args.host}:{args.port} で待ち受けます")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
