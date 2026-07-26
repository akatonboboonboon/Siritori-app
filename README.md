# しりとり

Pythonと[NiceGUI](https://nicegui.io/)で作る、PC・スマートフォン対応のしりとりアプリです。
[jig.jp サマーインターンシップ2026 Webコースの選考課題](https://jigintern.github.io/summer-2026-assignment/)
を土台に、辞書判定、ログイン、オンライン部屋、観戦、Bot対戦、戦績・ランキング、
スコアアタック、終了後の再戦・部屋継続、リアクション、結果演出、
審査制の単語追加リクエストを実装しています。

## 公開URL

- Render: https://siritori-app-4myd.onrender.com/

RenderとNeonの無料枠を使う設計です。公開アカウントの作成と秘密情報の登録は、
リポジトリ所有者が行います。

## 現在できること

### 画面から確認できる機能

- 最初の単語を自由に入力
- [SudachiDict-fullとWikipedia監査](docs/LEXICON_DATA_SOURCES.md)に基づく実在する名詞の完全一致判定
- 漢字・カタカナを辞書のひらがな読みに変換して接続判定
- 絵文字、未知語、不正な文字、ひらがな・カタカナ1文字の拒否
- 複数読みを自動決定せず、読み選択ダイアログを表示
- 表記と読みを含む履歴
- 「ん」で終わる単語と、同じ読みの再使用による敗北・複数人戦での脱落
- Bot数・Easy/Normal/Hard・制限時間を選べる1人用Bot戦
- 参加コード、準備状態、脱落後と途中参加の観戦に対応したオンライン対戦部屋
- 非公開を既定にした部屋公開設定、安全な10文字コードの招待URL、公開部屋一覧
  （待機中は参加、観戦許可された進行中の部屋は観戦）
- 非公開の進行中部屋も、正しい招待URL／参加コードを知るユーザーだけが観戦可能
- 正規化後に同名となる未削除部屋の作成拒否と、削除後の部屋名再利用
- 人数不足を定員までNormal Botで補って始める設定
- 確認ダイアログ付きの降参（複数人戦では脱落・観戦へ移行）
- 複数人戦で脱落者が受け持っていた開始文字を次の生存者へ引き継ぐ進行
- 切断時のBot代行と、復帰時の本人への安全な引き継ぎ
- オンライン対戦中の単語・手番・履歴のリアルタイム同期
- 新規登録、ログイン、ログアウト、一時停止したBot戦一覧、戦績、ランキングの保護画面
- 固定3分のスコアアタックと、同じサーバー期限を保った進行中ランの再開
- Bot戦終了後は同じ設定で再挑戦し、対人戦終了後は同じ参加者・招待URLを保った
  待機部屋へ戻って、新しい参加者を加えて次の試合を開始
- 対局中のプレイヤーと観戦者が送れる固定リアクション（👍／👏／😮／😂／🔥）
  （同じ部屋・同じユーザーでは1秒に1回まで。履歴や戦績には保存しない）
- 正解・入力エラー・脱落・勝利／終了の効果音とアニメーション、および
  効果音OFF・「演出を減らす」設定
- 勝敗、成立語数、終了理由、対戦概要、最後の単語をまとめたレスポンシブなリザルトカード
- Web Share APIとクリップボードfallbackを使い、内部IDやURLを含めないリザルト共有
- JST基準で全員同じ開始語、1日1回、3分、途中再開対応の日次チャレンジと日次ランキング
- 初回だけ自動表示し、「遊び方」から再閲覧できる4段階チュートリアル
- 単語追加リクエストと、環境変数で指定した管理者だけが使える承認・却下画面
- PCと幅320px以上のスマートフォン向けレスポンシブ表示

### テスト済みのサーバー基盤

- Argon2idパスワードハッシュと、DBにSHA-256だけを保存する不透明セッション
- HttpOnly / SameSite Cookie、CSRF署名、Origin検証
- 正規化ユーザー名とIPの両方に対する、有効期限付き・上限付きの登録／ログイン回数制限
- リクエスト本文の上限とArgon2id処理の同時実行数制限
- PostgreSQL/SQLiteスキーマとAlembicマイグレーション
- Neon PostgreSQLを正本にするユーザー、部屋、役割、対局、履歴のSQL永続化
- 対局終了の状態更新と同じCASトランザクションで、人間の戦績を1対局につき一度だけ保存
- 初期値を非公開にした参加同意式（opt-in）のPvP勝数／スコアアタックランキング
- ソロBot戦を対局と同じ厳密な`Game`スナップショットで保存・一時停止・再開
- 固定したサーバー期限、状態バージョン、履歴からの再計算を使うスコアアタック保存・再開
- 部屋コード、定員、準備、所有者移譲、進行中観戦、開始済み対局検索を扱うロビーAPI
- 終了済みGameを不変で残し、部屋の`current_game_id`だけを新しい対局へ
  差し替える複数ラウンド制御
- 公開待機部屋と観戦可能な公開進行中部屋を返す一覧、および
  NFKC・大文字小文字・空白をそろえた部屋名の一意制約
- 招待参照のアカウント/IP別回数制限、非公開画面の`no-store`、終了済み部屋の再起動回収
- 参加中の人間が全員準備済みなら、空席を定員まで永続的なNormal Botで補う開始処理
- 降参を状態バージョン付きで一度だけ確定し、再接続でも取り消さない対局処理
- 部屋単位ロック、状態バージョン、操作IDと意味的フィンガープリントによる再送対策
- 進行中の観戦参加でロビー会員・対局スナップショット・状態バージョンを同時に更新
- 送信時に最新の部屋役割を確認し、対局状態や履歴を変更せずRoomHubだけで配信する
  期限付き・非永続のリアクション
- 単語追加リクエストを一般辞書・Bot語彙と分離してNeonへ保存し、本人には自分の申請だけを表示
- 審査待ちを1人20件までに制限し、同じ単語と読みの再送を重複登録しない申請処理
- 申請しただけでは対局へ反映せず、環境変数で指定した管理者だけが承認・却下できる審査画面
- 承認語を通常対局の人間入力へ安全に補い、固定したBot語彙と公平性が必要な
  ランキング対象モードには混ぜない境界
- プレイヤー・観戦者権限、複数タブpresence、15秒の再接続猶予
- 切断・プロセス再起動後の不在猶予、Bot引継ぎ、安全な手番境界での本人復帰
- 人間が0人の対人部屋削除、Bot戦の一時停止・復元
- 暗号学的に安全なランダム先攻、自由初手、3〜180秒または無制限
- Easy / Normal / 2手先を読むHard Botと、大語彙でも即答できる索引・探索キャッシュ
- 日本語WordNet、TKG、Sudachiで開発時に検査した一般Bot語彙30,643語
- 新規・再開を問わず、すべてのPvP／ソロ対局はテーマ選択なしで一般辞書を使用
- 以前保存したテーマ付き対局も、再開時はテーマ制約を適用せず、テーマ名を表示しない
- `theme_key`と旧分類データはスキーマ・開発履歴の互換性だけのため一時的に維持し、実行時には使用しない

最終的な文言・配色・PC／実スマートフォンでの表示確認は、応募者本人が判断して
記録できるように残しています。本人のコードも別PRで共有・レビューできる構成です。

## ルール

採用ルールの正本は[`docs/RULES.md`](docs/RULES.md)です。主なルールは次のとおりです。

1. ランダムに選ばれた先攻が、辞書にある好きな単語から開始する
2. 2手目以降は、直前の単語の「読み」の末尾からつなぐ
3. 小書きかなは通常サイズ、長音は直前の母音として扱い、接続時の `ぢ/じ`・`づ/ず` は同一視する
4. 末尾が「ん」なら、その単語を履歴に残して送信者が敗北・脱落する
5. 同じ正規化読みを再使用したら、重複語を履歴へ足さず送信者が敗北・脱落する
6. 3人以上では敗者だけが観戦へ回り、その人が受け持っていた開始文字を次の生存者へ引き継ぐ
7. 最後の1人になった時点で対局を終了する
8. 読みが複数ある場合は、プレイヤーが明示的に選ぶ

## 構成

```text
NiceGUI / FastAPI
├─ public game + authentication pages
├─ LexiconValidator / GameSession
├─ Lobby / RoomCoordinator / RoomHub / Bot strategies
├─ Statistics / ScoreAttack
└─ SQLAlchemy repositories / Alembic
             │
             └─ Neon PostgreSQL（本番の正本）
```

詳しい設計は[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、実装順と担当境界は
[`docs/ROADMAP.md`](docs/ROADMAP.md)を参照してください。

## 使用技術

- Python 3.13
- NiceGUI 3.14.0
- SudachiPy 0.6.11 / SudachiDict-full 20260723
- SQLAlchemy 2.0.51 / Alembic 1.18.5
- Argon2-cffi 25.1.0
- Psycopg 3.3.4
- Neon PostgreSQL / Render Web Service
- Python標準ライブラリ`unittest`

依存関係のライセンスと出典は[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)に
記録しています。

## ローカル実行

Python 3.13を用意し、リポジトリのルートで仮想環境を作ります。

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m scripts.start
```

macOS / Linux:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m scripts.start
```

`python -m scripts.start`は、Alembicを最新版へ上げてからNiceGUIを起動します。
開発時は、Git対象外の`./siritori-dev.db`を自動的な接続先として使えます。起動後に
<http://localhost:8080>を開いてください。

SudachiDict-fullの初回ダウンロードは約127MBあり、展開後の辞書ファイルは
約360MBになるため、インストールに時間と空き容量が必要です。

## テスト

```bash
python -m pip check
python -m unittest discover -s tests -v
python -m compileall -q .
```

マイグレーションだけを確認する場合:

```powershell
$env:DIRECT_DATABASE_URL = 'sqlite+pysqlite:///./migration-check.db'
python -m alembic upgrade head
python -m alembic check
```

GitHub Actionsでも依存整合性、Alembic、構文、全テストをPython 3.13で確認します。
機能別の自動テストに加え、同じ全テストを各PRとmainのGitHub Actionsでも再実行します。

## Render + Neonへの公開

リポジトリには`render.yaml`とマイグレーション起動処理を含めています。
Dashboard上の秘密情報登録はリポジトリ所有者が行います。秘密情報は受領・記録していません。
必要な値は次の5つです。

| 環境変数 | 値 |
|---|---|
| `DATABASE_URL` | Neonのpooled connection string |
| `DIRECT_DATABASE_URL` | Neonのdirect connection string |
| `NICEGUI_STORAGE_SECRET` | 32文字以上のランダム値 |
| `SESSION_SECRET` | 上とは別の32文字以上のランダム値 |
| `ADMIN_USERNAMES` | 単語審査を許可する登録済みユーザー名。複数ならカンマ区切り |

`render.yaml`の起動コマンドは`python -m scripts.start`、ヘルスチェックはDB接続も確認する
`/readyz`です。実際のNeon/Render設定手順と公開後チェックは
[`docs/DEPLOY_RENDER.md`](docs/DEPLOY_RENDER.md)を参照してください。
戦績・ランキング用の`0003_match_statistics`、スコアアタック用の
`0004_score_attack_runs`、複数ラウンド再戦用の`0005_room_current_game`、
単語追加リクエスト用の`0006_word_suggestions`、審査・デイリー・チュートリアル用の
`0007_final_features`を含む追加列・テーブルは、この起動処理がNeonへ自動マイグレーションします。
Neon Consoleでの手作業によるSQL実行は不要です。管理画面を使う場合だけ、
`ADMIN_USERNAMES`へ登録済みユーザー名を設定します。

### 対局終了後の再戦

Bot戦の終了画面では、Bot数・難易度・制限時間をサーバー側で引き継いだ新しい対局を作ります。
対人戦は結果と戦績を確定したまま、同じ参加者・招待URL・公開設定の待機部屋へ自動で戻ります。
準備状態だけを全員OFFへ戻すため、参加者が準備し直し、空席には新しい参加者を加えて
部屋主が次の試合を開始できます。終了済みGameは書き換えず、試合ごとに別の対局IDを使います。

### リアクション・演出・単語追加リクエスト

リアクションは現在その部屋に参加しているプレイヤーと観戦者だけが送れます。
固定した5種類だけを部屋の接続中クライアントへ配信し、対局履歴、戦績、DBには保存しません。
効果音は外部音源を配布せずブラウザで生成し、音量OFFとアニメーション軽減の選択を
ブラウザのユーザー設定へ保存します。OS／ブラウザの`prefers-reduced-motion`も尊重します。

単語追加リクエストはログイン後の保護画面`/word-suggestions`から送信し、自分の申請状態だけを一覧できます。
審査待ちは1人20件までです。申請は一般辞書やBot語彙から分離して保存するため、
送信直後にその単語が対局で使えるようになることはありません。管理者が表記と読みを確認して承認すると、
Sudachiで未収録または対象外の語に限って通常対局の人間入力へ反映します。Bot候補は変更しません。
スコアアタックとデイリーチャレンジは全参加者の条件を固定するため、ルール版で固定した
Sudachi辞書だけを使い、途中の承認によってランキング条件を変えません。

単語審査、デイリーチャレンジ、結果共有、チュートリアルの詳細は
[`docs/FINAL_FEATURES.md`](docs/FINAL_FEATURES.md)を参照してください。

## 応募者本人が仕上げる部分

AIが安全性・同時実行制御の基盤を担当し、次は応募者本人が実装・判断します。

- Easy / Normal / Hardの強さと収録語のプレイ感レビュー
- タイマー警告表示と本人らしいデザイン調整
- PC・実スマートフォンと公開URLでの最終操作・再戦・リザルト・演出確認
- 2ブラウザでのプレイヤー／観戦者リアクションと、単語追加リクエストの保存確認
- READMEの「工夫した点」「学んだこと」「AIを使わず実装した範囲」

共同作業手順は[`CONTRIBUTING.md`](CONTRIBUTING.md)、AIが実装した範囲は
[`AI_USAGE.md`](AI_USAGE.md)に記録します。本人の変更は`user/...`ブランチの小さな
Draft PRにすると、双方の変更をGitHub上で共有できます。

## AIの活用方法

OpenAI Codexを、仕様整理、辞書・状態機械、認証、DB、同時実行制御、テスト、
Render設定の下書きと検証に使用しました。AIが生成したコードをそのまま本人実装とせず、
PR、`AI_USAGE.md`、本人のレビュー・手動確認によって区別します。

### AIを使わず自分で実装・判断した部分

ここには、実際に本人が作業した内容だけを後から追記します。

- （本人の作業後に追記）

## 参考資料

- [jig.jp サマーインターンシップ2026 選考課題](https://jigintern.github.io/summer-2026-assignment/)
- [NiceGUI Documentation](https://nicegui.io/documentation/)
- [SudachiPy 0.6.11](https://pypi.org/project/SudachiPy/0.6.11/)
- [SudachiDict](https://github.com/WorksApplications/SudachiDict)
- [日本語WordNet 1.1](https://bond-lab.github.io/wnja/eng/downloads.html)
- [TKG Japanese-English Learner's Dictionary](https://github.com/tkgally/je-dict-1)
- [廃止済みテーマデータの出典と作成履歴](docs/THEME_DATA_SOURCES.md)
- [一般Bot語彙の出典と生成方法](docs/BOT_DATA_SOURCES.md)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/en/20/)
- [Alembic Documentation](https://alembic.sqlalchemy.org/en/latest/)
- [Render: Deploy for Free](https://render.com/docs/free)
- [Render: WebSockets](https://render.com/docs/websocket)
- [Neon: Connection pooling](https://neon.com/docs/connect/connection-pooling)
