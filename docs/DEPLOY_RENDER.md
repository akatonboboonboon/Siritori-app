# Neon + Render 公開手順

`render.yaml`、起動時マイグレーション、Neon接続、公開手順は本番へ反映済みです。
2026-07-27に公開URLの`/healthz`と`/readyz`がHTTP 200を返し、PR #24で追加した保護ページが
ログイン画面へ安全に戻ることを確認しました。Windows sandboxのACLエラーで実ブラウザ操作は
できなかったため、認証済み画面とPC・実スマートフォンの目視確認はリポジトリ所有者本人が行います。
Codexは認証情報やDashboardの秘密値を受け取らず、リポジトリや文書へ記録していません。

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

今回の変更を含む自動テストを実行し、成功を確認してからマージします。
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
8. 登録済みアカウントを確認してから`ADMIN_USERNAMES=あかとんぼ`を設定します。
   複数の管理者はカンマで区切ります。まだ「あかとんぼ」を登録していない場合は、変数を空のまま
   一度デプロイしてアプリから登録し、その後に変数を設定して再デプロイします。存在しない名前を
   設定した状態では、本番の起動時検証が安全側に失敗します。
9. Blueprintを適用するか、既存Serviceへ同じ設定を反映してデプロイします。

`PORT`はRenderが設定するため、本人が追加する必要はありません。本番起動時は既定値や
例示用の秘密値が拒否されるため、実値を設定した状態で確認します。

## 5. 初回デプロイを確認する

デプロイログを上から確認します。

1. `pip install -r requirements.txt`が成功
2. Alembicが`0007_final_features`を含む最新版までupgrade
3. NiceGUIが`0.0.0.0:$PORT`で起動
4. `/readyz`がHTTP 200

マイグレーションに失敗した場合、NiceGUIは起動しません。接続文字列や権限を直して再デプロイし、
手作業でテーブルを作らないでください。

`0002_room_discovery`は部屋名キー、公開設定、空席Bot補充設定と一意索引を追加します。
`0003`〜`0005`は戦績、スコアアタック、複数ラウンドを追加し、
`0006_word_suggestions`は一般辞書と分離した単語追加リクエスト表を追加します。
`0007_final_features`は審査監査、承認語、デイリーチャレンジ、チュートリアル完了状態を追加します。
マージ後のRender再デプロイ時に、既存の`DIRECT_DATABASE_URL`を使ってNeonへ自動適用されます。
Neon ConsoleでのSQL・列・索引の手動作成は不要です。管理画面には`ADMIN_USERNAMES`を使います。
リアクションと音・アニメーションはDBマイグレーションや外部アセットを必要としません。

## 6. 公開後の受け入れ確認

発行された`https://...onrender.com`で、最低限次を確認します。

- トップページから辞書にある最初の単語を自由に入力できる
- `林檎`の読みが`りんご`として履歴へ表示される
- 絵文字、未知語、ひらがな・カタカナ1文字が拒否される
- 新規登録、ログイン、ログアウトができる
- ログアウト後に`/lobby`へ直接アクセスするとログインへ戻る
- 再デプロイ後も同じアカウントと保存データが残る
- PCと実スマートフォンで横スクロールや操作不能がない
- 2ブラウザで、部屋作成・対戦・観戦・切断時のBot引継ぎと本人復帰を確認する
- 新規の招待コードが10文字で、短い旧コードを入力する互換性も維持される
- 無効・削除済みの招待URLが部外者へ同じ利用不可表示を返す
- 非公開部屋が公開一覧へ出ず、待機中は正しい招待URLからだけ参加できる
- 観戦を許可した進行中部屋は、公開部屋なら一覧から、非公開部屋なら正しい招待URLから観戦できる
- 進行中部屋には新しいプレイヤーとして参加できず、観戦不許可なら新しい観戦者も参加できない
- 公開の待機部屋と観戦可能な公開進行中部屋だけが一覧へ出る
- 表記ゆれを含む同名部屋が拒否され、元の部屋を削除すると名前を再利用できる
- 1人以上の人間が全員準備した部屋で、空席をNormal Botで補って開始できる
- 2席・3席以上・ソロで降参し、終了・脱落・観戦・手番移動が規則どおりになる
- 降参後に再接続しても選手へ戻らない
- プレイヤーと観戦者が固定5種類のリアクションを相互に確認でき、1秒以内の連打は拒否される
- リアクション後も手番・履歴・タイマーが変わらず、再読み込み後に過去のリアクションが復元されない
- 正解、入力エラー、脱落、勝利／終了で効果音とアニメーションが1回だけ動く
- 効果音OFFと「演出を減らす」を切り替え、次の対局画面にも選択が引き継がれる
- PC・実スマートフォンで、勝敗、成立語数、終了理由、対戦概要、最後の単語を
  リザルトカードから横スクロールなしで確認できる
- ログイン後に`/word-suggestions`から単語・ひらがなの読み・任意補足を申請し、自分の申請だけを一覧できる
- 申請した単語が直ちに対局で使用可能にならず、再デプロイ後も申請行が残る
- 別アカウントから他人の申請を閲覧できず、審査待ち20件の上限を超えて登録できない
- 新規ユーザーは初回だけチュートリアルへ進み、完了後も「遊び方」から再表示できる
- 一般ユーザーに審査リンクが出ず、管理者「あかとんぼ」だけが申請を承認・却下できる
- 承認語は人の入力で使える一方、Bot候補には追加されない
- デイリーは画面を開いただけでは始まらず、1日1回・3分・再開可能な公式記録になる
- 日次ランキングが既存のランキング公開設定を尊重する
- スマートフォン共有とPCコピーの本文に、内部ID、部屋コード、URLが含まれない

公開URLはREADMEへ記録済みで、上記の非認証HTTP確認も完了しています。
PC・実スマートフォンの目視確認後に、秘密情報を写さないスクリーンショットを本人が追加します。
Codex側ではDashboardの秘密値、認証済み画面、実機表示を確認済みとは扱いません。

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
