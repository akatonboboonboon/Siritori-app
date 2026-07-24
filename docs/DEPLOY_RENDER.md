# Render への公開手順

このアプリは、ブラウザとの通信にPythonサーバーとWebSocketを使うNiceGUIアプリです。
Renderでは **Static SiteではなくWeb Service** として公開します。

リポジトリ直下の `render.yaml` は、次の設定を宣言しています。

- PythonのWeb Service
- 日本から比較的近いSingaporeリージョン
- 無料インスタンス
- `requirements.txt` を使った依存関係のインストール
- `python main.py` による起動
- `/healthz` によるHTTPヘルスチェック
- 連携ブランチのGitHub Actions成功を契機とする自動デプロイ

## 公開前に確認するファイル

### `requirements.txt`

ローカルとRenderで同じ動作を再現できるよう、テスト済みのNiceGUIを固定します。
このプロジェクトでは次のバージョンを前提にします。

```text
nicegui==3.14.0
```

NiceGUI以外の外部パッケージをコードから直接importする場合は、それらも
`requirements.txt` に記載してください。Python標準ライブラリは記載不要です。

### `.python-version`

このリポジトリではPython 3.13系を指定しています。
Renderは `.python-version` にパッチ番号がない場合、利用可能な最新の3.13系を使います。
ローカルでもPython 3.13系を使うと、環境差による問題を減らせます。

### `main.py`

RenderのWeb Serviceは、ホスト `0.0.0.0` とRenderが設定する環境変数 `PORT`
にバインドする必要があります。`PORT` をリポジトリや `render.yaml` に固定値として
設定する必要はありません。

```python
import os

from nicegui import app, ui


@app.get('/healthz')
def healthz() -> dict[str, str]:
    return {'status': 'ok'}


# ここに画面やイベント処理を定義する


if __name__ == '__main__':
    ui.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', '8080')),
        reload=False,
        show=False,
    )
```

- `8080` はローカル実行時だけ使うフォールバックです。
- `reload=False` は本番で不要なファイル監視を止めます。
- `show=False` はRender上でブラウザを開こうとする処理を止めます。
- `/healthz` はログイン不要かつ軽量にし、5秒以内に `2xx` または `3xx` を返します。
- NiceGUIにはUvicornベースのサーバーが含まれるため、この構成ではGunicornや
  複数workerを追加せず `python main.py` で起動します。

## ローカル確認

PowerShellでは、Renderに近いポート設定で次のように確認できます。

```powershell
$env:PORT = '10000'
python main.py
```

別のPowerShellからヘルスチェックを確認します。

```powershell
Invoke-WebRequest http://127.0.0.1:10000/healthz
```

ステータスコード `200` が返り、`http://127.0.0.1:10000/` でゲームを操作できれば
公開前の基本確認は完了です。確認後は必要に応じて環境変数を削除します。

```powershell
Remove-Item Env:PORT
```

## GitHubとRenderの連携

1. Draft PRをレビューし、自動テスト成功後に `main` へマージします。
2. GitHubの `main` ブランチに `render.yaml` があることを確認します。
3. [Render Dashboard](https://dashboard.render.com/) にログインします。
4. GitHubを **Git Provider** としてRenderに連携します。公開リポジトリのURLを
   貼り付けるだけの方式では、自動デプロイを利用できません。
5. Renderで **New > Blueprint** を選び、このGitHubリポジトリを指定します。
6. Renderがリポジトリ直下の `render.yaml` を読み取ったことを確認します。
7. 作成内容を確認してBlueprintを適用します。
8. デプロイログで依存関係のインストール、`python main.py` の起動、
   ポート検出、`/healthz` の成功を確認します。
9. 発行された `https://...onrender.com` のURLでゲームを確認します。

`autoDeployTrigger: checksPass` により、連携ブランチのGitHub Actionsが成功した
コミットだけが自動デプロイされます。共同作業では各自のブランチで変更し、レビュー後に
連携ブランチへマージすると、コード共有と公開を同じGit履歴で管理できます。

## 秘密情報をコミットしない

APIキー、パスワード、Cookie署名用キー、データベース接続文字列などは、
ソースコード、`render.yaml`、README、スクリーンショットへ直接書かないでください。

- ローカルでは `.env` などGitの追跡対象外のファイルを使います。
- `.env` が `.gitignore` の対象であることを、コミット前に確認します。
- RenderではDashboardの **Environment** から環境変数として登録します。
- Blueprintに変数名だけを宣言する場合は、値を書かず `sync: false` を使います。
- 誤って公開した秘密情報は、Gitから消すだけでなく発行元で直ちに失効・再発行します。

例:

```yaml
envVars:
  - key: EXAMPLE_SECRET
    sync: false
```

## 無料プランの注意点

- HTTPリクエストまたはWebSocketメッセージを15分間受信しないとスピンダウンします。
- 次のアクセスで再起動しますが、コールドスタートに約1分かかることがあります。
- 無料Web Serviceは512 MB RAM、0.1 CPUです。重い起動処理や大きなデータの
  一括展開は避けてください。
- 無料利用時間はワークスペース全体で月750時間です。
- ローカルファイルシステムは一時的です。再デプロイ、再起動、スピンダウンで、
  SQLite、アップロードファイル、サーバー側のゲーム履歴などの変更は失われます。
- 無料Web Serviceには永続ディスクを追加できません。
- メモリ上の対局状態もプロセス再起動時にリセットされます。
- リポジトリに含めた読み取り専用の辞書ファイルや画像は、各デプロイで復元されます。
- Renderは無料インスタンスを本番サービス向けには推奨していません。
  課題提出やデモ用途として利用し、必要になったら永続ストレージや有料プランを検討します。

発表や審査の直前は、公開URLへ一度アクセスして起動を確認してください。

## 一次資料

- [NiceGUI: `ui.run`](https://nicegui.io/documentation/run)
- [NiceGUI: Pages & Routing](https://nicegui.io/documentation/section_pages_routing)
- [Render: Web Servicesとポートバインド](https://render.com/docs/web-services)
- [Render: Blueprint YAML Reference](https://render.com/docs/blueprint-spec)
- [Render: Health Checks](https://render.com/docs/health-checks)
- [Render: Deploy for Free](https://render.com/docs/free)
- [Render: Python Version](https://render.com/docs/python-version)
- [Render: WebSockets](https://render.com/docs/websocket)
