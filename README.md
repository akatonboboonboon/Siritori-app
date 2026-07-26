# しりとり

Pythonと[NiceGUI](https://nicegui.io/)で作る、PC・スマートフォン対応のしりとりアプリです。
[jig.jp サマーインターンシップ2026 Webコースの選考課題](https://jigintern.github.io/summer-2026-assignment/)
を土台に、辞書判定、ログイン、オンライン部屋、観戦、Bot対戦のサーバー基盤を実装しています。

## 公開URL

- Render: https://siritori-app-4myd.onrender.com/

RenderとNeonの無料枠を使う設計です。公開アカウントの作成と秘密情報の登録は、
リポジトリ所有者が行います。

## 現在できること

### 画面から確認できる機能

- 最初の単語を自由に入力
- SudachiDictによる実在する名詞の完全一致判定
- 漢字・カタカナを辞書のひらがな読みに変換して接続判定
- 絵文字、未知語、不正な文字、ひらがな・カタカナ1文字の拒否
- 複数読みを自動決定せず、読み選択ダイアログを表示
- 表記と読みを含む履歴
- 「ん」と同じ読みの再使用による敗北・複数人戦での脱落
- Bot数・Easy/Normal/Hard・テーマ・制限時間を選べる1人用Bot戦
- 参加コード、準備状態、脱落後の観戦に対応したオンライン対戦部屋
- 切断時のBot代行と、復帰時の本人への安全な引き継ぎ
- オンライン対戦中の単語・手番・履歴のリアルタイム同期
- 新規登録、ログイン、ログアウト、一時停止したBot戦一覧の保護画面
- PCと幅320px以上のスマートフォン向けレスポンシブ表示

### テスト済みのサーバー基盤

- Argon2idパスワードハッシュと、DBにSHA-256だけを保存する不透明セッション
- HttpOnly / SameSite Cookie、CSRF署名、Origin検証
- 正規化ユーザー名とIPの両方に対する、有効期限付き・上限付きの登録／ログイン回数制限
- リクエスト本文の上限とArgon2id処理の同時実行数制限
- PostgreSQL/SQLiteスキーマとAlembicマイグレーション
- Neon PostgreSQLを正本にするユーザー、部屋、役割、対局、履歴のSQL永続化
- ソロBot戦を対局と同じ厳密な`Game`スナップショットで保存・一時停止・再開
- 部屋コード、定員、準備、所有者移譲、観戦、開始済み対局検索を扱うロビーAPI
- 部屋単位ロック、状態バージョン、操作IDと意味的フィンガープリントによる再送対策
- プレイヤー・観戦者権限、複数タブpresence、15秒の再接続猶予
- 切断・プロセス再起動後の不在猶予、Bot引継ぎ、安全な手番境界での本人復帰
- 人間が0人の対人部屋削除、Bot戦の一時停止・復元
- 暗号学的に安全なランダム先攻、自由初手、3〜180秒または無制限
- Easy / Normal / 2手先を読むHard Botと、大語彙でも即答できる索引・探索キャッシュ
- 日本語WordNet、TKG、Sudachiで開発時に検査した一般Bot語彙28,750語
- 選択テーマを「表記＋読み」で照合するサーバー側テーマ制約
- 日本語WordNet 1.1から開発時に候補を抽出し、Sudachiで実在語と読みを検査した9テーマ（アプリ実行時の外部通信なし）

タイマーの残り秒数を継続更新する演出と最終デザイン調整は、応募者本人の
実装領域として残しています。本人のコードを別PRで共有・レビューできる構成です。

## ルール

採用ルールの正本は[`docs/RULES.md`](docs/RULES.md)です。主なルールは次のとおりです。

1. ランダムに選ばれた先攻が、辞書にある好きな単語から開始する
2. 2手目以降は、直前の単語の「読み」の末尾からつなぐ
3. 小書きかなは通常サイズ、長音は直前の母音として扱う
4. 末尾が「ん」なら、その単語を履歴に残して送信者が敗北・脱落する
5. 同じ正規化読みを再使用したら、重複語を履歴へ足さず送信者が敗北・脱落する
6. 3人以上では敗者だけが観戦へ回り、最後の1人になった時点で終了する
7. 読みが複数ある場合は、プレイヤーが明示的に選ぶ

## 構成

```text
NiceGUI / FastAPI
├─ public game + authentication pages
├─ LexiconValidator / GameSession
├─ Lobby / RoomCoordinator / RoomHub / Bot strategies
└─ SQLAlchemy repositories / Alembic
             │
             └─ Neon PostgreSQL（本番の正本）
```

詳しい設計は[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、実装順と担当境界は
[`docs/ROADMAP.md`](docs/ROADMAP.md)を参照してください。

## 使用技術

- Python 3.13
- NiceGUI 3.14.0
- SudachiPy 0.6.11 / SudachiDict-core 20260428
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

SudachiDict-coreの初回ダウンロードは約70MBあるため、インストールに時間がかかる場合があります。

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
最終統合後の全256件の自動テストを通過しています。
同じテストは各PRとmainのGitHub Actionsでも再実行します。

## Render + Neonへの公開

リポジトリには`render.yaml`とマイグレーション起動処理を含めています。
Dashboard上の秘密情報登録はリポジトリ所有者が行います。秘密情報は受領・記録していません。
必要な値は次の4つです。

| 環境変数 | 値 |
|---|---|
| `DATABASE_URL` | Neonのpooled connection string |
| `DIRECT_DATABASE_URL` | Neonのdirect connection string |
| `NICEGUI_STORAGE_SECRET` | 32文字以上のランダム値 |
| `SESSION_SECRET` | 上とは別の32文字以上のランダム値 |

`render.yaml`の起動コマンドは`python -m scripts.start`、ヘルスチェックはDB接続も確認する
`/readyz`です。実際のNeon/Render設定手順と公開後チェックは
[`docs/DEPLOY_RENDER.md`](docs/DEPLOY_RENDER.md)を参照してください。

## 応募者本人が仕上げる部分

AIが安全性・同時実行制御の基盤を担当し、次は応募者本人が実装・判断します。

- 9テーマに収録した単語の目視レビューと誤分類修正
- Easy / Normal / Hardの強さと収録語のプレイ感レビュー
- タイマー警告表示と本人らしいデザイン調整
- PC・実スマートフォンと公開URLでの最終操作確認
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
- [テーマ単語データの出典と作成方法](docs/THEME_DATA_SOURCES.md)
- [一般Bot語彙の出典と生成方法](docs/BOT_DATA_SOURCES.md)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/en/20/)
- [Alembic Documentation](https://alembic.sqlalchemy.org/en/latest/)
- [Render: Deploy for Free](https://render.com/docs/free)
- [Render: WebSockets](https://render.com/docs/websocket)
- [Neon: Connection pooling](https://neon.com/docs/connect/connection-pooling)
