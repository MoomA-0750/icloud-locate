# icloud-locate — AIエージェント向け仕様

このリポジトリのセットアップ・運用をAIエージェントが代行するための指示。人間向けの説明は`README.md`にある。

## このツールの位置づけ

iCloud（Find My）経由でApple端末の位置を取得するCLI。**iOS/iPadOS端末側には何もインストールしない**ことが設計上の要件。pyicloudの`fmipservice`（`initClient` / `refreshClient`）を叩いている。

## セットアップを代行するときの手順

1. Python 3.10以上を確認する
2. `python3 -m venv .venv` → `./.venv/bin/pip install pyicloud`
3. **初回認証はユーザー自身にやってもらう**（下記「絶対に守ること」参照）
4. 認証後、`./locate.sh --apple-id <ID> --refresh never` を非対話で実行し、端末一覧を取得する
5. 一覧から対象端末の登録名をユーザーに確認する。iCloudには過去に手放した端末も残っているため、**モデル名だけで推測しない**
6. `--device "<登録名>"` を付けて再実行し、位置が取れることを確認する

## 絶対に守ること

- **ユーザーにApple IDのパスワードをチャットへ貼らせない。** 会話履歴に残る。必ずユーザー自身の端末で`getpass`プロンプトに入力してもらう
- **`ICLOUD_PASSWORD`をコマンドラインやスクリプトに書かない。** シェル履歴に残る
- **初回認証を代行しようとしない。** パスワードと2FAコードの両方が必要で、いずれもエージェントが持つべきものではない。ユーザーに実行してもらうコマンドを提示すること
- **取得した座標をリポジトリやログファイルに書き残さない。** 会話上で使うだけに留める
- **セッションファイル（`~/.local/state/icloud-locate`）を読み出したり、コピーして配布したりしない。** 認証情報そのもの

## 初回認証をユーザーに依頼するときの文面

ローカルの場合:

```
./locate.sh --apple-id <ユーザーのApple ID>
```

リモートホストの場合（**`getpass`にttyが必要なので`-t`が必須**）:

```
ssh -t <host> '~/Documents/git-cloned/icloud-locate/locate.sh --apple-id <ID>'
```

## 設定ファイル

`~/.config/icloud-locate/env` に `ICLOUD_LOCATE_APPLE_ID` / `ICLOUD_LOCATE_DEVICE` があれば、`--apple-id` / `--device` を省略できる（優先順位はフラグ > 環境変数 > ファイル）。端末名が確定したら、このファイルに書くことをユーザーに提案してよい。**`ICLOUD_PASSWORD` はこのファイルにも書かない**（書いても無視される）。

## 非対話で呼ぶときの契約

セッションが有効なら、パスワードもttyも不要で実行できる。これがエージェントから使うための前提。

```bash
./locate.sh --apple-id <ID> --device "<登録名>" --json
```

**終了コード**

| コード | 意味 | エージェントの対応 |
|---|---|---|
| `0` | 成功 | JSONを使う |
| `1` | 位置が取れない等のエラー | stderrをユーザーに見せる |
| `2` | セッション切れ・2FA要求 | **再認証をユーザーに依頼する**。リトライしても無駄 |

`2`が返ったときにパスワードを聞き出して自動化しようとしないこと。設計上、そこは人間の作業。

**JSONの読み方**

`fresh`（boolean）と`age_seconds`を必ず見る。`fresh: false`は測位が`--max-age`（既定600秒）より古いという意味。現在地として使う前にユーザーへ確認する。

`location_available: false`の端末は座標を持たない。オフラインか位置共有オフなので、`--refresh always`にしても解決しない。

## 常駐API（server.py）を使う場合

常駐APIが動いていれば、CLI より先にそちらを使う。`curl -s http://127.0.0.1:8765/health` で確認できる。

- `GET /location?device=<登録名>&refresh=auto` が CLI の `--device ... --json` に相当する。応答の形も同じ
- `503`（`error: auth_required`）は CLI の終了コード`2`と同じ。**再認証をユーザーに依頼する**。サーバーの再起動は不要
- `502` は iCloud 側の一時的なエラー。`404` は端末名の誤り
- 応答にトークンが要る構成（`--token-file`）では、トークンファイルの中身をチャットやログに出さない
- サーバー稼働中に CLI を併用するとセッションファイルを奪い合う。再認証以外は API を使う
- `tailscale-serve.sh up` は tailnet 内へ公開する操作。ユーザーに確認してから実行する。`tailscale serve reset` は他のサービスの公開も消すので使わない

## パラメータの選び方

- 通常は`--refresh auto`（既定）でよい。新しい測位があれば端末を起こさない
- 「今どこにいるか」を厳密に知りたいときだけ`--refresh always --max-age 30`程度にする
- `--locate-scope`は`single`のまま使う。`all`にすると全端末に測位要求が飛び、無関係な端末のバッテリーを消費する
- 測位要求は1回では新しい位置が返らないことが多い。**ポーリング前提**で、`--refresh-timeout`を短くしすぎない（既定45秒）

## 実装上の注意（コードを触る場合）

- pyicloudの`_refresh_client`は`selectedDevice`を`"all"`固定で送る。単一端末指定は`_fmip_refresh_url`への自前POSTで実現している（`request_locate()`）
- `shouldLocate`が載るのは`serverContext`を持つ**2回目以降**のリクエストだけ。初回`initClient`はキャッシュを返すのみ。**1回呼んだだけでは能動測位にならない**
- `AppleDevice.content`はpyicloud 2.xで廃止。`AppleDevice.data`を使う
- セッション保存先はpyicloud既定だと`$TMPDIR/pyicloud/<user>`で、Linuxでは再起動で消える。`cookie_directory()`で`~/.local/state/icloud-locate`に固定している。**ここを`/tmp`配下に戻さない**
- keyringバックエンドが無い環境で例外が飛ぶため、`safe_keyring()`で握りつぶしている

## route-plan スキルとの連携

経路案内スキル`route-plan`の現在地providerとして呼ばれる。スキル側の`scripts/current_location.sh`が以下の環境変数を読む:

```bash
ROUTE_PLAN_ICLOUD_APPLE_ID   # 必須
ROUTE_PLAN_ICLOUD_DEVICE     # 必須。未指定だと全端末が対象になり意図しない位置を拾う
ROUTE_PLAN_ICLOUD_DIR        # 既定 $HOME/Documents/git-cloned/icloud-locate
```

`current_location.sh`は provider によらない統一JSONを返す。詳細はスキル側のSKILL.md手順2を参照。
