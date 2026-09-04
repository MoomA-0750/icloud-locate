#!/usr/bin/env python3
"""iCloud (Find My) から自分のApple端末の位置を取得する。

端末側には一切何も入れない。pyicloud経由で fmipservice を叩くだけ。
初回だけ2FAが要る。以降は ~/.pyicloud のセッションcookieで非対話に動く。

測位が古い場合は refreshClient に shouldLocate を立てて能動的に測位要求を出し、
新しい位置が返るまでポーリングする（Find Myアプリの「更新」相当）。
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from getpass import getpass
from os import environ

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException  # noqa: F401


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def safe_keyring(apple_id):
    """keyringバックエンドが無い環境でも落ちないパスワード取得。"""
    try:
        from pyicloud.utils import get_password_from_keyring

        return get_password_from_keyring(apple_id)
    except Exception as exc:
        log(f"→ keyringは使えません ({type(exc).__name__})")
        return None


def _needs_tty(apple_id):
    log("!! 保存済みセッションでは認証できませんでした。")
    log("   ttyのある環境で一度だけ対話実行し、2FAを通してください:")
    log(f"   ssh -t <host> '~/Documents/git-cloned/icloud-locate/locate.sh --apple-id {apple_id}'")
    sys.exit(2)


def connect(apple_id):
    """まず保存済みセッション(~/.pyicloud)で認証を試し、駄目なときだけ対話に落とす。

    セッションが生きていればパスワードもttyも不要なので、エージェントから
    非対話で呼べる。これが route-plan に組み込むための前提。
    """
    password = environ.get("ICLOUD_PASSWORD") or safe_keyring(apple_id)

    try:
        api = PyiCloudService(apple_id, password)
    except Exception as exc:
        if not sys.stdin.isatty():
            log(f"→ セッション認証に失敗 ({type(exc).__name__}: {exc})")
            _needs_tty(apple_id)
        log(f"→ セッション認証に失敗 ({type(exc).__name__})。パスワードを入力してください。")
        password = getpass(f"iCloud password for {apple_id}: ")
        api = PyiCloudService(apple_id, password)

    if api.requires_2fa:
        if not sys.stdin.isatty():
            log("→ 2FAが要求されました（セッション期限切れ）")
            _needs_tty(apple_id)
        log("→ 2FAが必要です。信頼済み端末に届いた6桁コードを入力してください。")
        code = input("2FA code: ").strip()
        if not api.validate_2fa_code(code):
            log("!! 2FAコードの検証に失敗しました")
            sys.exit(1)
        log("→ 2FA検証OK")
        if not api.is_trusted_session:
            log("→ セッションを信頼済みにします (次回から非対話で動きます)")
            api.trust_session()
    elif api.requires_2sa:
        if not sys.stdin.isatty():
            _needs_tty(apple_id)
        log("→ 旧2SAフローが要求されました。信頼済み端末を選んでください。")
        devices = api.trusted_devices
        for i, d in enumerate(devices):
            log(f"  [{i}] {d.get('deviceName', d.get('phoneNumber', d))}")
        idx = int(input("device index: ").strip())
        dev = devices[idx]
        if not api.send_verification_code(dev):
            log("!! 検証コードの送信に失敗しました")
            sys.exit(1)
        code = input("verification code: ").strip()
        if not api.validate_verification_code(dev, code):
            log("!! 検証コードの検証に失敗しました")
            sys.exit(1)

    return api


def matches(content, needle):
    if not needle:
        return True
    needle = needle.lower()
    name = (content.get("name") or "").lower()
    model = (content.get("deviceDisplayName") or content.get("rawDeviceModel") or "").lower()
    return needle in name or needle in model


def snapshot(manager, needle, now=None):
    """現在保持しているデバイス情報から、対象端末の要約リストを作る。"""
    now = now or time.time()
    out = []
    for dev in manager.devices.values():
        c = dev.data
        if not matches(c, needle):
            continue
        loc = dev.location or {}
        ts_ms = loc.get("timeStamp")
        age = (now - ts_ms / 1000) if ts_ms else None
        out.append({
            "name": c.get("name") or "",
            "model": c.get("deviceDisplayName") or c.get("rawDeviceModel") or "",
            "battery": c.get("batteryLevel"),
            "location_available": bool(loc),
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
            "horizontal_accuracy_m": loc.get("horizontalAccuracy"),
            "position_type": loc.get("positionType"),
            "is_old": loc.get("isOld"),
            "timestamp_ms": ts_ms,
            "timestamp": iso(ts_ms),
            "age_seconds": round(age) if age is not None else None,
        })
    return out


def iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat()


def is_fresh(entry, max_age):
    return (
        entry["location_available"]
        and entry["age_seconds"] is not None
        and entry["age_seconds"] <= max_age
    )


def locate_until_fresh(manager, needle, max_age, timeout, interval):
    """shouldLocate を立てて能動的に測位要求を出し、新しい位置が返るまで待つ。

    Find Myアプリの「更新」と同じで、iCloud側から端末へプッシュして測り直させる。
    端末がスリープ・圏外だと応答しないので、必ずタイムアウトを持たせる。
    """
    deadline = time.time() + timeout
    attempt = 0
    while True:
        attempt += 1
        log(f"→ 測位要求 {attempt}回目 (shouldLocate)")
        try:
            manager.refresh(locate=True)
        except Exception as exc:
            log(f"   測位要求が失敗しました: {type(exc).__name__}: {exc}")
        current = snapshot(manager, needle)
        fresh = [e for e in current if is_fresh(e, max_age)]
        if fresh:
            for e in fresh:
                log(f"   {e['name']}: {e['age_seconds']}秒前の測位を取得")
            return current
        if time.time() >= deadline:
            log(f"   {timeout}秒待ちましたが新しい測位が返りませんでした")
            return current
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apple-id", required=True)
    p.add_argument("--device", default=None, help="端末名・モデル名の部分一致で絞り込む")
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-age", type=int, default=600,
                   help="この秒数以内の測位を「新しい」とみなす (既定600=10分)")
    p.add_argument("--refresh", choices=["auto", "always", "never"], default="auto",
                   help="auto=古いときだけ測位要求 / always=常に / never=キャッシュのみ")
    p.add_argument("--refresh-timeout", type=int, default=45)
    p.add_argument("--refresh-interval", type=int, default=5)
    args = p.parse_args()

    api = connect(args.apple_id)
    manager = api.devices

    devices = snapshot(manager, args.device)
    if not devices:
        log("該当する端末がありません")
        sys.exit(1)

    stale = not any(is_fresh(d, args.max_age) for d in devices)
    if args.refresh == "always" or (args.refresh == "auto" and stale):
        if args.refresh == "auto":
            log(f"→ {args.max_age}秒以内の測位がないので、端末に測位を要求します")
        devices = locate_until_fresh(
            manager, args.device, args.max_age,
            args.refresh_timeout, args.refresh_interval,
        )

    for d in devices:
        d["fresh"] = is_fresh(d, args.max_age)

    if args.json:
        print(json.dumps(devices, ensure_ascii=False, indent=2))
        return

    for d in devices:
        mark = "" if d["fresh"] else "  ⚠️"
        print(f"■ {d['name']}  ({d['model']}){mark}")
        if d["location_available"]:
            print(f"   {d['latitude']}, {d['longitude']}")
            age = d["age_seconds"]
            age_txt = f"{age}秒前" if age is not None and age < 120 else \
                      (f"{round(age / 60)}分前" if age is not None else "不明")
            print(f"   精度 {d['horizontal_accuracy_m']}m / {d['position_type']} / {age_txt} ({d['timestamp']})")
            if not d["fresh"]:
                print("   ※ 測位が古いため出発地としては要確認")
        else:
            print("   位置情報なし (オフライン / 位置共有オフ)")
        if d["battery"] is not None:
            print(f"   バッテリー {round(d['battery'] * 100)}%")
        print()


if __name__ == "__main__":
    main()
