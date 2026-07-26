# Neon + Render 公開手順

`render.yaml`、起動時マイグレーション、公開手順は準備済みです。ただし、本番デプロイ、
既存Dashboardの設定、公開URLはまだ確認していません。Windows sandboxのACLエラーで
Codexの実ブラウザ接続も使えなかったため、画面の目視確認はリポジトリ所有者本人が行います。
Codexは認証情報を受け取らず、秘密情報をリポジトリや文書へ記録していません。

## 公開構成

- Render Free Web Service: NiceGUI/FastAPIを1 workerで実行
- Python: `.python-version`でCIと同じ3.13系に固定
- Neon Free PostgreSQL: ユーザー、セッション、部屋、対局、履歴の正本
- 起動: `python -m scripts.start`
- 起動前処理: `alembic upgrade head`
- Liveness: `/healthz`
- DB readiness: `/readyz`

RenderのローカルSQLiteやメモリは永続化先に使いません。

## 1. マージ前のローカル確認

```bash
python -m pip install -r requirements.txt
python -m pip check
python -m unittest discover -s tests -v
python -m compileall -q .
```

最終統合後の全298件の自動テストが成功しています。
同じテストを各PRとmainのGitHub Actionsでも再実行します。

ローカル起動:

```bash
python -m scripts.start
```

<http://127.0.0.1:8080/healthz>と<http://127.0.0.1:8080/readyz>がどちらも
HTTP 200になることを確認します。

## 2. Neonプロジェクトを確認する

1. [Neon Console](https://console.neon.tech/)へ本人のアカウントでログインします。
2. 既存のFreeプロジェクトを使うか、なければ新規作成します。
3. 新規作成時に選択できる場合は、RenderのSingaporeに近いリージョンを選びます。
4. Connection Detailsから、同じDBに対する次の2種類の接続文字列を取得します。

| Renderの変数名 | Neonで取得する文字列 |
|---|---|
| `DATABASE_URL` | pooled connection string（ホスト名に`-pooler`を含むもの） |
| `DIRECT_DATABASE_URL` | direct connection string（非pooler） |

Neonが表示するTLS指定を含む文字列をそのまま使います。接続文字列は`.env.example`、
Issue、PR、README、スクリーンショットへ貼り付けません。

## 3. GitHubの変更をレビューする

1. CodexのDraft PRで差分とCI結果を確認します。
2. 秘密情報が含まれないことを確認します。
3. 本人担当のPRと競合がないことを確認します。
4. 内容を理解して採用すると決めた場合だけ、順番に`main`へマージします。

Codexは明示的な許可なしにPRをマージしません。

## 4. Render Serviceを設定する

1. [Render Dashboard](https://dashboard.render.com/)へ本人のアカウントでログインします。
2. GitHubをGit Providerとして連携します。
3. 既存Web Serviceを使う場合は対象リポジトリとブランチを確認します。新規作成なら
   **New > Blueprint**から`akatonboboonboon/Siritori-app`を選びます。
4. `render.yaml`どおりFree/SingaporeのWeb Serviceが1つ設定されることを確認します。
5. DashboardのEnvironmentへ次の2値を貼り付けます。
   - `DATABASE_URL`: Neon pooled URL
   - `DIRECT_DATABASE_URL`: Neon direct URL
6. `APP_ENV=production`になっていることを確認します。
7. `NICEGUI_STORAGE_SECRET`と`SESSION_SECRET`はBlueprintの`generateValue: true`で
   別々に生成されることを確認します。手動設定する場合も、それぞれ32文字以上の別の値にします。
8. Blueprintを適用するか、既存Serviceへ同じ設定を反映してデプロイします。

`PORT`はRenderが設定するため、本人が追加する必要はありません。本番起動時は既定値や
例示用の秘密値が拒否されるため、実値を設定した状態で確認します。

## 5. 初回デプロイを確認する

デプロイログを上から確認します。

1. `pip install -r requirements.txt`が成功
2. Alembicが`0001`以降の最新版までupgrade
3. NiceGUIが`0.0.0.0:$PORT`で起動
4. `/readyz`がHTTP 200

マイグレーションに失敗した場合、NiceGUIは起動しません。接続文字列や権限を直して再デプロイし、
手作業でテーブルを作らないでください。

## 6. 公開後の受け入れ確認

発行された`https://...onrender.com`で、最低限次を確認します。

- トップページから辞書にある最初の単語を自由に入力できる
- `林檎`の読みが`りんご`として履歴へ表示される
- 絵文字、未知語、ひらがな・カタカナ1文字が拒否される
- 新規登録、ログイン、ログアウトができる
- ログアウト後に`/lobby`へ直接アクセスするとログインへ戻る
- 再デプロイ後も同じアカウントと保存データが残る
- PCと実スマートフォンで横スクロールや操作不能がない
- 2ブラウザで、本人実装の部屋作成・対戦・観戦・切断復帰を確認する

公開確認が終わってから、READMEの「公開URL」とスクリーンショットを本人が追加します。
Codex側ではDashboardも公開画面も確認済みとは扱いません。

## 7. シークレットの確認・更新

Render DashboardとNeon Console以外に実値を残しません。

- `DATABASE_URL`
- `DIRECT_DATABASE_URL`
- `NICEGUI_STORAGE_SECRET`
- `SESSION_SECRET`

漏えいした場合はGitの行を消すだけでは不十分です。Neonのパスワードまたは接続文字列を再発行し、
Renderの該当値も更新します。セッション秘密値を変更すると、既存Cookieは無効になります。

## マイグレーション運用

スキーマ変更時は、コードと同じPRでAlembic revisionを追加します。

```bash
python -m alembic upgrade head
python -m alembic check
```

本番のDDLには`DIRECT_DATABASE_URL`だけを使い、通常のアプリ処理にはpooledの
`DATABASE_URL`だけを使います。Freeプランではpre-deploy commandが使えないため、
`python -m scripts.start`がupgrade成功後にだけアプリを開始します。

## 無料枠の注意

- Render Freeはアイドル時に停止し、最初のアクセスでコールドスタートする場合があります。
- デプロイ、保守、スマートフォンのスリープでWebSocketが切断される場合があります。
- Renderのファイルとプロセスメモリは永続ではありません。
- Neonも無料枠の容量、compute時間、同時接続などの上限があります。
- 公開条件と無料枠は変更され得るため、公開日に公式ページを本人が再確認してください。

発表や審査の直前は、一度公開URLへアクセスし、`/readyz`と主要操作を確認します。

## 一次資料

- [Render: Deploy for Free](https://render.com/docs/free)
- [Render: Blueprint YAML Reference](https://render.com/docs/blueprint-spec)
- [Render: Setting Your Python Version](https://render.com/docs/python-version)
- [Render: Health Checks](https://render.com/docs/health-checks)
- [Render: WebSockets](https://render.com/docs/websocket)
- [Neon: Connection pooling](https://neon.com/docs/connect/connection-pooling)
- [Neon: Pricing](https://neon.com/pricing)
- [Alembic Tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
