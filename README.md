# icloud-locate

iCloud（Find My）経由で、自分の Apple 端末の位置情報を取得する CLI ツール。

**iPhone / iPad 側には何も入れません。** アプリのインストールも設定変更も不要で、Apple 純正の iCloud の仕組みだけを使います。位置情報を必要とする AI エージェント（Claude Code 等）に、現在地を渡すことを想定しています。

## できること / できないこと

**できること**

- iCloud に紐づく Apple 端末の位置を一覧で取得する
- 端末に測位を要求して、位置を「今のもの」に更新する（Find My アプリの更新ボタン相当）
- 測位の鮮度（何秒前のものか）を返す
- 一度認証すれば、以降はパスワード入力なしで非対話実行できる

**できないこと**

- リアルタイムの追跡・常時監視（一回分のスナップショットを取るツールです）
- 他人の端末の位置取得（自分の Apple ID に紐づく端末だけです）
- AirTag などアクセサリの高度な機能（位置は取れますが、専用機能はありません）

## 必要なもの

- Python 3.10 以上
- 自分の Apple ID とパスワード
- 2FA コードを受け取れる Apple 端末（**初回セットアップ時のみ**）
- 位置情報を取りたい端末で「探す」がオンになっていること

## セットアップ

### 1. 取得して環境を作る

```bash
git clone <このリポジトリ> ~/Documents/git-cloned/icloud-locate
cd ~/Documents/git-cloned/icloud-locate
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install pyicloud
```

### 2. 初回認証（2FA）

**この一回だけは、対話的に実行する必要があります。**

```bash
./locate.sh --apple-id your@apple.id
```

1. `iCloud password:` — 入力は画面に表示されません
2. `2FA code:` — 信頼済みの Apple 端末に届く6桁のコード

成功すると、iCloud に紐づく全端末が一覧表示されます。過去に手放した端末も履歴として残っているため、想像より多く出ることがあります。**この一覧から、位置を取りたい端末の登録名を控えてください。**

> **リモートホストで実行する場合**、パスワード入力には端末（tty）が必要です。`ssh -t` を使ってください:
>
> ```bash
> ssh -t <host> '~/Documents/git-cloned/icloud-locate/locate.sh --apple-id your@apple.id'
> ```

### 3. 動作確認

```bash
./locate.sh --apple-id your@apple.id --device "端末の登録名"
```

パスワードを聞かれずに位置が出れば成功です。以降は非対話で使えます。

## 使い方

```
locate.sh --apple-id <ID> [--device <名前>] [--json]
          [--max-age 600] [--refresh auto|always|never]
          [--refresh-timeout 45] [--refresh-interval 5]
          [--locate-scope single|all]
```

| オプション | 既定 | 説明 |
|---|---|---|
| `--device` | 全端末 | 端末名・モデル名の部分一致で絞り込む |
| `--json` | off | JSON で出力する（プログラムから使う場合） |
| `--max-age` | `600` | この秒数以内の測位を「新しい」とみなす |
| `--refresh` | `auto` | `auto`=古いときだけ測位要求 / `always`=常に / `never`=キャッシュのみ |
| `--refresh-timeout` | `45` | 測位要求を諦めるまでの秒数 |
| `--refresh-interval` | `5` | 測位要求のポーリング間隔（秒） |
| `--locate-scope` | `single` | `single`=対象端末のみに測位要求 / `all`=全端末 |

### 例

```bash
# 現在地を JSON で取得（古ければ自動で測り直す）
./locate.sh --apple-id your@apple.id --device "My iPad" --json

# キャッシュだけ見る（測位要求を出さない＝端末のバッテリーを使わない）
./locate.sh --apple-id your@apple.id --device "My iPad" --refresh never

# 30秒以内の測位を必ず取る
./locate.sh --apple-id your@apple.id --device "My iPad" --refresh always --max-age 30
```

### JSON 出力

```json
[
  {
    "name": "My iPad",
    "model": "iPad Pro 13-inch",
    "battery": 0.26,
    "location_available": true,
    "latitude": 35.681236,
    "longitude": 139.767125,
    "horizontal_accuracy_m": 4.59,
    "position_type": "Wifi",
    "is_old": false,
    "timestamp_ms": 1767225600000,
    "timestamp": "2026-01-01T09:00:00.000000+09:00",
    "age_seconds": 9,
    "fresh": true
  }
]
```

呼び出し側は **`fresh` を見て判断してください**。`false` なら測位が古いので、そのまま現在地として使ってよいか確認するのが安全です。

## 測位の更新について

`--refresh` を `auto` / `always` にすると、iCloud 経由で端末に測位を要求します（`refreshClient` の `shouldLocate`）。Find My アプリの更新ボタンと同じ仕組みです。

実測では、**1〜4回のポーリング（5〜30秒）で数秒前の位置**が取れ、精度は 5〜7m 程度まで改善します。

端末がスリープ・圏外・電源オフだと応答しません。`--refresh-timeout` で必ず打ち切られます。

`--locate-scope single`（既定）は、`--device` で絞った端末だけに測位を要求します。`all` にすると全端末に飛ぶため、他の端末のバッテリーを消費します。**特別な理由がなければ `single` のままにしてください。**

## 環境変数

| 変数 | 用途 |
|---|---|
| `ICLOUD_PASSWORD` | パスワードをプロンプトなしで渡す（後述の注意を参照） |
| `ICLOUD_COOKIE_DIR` | セッションの保存先。既定は `~/.local/state/icloud-locate` |

## route-plan スキルとの連携

経路案内スキル `route-plan` の現在地 provider として使えます。`~/.zshrc` 等に:

```bash
export ROUTE_PLAN_ICLOUD_APPLE_ID="your@apple.id"
export ROUTE_PLAN_ICLOUD_DEVICE="端末の登録名"
export ROUTE_PLAN_ICLOUD_DIR="$HOME/Documents/git-cloned/icloud-locate"
```

スキル側の `scripts/current_location.sh` が、この設定を見て自動的に icloud provider を選びます。

## トラブルシューティング

**`保存済みセッションでは認証できませんでした` と出る**

セッションが期限切れです。`ssh -t` などで対話実行し、2FA をやり直してください。

Apple 側のセッション有効期間は公称2ヶ月程度とされていますが、**実際にどれくらいもつかは保証されません。** 短く切れることもあれば、Apple 側の都合で挙動が変わることもあります。「いつ切れるか分からないもの」として扱ってください。

**再起動したら毎回 2FA を求められる**

`ICLOUD_COOKIE_DIR` が `/tmp` 配下を指していませんか。pyicloud の既定はテンポラリディレクトリで、Linux では再起動時に消えます。このツールは既定を `~/.local/state/icloud-locate` にしていますが、明示的に上書きしている場合は永続する場所に変更してください。

**位置情報なし（オフライン / 位置共有オフ）になる**

端末の電源が入っていない、ネットワークに繋がっていない、または「探す」がオフです。`--refresh always` にしても、端末が応答できなければ位置は返りません。

**セッションはマシンごとに別です**

複数のマシンで使う場合、それぞれで一度ずつ 2FA を通す必要があります。セッションファイルを他のマシンへコピーしても動く場合がありますが、想定された使い方ではありません。

**`keyringは使えません` と出る**

問題ありません。keyring バックエンドが無い環境（サーバー等）で出る情報メッセージで、パスワード入力プロンプトに切り替わるだけです。

## 外出先での再認証に備える

このツールを**現在地の取得**に使う場合、セッションが切れて困るのはたいてい外出先です。自宅のマシンで動かしている場合、そこへ入る手段が無いと再認証できません。

セッションの有効期間は保証されないため、**「いつ切れてもその場で復旧できる」状態を先に作っておくことを強く勧めます。**

用意しておくとよいもの:

- **実行マシンへ外から入る手段。** Tailscale や Tailscale Funnel、あるいは VPN 越しの SSH など。スマートフォンの SSH クライアントから入れれば、外出先でもその場で再認証できます
- **`ssh -t` を付ける癖。** パスワード入力（`getpass`）には端末が必要です。`-t` を忘れると入力できません
- **手元で完結する2FAの受け取り。** 2FA コードは信頼済み Apple 端末に届きます。外出時に持ち歩いている端末が信頼済みになっているか確認しておいてください。自宅に置いてある端末にしか届かない設定だと、外では詰みます

再認証は次の一行で済みます:

```bash
ssh -t <host> '~/Documents/git-cloned/icloud-locate/locate.sh --apple-id your@apple.id'
```

なお、エージェントから非対話で呼んだ場合、セッション切れは**終了コード `2`** で返ります。リトライしても回復しないので、上記の再認証が必要だと判断してください。

## セキュリティとプライバシー

- **これは Apple の非公式 API です。** Apple の仕様変更で動かなくなる可能性があります。公式にサポートされた手段ではないことを理解した上で使ってください
- **Apple ID のパスワードを扱います。** `ICLOUD_PASSWORD` をシェルの履歴やスクリプトに直書きすると漏洩の原因になります。基本はプロンプト入力を使ってください
- **セッションファイルは認証情報そのものです。** `~/.local/state/icloud-locate` を他人と共有したり、git にコミットしたりしないでください
- **位置情報は個人データです。** ログやリポジトリに座標を書き残さない運用を推奨します
- 取得できるのは自分の Apple ID に紐づく端末だけです。他人の端末を追跡する用途には使えませんし、使わないでください

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照してください。

個人利用を想定した実験的なツールです。Apple の非公式 API に依存しているため、予告なく動作しなくなる可能性があります。利用は自己責任でお願いします。
