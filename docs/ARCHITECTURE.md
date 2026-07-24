# アーキテクチャ

この文書は、課題提出と共同開発を無料枠の範囲で続けるための構成を定めます。
最初から大規模な分散システムにはせず、次の2サービスを基本にします。

- **Render Free Web Service**: NiceGUIアプリを1プロセスで実行する
- **Neon Free PostgreSQL**: アカウント、部屋、対局状態、単語履歴を永続化する

Redisや常駐ワーカーは最初の構成には追加しません。必要性が実測できた段階で導入します。

## 全体構成

```text
PC / スマートフォン
        |
        | HTTPS + NiceGUI WebSocket
        v
Render Free Web Service
  NiceGUI / FastAPI
  ├─ 認証・認可
  ├─ ゲームルール
  ├─ RoomHub（接続中だけのメモリ状態）
  └─ 対局タイマー
        |
        | TLS付きPostgreSQL接続
        v
Neon Free PostgreSQL
  ├─ users / sessions
  ├─ rooms / members / seats
  └─ games / moves / themes
```

NiceGUIは1つのUvicorn workerで動かします。無料プランでは水平スケールを前提にせず、
同時接続数やメモリ使用量を確認しながら機能を増やします。

## PostgreSQLを正本にする

対局に関する次の情報はPostgreSQLを唯一の正本（authoritative source）にします。

- ユーザーとログインセッション
- 部屋の設定、参加者、プレイヤーと観戦者の役割
- 手番、先攻、現在の単語、終了状態、制限時刻
- 受理済み単語の履歴
- 一人用Bot戦の中断データ

単語の送信は1トランザクションで処理します。ゲーム行をロックし、手番、接続条件、
重複、制限時刻、状態バージョンを再確認してから履歴追加と次手番への更新を同時に
確定します。ブラウザから送られた「現在の手番」や「残り時間」は信用しません。

### RoomHubの役割

`RoomHub` はRenderプロセス内の軽量なメモリ構造です。

- 部屋ごとの接続中NiceGUI clientを管理する
- DBコミット後の更新をプレイヤーと観戦者へ通知する
- タイマーやBot処理を予約する
- 同一ユーザーの複数タブを数える

RoomHubはキャッシュ兼通知経路であり、対局データの保存場所ではありません。
Renderの再起動で消えても、PostgreSQLから復元できることを必須条件にします。
単一インスタンスの間はRedisを使わず、将来複数インスタンスへ拡張するときにだけ
共有presenceとPub/Subの導入を検討します。

## 認証とセッション

パスワードは平文や復号可能な形式で保存せず、Argon2idでハッシュ化して
PostgreSQLへ保存します。ログイン成功時には暗号学的に安全なランダム値から
セッションIDを発行します。

- ブラウザには `HttpOnly`、`Secure`、`SameSite=Lax` のCookieを設定する
- DBにはセッションIDそのものではなく、そのハッシュ、ユーザーID、有効期限、
  失効日時を保存する
- ログアウト時とパスワード変更時にセッションを失効させる
- ログイン、部屋参加、単語送信の各処理でサーバー側の認可を行う
- 観戦者には単語送信や部屋設定変更を許可しない

NiceGUIの `storage_secret` はCookie署名に必要なため、安定した秘密値を環境変数で
渡します。ただし、`app.storage.user` やブラウザ内データだけをログイン状態の正本には
しません。保護されたページは、NiceGUIのclient IDを発行する前に認証を確認します。

ユーザー名、部屋名、入力単語は長さと文字種をサーバー側で検証し、HTMLとして
直接描画しません。ログイン試行には回数制限と待ち時間を設け、エラーメッセージから
登録済みユーザー名を推測できないようにします。

## 切断、再接続、復旧

WebSocketの切断は、一時的な回線変更やスマートフォンのスリープでも発生します。
そのため、切断イベントだけで即座に敗北や退室を確定しません。

1. 切断を検出したら短い再接続猶予に入る
2. 同じユーザーの有効な接続が0のまま猶予を過ぎたら離脱を確定する
3. 対戦中の席は一時Botへ引き継ぐ
4. 本人が戻ったら同じユーザーIDを確認し、その席の次の操作権を返す
5. 既にDBへ確定したBotの手は巻き戻さない

再接続した画面は、メモリ上の差分通知に依存せず、必ずDBから部屋のスナップショットと
単語履歴を再取得します。更新処理には状態バージョンと一意な操作IDを持たせ、再送された
同じ操作が二重登録されないようにします。

制限時間は「残り秒数」ではなくUTCの `deadline_at` をDBへ保存します。画面上の
カウントダウンは表示用で、判定はサーバー時刻で行います。Renderが停止してタイマー処理が
動かなかった場合も、次回起動時または次の入力時に期限を再評価します。一人用Bot戦は
ユーザー不在時に一時停止し、残り時間と進捗をDBへ保存します。

## データベース接続

Neonでは実行時とマイグレーションで接続先を分けます。

| 環境変数 | 用途 |
|---|---|
| `DATABASE_URL` | 通常のアプリ処理。Neonのpooled connection stringを使う |
| `DIRECT_DATABASE_URL` | Alembicなどのスキーママイグレーション。direct connection stringを使う |

通常処理は多数の短いトランザクションになるため、Neonのpooler経由で接続します。
SQLAlchemy側のpoolも無料枠に合わせて小さく保ち、接続を無制限に作りません。
マイグレーションはDDLや接続単位の処理を含むため、poolerを通さずdirect URLを使います。
両方ともNeonが発行するTLS設定を含む接続文字列をそのまま秘密情報として管理します。

Render Freeではpre-deploy commandを利用できないため、将来Alembicを追加するときは、
起動前の専用Pythonコマンドが `DIRECT_DATABASE_URL` で `alembic upgrade head` 相当を
実行し、成功した場合だけNiceGUIを起動する構成にします。マイグレーション失敗時は
古いコードのまま無理に起動しません。

## デプロイ環境変数

Render Dashboardには少なくとも次を登録します。値はリポジトリ、`render.yaml`、
README、ログへ書きません。

| 変数 | 内容 |
|---|---|
| `DATABASE_URL` | Neonのpooled runtime URL |
| `DIRECT_DATABASE_URL` | Neonのdirect migration URL |
| `NICEGUI_STORAGE_SECRET` | NiceGUIのCookie署名用ランダム秘密値 |
| `SESSION_SECRET` | アプリ独自セッション用ランダム秘密値 |
| `APP_ENV` | 本番では `production` |

`PORT` はRenderが自動設定するため手動登録しません。DB接続文字列や秘密値をローカルで
使う場合は、Git対象外の `.env` に置きます。NeonはRenderからの通信遅延を抑えるため、
選択可能ならRenderのSingaporeに近いリージョンを選びます。

## 無料枠での制約

この構成は課題提出、デモ、小規模な共同開発を対象としています。

- Render Freeは一定時間受信がないとスピンダウンし、次回アクセスでコールドスタートする
- WebSocketはデプロイ、保守、回線変更、スマートフォンのスリープで切断され得る
- Renderのローカルファイルとメモリは永続化されない
- プロセス停止中はBotやタイマーのPython処理も停止する
- Neon Freeもアイドル時のcompute起動待ち、保存容量、compute時間、同時接続数などの
  無料枠上限があり、上限や条件は将来変更される可能性がある
- RenderとNeonの両方が休止している場合、最初の画面表示は通常より長くなる
- 無料構成では可用性保証や常時稼働を前提にしない

これらを理由に、ローカルSQLite、JSONファイル、NiceGUIのメモリstorageへ重要データを
保存しません。サービス停止中にも時間そのものを進める必要がある処理は、DBの絶対時刻を
使って復旧時に計算します。公開前や発表前には、URLへ一度アクセスして両サービスを
起動させます。

## PC・スマートフォンへの影響

PCとスマートフォンは同じNiceGUIページとAPIを利用します。端末別のゲーム状態は持たず、
レスポンシブCSSだけで表示を調整します。

- スマートフォンでは縦1列、PCではゲーム盤と履歴の2列を基本にする
- タップ対象を十分な大きさにし、入力欄がソフトウェアキーボードで隠れないようにする
- タブのバックグラウンド化や端末スリープ後は自動再接続し、DBから再同期する
- カウントダウンは復帰時に `deadline_at` から再計算し、端末内タイマーを信用しない
- 低速回線でも履歴全件を毎秒送らず、確定イベント時と再接続時だけ同期する

## 将来の拡張条件

同時接続数や複数インスタンスの必要性が確認されるまでは、Redisを追加しません。
Renderを複数インスタンスへ増やす場合は、各プロセスのRoomHubだけでは通知が届かないため、
共有Pub/Subと期限付きpresenceを追加します。その場合も、Redisは通知と一時状態専用とし、
PostgreSQLを正本のまま維持します。

## 参考資料

- [NiceGUI: Storage](https://nicegui.io/documentation/storage)
- [NiceGUI: Actions, Events and Tasks](https://nicegui.io/documentation/section_action_events)
- [NiceGUI: Security Best Practices](https://nicegui.io/documentation/section_security)
- [Render: Deploy for Free](https://render.com/docs/free)
- [Render: WebSockets](https://render.com/docs/websocket)
- [Render: Deploysとpre-deploy command](https://render.com/docs/deploys)
- [Neon: Connect from any application](https://neon.com/docs/connect/connect-from-any-app)
- [Neon: Connection pooling](https://neon.com/docs/connect/connection-pooling)
- [Neon: Plans](https://neon.com/docs/introduction/plans)
- [SQLAlchemy: AsyncSession](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [PostgreSQL: `SELECT`](https://www.postgresql.org/docs/current/sql-select.html)
