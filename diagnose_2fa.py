#!/usr/bin/env python3
"""2FAの配信方法（信頼済みデバイスへのプッシュ / SMS）を診断する。

セッション切れ時の再認証を自動化できるかを判断するための調査用スクリプト。
Appleが何を提示してくるかは事前に分からないため、実際に認証を開始して確かめる。

既存のセッションを壊さないよう、必ず ICLOUD_COOKIE_DIR に使い捨てのディレクトリを
指定して実行すること。
"""
import sys
from getpass import getpass
from os import environ

from pyicloud import PyiCloudService


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def mask_phone(payload):
    """電話番号IDだけを出す。番号本体はAppleから返らないが念のため伏せる。"""
    if payload is None:
        return "（なし）"
    return f"id={payload.device_id} / pushMode={payload.push_mode} / nonFTEU={payload.non_fteu}"


def main():
    apple_id = sys.argv[1] if len(sys.argv) > 1 else environ.get("ICLOUD_APPLE_ID")
    if not apple_id:
        log("Usage: diagnose_2fa.py <apple-id>")
        sys.exit(1)

    cookie_dir = environ.get("ICLOUD_COOKIE_DIR")
    if not cookie_dir:
        log("!! ICLOUD_COOKIE_DIR が未設定です。")
        log("   既存セッションを壊さないため、使い捨てディレクトリを指定してください:")
        log("   ICLOUD_COOKIE_DIR=/tmp/icloud-2fa-probe ./diagnose_2fa.py <apple-id>")
        sys.exit(1)
    log(f"→ セッション保存先(使い捨て): {cookie_dir}")

    password = environ.get("ICLOUD_PASSWORD") or getpass(f"iCloud password for {apple_id}: ")

    log("→ 認証を開始します（新規セッションなので2FAが要求されるはずです）")
    api = PyiCloudService(apple_id, password, cookie_directory=cookie_dir)

    print("\n=========== 2FA 診断結果 ===========")
    print(f"requires_2fa            : {api.requires_2fa}")
    print(f"requires_2sa            : {api.requires_2sa}")
    print(f"is_trusted_session      : {api.is_trusted_session}")
    print(f"two_factor_delivery_method : {api.two_factor_delivery_method}")
    print(f"two_factor_delivery_notice : {api.two_factor_delivery_notice}")

    # 私有APIだが、SMSが選べるかの判定はここにしか無い
    try:
        mode = api._two_factor_mode()
        phone = api._trusted_phone_number()
        can_sms = api._can_request_sms_2fa_code()
        print(f"_two_factor_mode()      : {mode}")
        print(f"_trusted_phone_number() : {mask_phone(phone)}")
        print(f"_can_request_sms_2fa_code() : {can_sms}")
    except Exception as exc:
        print(f"内部状態の取得に失敗: {type(exc).__name__}: {exc}")
        can_sms = False

    print("====================================\n")

    if not api.requires_2fa:
        log("2FAが要求されませんでした。診断はここまでです。")
        return

    if can_sms:
        log("→ SMS配信が選択可能です。SMSを要求しますか?")
        if input("SMSを送る? [y/N]: ").strip().lower() == "y":
            api._request_sms_2fa_code()
            log(f"→ SMS送信を要求しました (delivery_method={api.two_factor_delivery_method})")
    else:
        log("→ SMS配信は提示されていません。信頼済みデバイスへのプッシュのみです。")

    code = input("2FA code (空Enterで中断): ").strip()
    if not code:
        log("中断しました。使い捨てセッションは破棄して構いません。")
        return
    if api.validate_2fa_code(code):
        log("→ 検証成功。この使い捨てセッションは不要なら削除してください。")
    else:
        log("!! 検証失敗")
        sys.exit(1)


if __name__ == "__main__":
    main()
