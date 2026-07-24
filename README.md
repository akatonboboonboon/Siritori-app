# しりとり

Python と [NiceGUI](https://nicegui.io/) で作る、ブラウザで遊べるしりとりアプリです。
jig.jp サマーインターンシップ2026 Webコースの選考課題仕様に沿って実装しています。

## 公開URL

- Render: デプロイ完了後に、実在するURLをここへ追記します

> Render の無料インスタンスは、しばらくアクセスがないと停止します。最初の表示に時間がかかる場合があります。

## 遊び方

1. 画面に表示されている「いまのことば」の最後のひらがなを確認します。
2. そのひらがなから始まる、2文字以上のことばを入力します。
3. Enter キーまたは「つなぐ」ボタンで送信します。
4. 「ん」で終わることば、または一度使ったことばを入力するとゲーム終了です。
5. ゲーム中も終了後も「もう一度」からリセットできます。

## 実装した機能

### 必須機能

- 直前のことばを表示
- 任意のことばを入力
- 前のことばの末尾と、入力したことばの先頭が一致した場合だけ更新
- 一致しない場合に、必要な先頭文字を含むエラーを表示
- 「ん」で終わることばでゲーム終了
- 使用済みのことばでゲーム終了
- ゲーム中・終了後の両方で使えるリセット

### 追加機能

- ひらがな以外を受け付けない入力チェック
- 1文字だけの入力を受け付けない入力チェック
- ことばの履歴と、つないだ回数を表示
- 「ゃ・ゅ・ょ・っ」などで終わった場合、通常の大きさのかなとして次の文字を判定
- PCとスマートフォンの両方で使えるレスポンシブ表示
- ブラウザごとに独立したゲーム状態（他のプレイヤーの操作と混ざらない）

## 次の段階として実装中の機能

採用ルール、PC・スマートフォンの画面別合格条件、Render＋Neonの無料構成を
[`docs/RULES.md`](docs/RULES.md)、[`docs/ROADMAP.md`](docs/ROADMAP.md)、
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) に固定しました。

現在の開発ブランチには、SudachiDict coreの完全一致項目から普通名詞・固有名詞だけを調べ、
漢字・カタカナの読み、複数の読み候補、表記違いの共通キーを返す辞書判定基盤があります。
現行のゲーム画面はまだ従来のひらがな判定を使っており、画面への接続と読み選択UIは
次の小さなPRで実装します。

## デザイン

入力すべき文字を迷わないように、現在のことばと次の先頭文字を画面の中心に大きく表示しています。
成功・入力エラー・ゲーム終了は、色だけに頼らず記号と文章でも伝えます。

配色、文言、最初のことばは [`shiritori/customize.py`](shiritori/customize.py)、
見た目は [`assets/styles.css`](assets/styles.css) から変更できます。
この2ファイルは、リポジトリ所有者が自分のデザインへ育てるための担当領域として分離しています。

## 使用技術

- Python 3.13
- NiceGUI 3.14.0
- SudachiPy 0.6.11（辞書判定基盤）
- SudachiDict core 20260428（固定した日本語辞書）
- Python 標準ライブラリ `unittest`
- Render Web Service

## ローカルでの実行方法

Python 3.10 以上を用意し、リポジトリのルートで次を実行します。

```bash
python -m venv .venv
```

macOS / Linux:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

初回の依存関係インストールでは、SudachiDict core（約70MB）も取得するため時間がかかることがあります。

起動後、<http://localhost:8080> を開きます。

## テスト

ゲームルールは画面から分離しているため、ブラウザを起動せずにテストできます。

```bash
python -m unittest discover -s tests -v
```

手動確認では、少なくとも次を試します。

- `しりとり → りんご → ごりら` と正しくつながる
- `しりとり` に `すいか` を入力すると更新されずエラーになる
- `しりとり` に `りぼん` を入力するとゲーム終了になる
- 一度使ったことばを再入力するとゲーム終了になる
- ゲーム中とゲーム終了後の両方でリセットできる
- カタカナ、英数字、空入力、1文字入力が拒否される

## Renderへのデプロイ

`render.yaml` を含めているため、Render の **Web Service** として GitHub リポジトリを接続できます。
詳しい手順は [`docs/DEPLOY_RENDER.md`](docs/DEPLOY_RENDER.md) に記載します。

- Build Command: `pip install -r requirements.txt`
- Start Command: `python main.py`
- Health Check Path: `/healthz`

## 共同開発

このリポジトリは、ユーザーとAIの担当範囲が分かるように短いブランチとDraft PRで作業します。
同じファイルを同時に編集しないためのルールは [`CONTRIBUTING.md`](CONTRIBUTING.md)、
AIの利用履歴は [`AI_USAGE.md`](AI_USAGE.md) を参照してください。
辞書依存関係の出典とライセンスは [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) に記録しています。

### リポジトリ所有者が次に担当する部分

- `shiritori/customize.py` のタイトル、説明、最初のことば
- `assets/styles.css` の配色・余白・文字サイズなどのデザイン
- 自分で考えた追加機能を最低1つ
- スマートフォン幅を含む手動テスト
- このREADMEの「デザイン」「工夫した点」「学んだこと」を自分の言葉で更新
- 公開後のURLとスクリーンショット

## AIの活用方法

初期基盤の作成に OpenAI Codex を使用しました。今回AIを利用した範囲は次のとおりです。

- 課題ページから必須仕様とREADME要件を整理
- NiceGUIの画面構成、ゲーム状態の分離、Render向け起動設定を提案・実装
- Unicode正規化、小書きかな、重複、「ん」終了を扱うゲームロジックを実装
- 自動テストの初期ケースを作成
- 共同作業用のファイル分割とドキュメントを作成
- 採用ルール、段階的ロードマップ、Render＋Neonの無料構成を文書化
- SudachiDictを用いる辞書判定基盤と境界テストを実装

AIが生成した内容は、そのまま提出せず、コードを読み、手動テストと自動テストを行い、
採用・修正した内容を [`AI_USAGE.md`](AI_USAGE.md) に追記します。

### AIを使わず自分で実装・判断した部分

共同作業を始めるための欄です。実際に自分で行った内容だけを、作業後に追記してください。

- （これから追記）

## 参考にしたWebサイト

- [jig.jp サマーインターンシップ2026 選考課題](https://jigintern.github.io/summer-2026-assignment/)
  - 必須仕様、追加機能数、提出READMEの要件を確認
- [NiceGUI Documentation](https://nicegui.io/documentation/)
  - UI部品、ページ、`ui.run` の設定を確認
- [NiceGUI: ui.run](https://nicegui.io/documentation/run)
  - ホスト、ポート、本番起動オプションを確認
- [Render: Web Services](https://render.com/docs/web-services)
  - `0.0.0.0` と環境変数 `PORT` へのバインドを確認
- [Render: Blueprint YAML Reference](https://render.com/docs/blueprint-spec)
  - `render.yaml` のサービス設定を確認
- [Render: Setting Your Python Version](https://render.com/docs/python-version)
  - `.python-version` によるPythonバージョン指定を確認
- [SudachiPy 0.6.11](https://pypi.org/project/SudachiPy/0.6.11/)
  - 完全一致辞書検索、対応Python、配布サイズ、ライセンスを確認
- [SudachiDict](https://github.com/WorksApplications/SudachiDict)
  - core辞書の由来とライセンス情報を確認
- [Neon: Connection pooling](https://neon.com/docs/connect/connection-pooling)
  - 無料PostgreSQLの実行時接続設計を確認
