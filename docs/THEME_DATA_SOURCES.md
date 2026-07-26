# テーマ単語データの出典

この文書は、アプリに同梱するテーマ単語CSVの出典、作成方法、ライセンス上の表示を記録するものです。

## 日本語WordNet 1.1

- データセット: Japanese WordNet 1.1
- 公式配布案内: <https://bond-lab.github.io/wnja/eng/downloads.html>
- 使用したSQLiteデータベース:
  <https://github.com/bond-lab/wnja/releases/download/v1.1/wnjpn.db.gz>
- 取得日: 2026-07-24
- 配布ファイルのSHA-256:
  `64A14DCFE3BA296566E91A70A2FC0616E85CF2EE7B7FD8CDCBC66C8B12A505A5`
- 公式ライセンス: <https://bond-lab.github.io/wnja/license.txt>
- リポジトリ内のライセンス写し:
  [`licenses/JAPANESE_WORDNET_LICENSE.txt`](../licenses/JAPANESE_WORDNET_LICENSE.txt)

テーマ単語CSVは、上記データベースから開発時に抽出したものです。アプリの実行時には日本語WordNet、GitHub、Wikipediaその他の外部サービスへ通信せず、同梱済みCSVだけを読み込みます。

## 開発時の再生成手順

1. 公式URLから`wnjpn.db.gz`を取得し、作業用ディレクトリへ展開します。ダウンロードした`.gz`と展開後のデータベースはGitへ追加しません。
2. 日本語WordNetの候補をSudachiで検査し、Wikipedia pageviewsで順位付けした作業用JSONを作ります。

   ```bash
   python -m scripts.rank_wordnet_themes --wordnet-db PATH --output ranked.json
   ```

3. 順位付けした候補へ固定の重複・誤分類除外ルールを適用し、アプリ用CSVを生成します。

   ```bash
   python -m scripts.build_theme_data --ranked-json ranked.json --output-dir shiritori/theme_data
   ```

4. 全テストを実行します。

   ```bash
   python -m unittest discover -s tests -v
   ```

`ranked.json`も開発時だけ使う中間生成物であり、Gitへの追加は不要です。Wikipedia pageviewsは時間とともに変化し、将来の再生成では単語の順位や採用結果が変わる可能性があります。そのため、通常は同梱済みCSVを不用意に再生成しません。再生成が必要な場合は、生成されたCSVのGit差分を応募者本人が目視レビューし、誤分類や不自然な語がないことを確認します。

## 今回同梱するデータ

- 日本語WordNetから自動生成した9テーマのCSV: 765語
- ユーザーが`food`へ手入力で追加した単語: 3語
- 実行時の登録: ユーザー追加分を含む`food` 123語と、その他8テーマ

使用時のクレジットは次のとおりです。

> [日本語ワードネット （1.1版）© 2009-2011 NICT, 2012-2015 Francis Bond and 2016-2024 Francis Bond, Takayuki Kuribayashi](https://bond-lab.github.io/wnja/index.ja.html)

## 一般Bot語彙

テーマを指定しない対局のBotは、日本語WordNet 1.1から開発時に抽出して
SudachiDict-core 20260428で検査した27,781語を使用します。

- 日本語WordNet上で日本語の名詞として登録されている
- 表記が1〜16文字のひらがな・カタカナ・漢字・許可記号だけで構成される
- プレイヤー入力と同じSudachi完全一致判定を通過する
- 読みが1通りに確定する普通名詞である
- 同じ読みは先に採用した1語だけを残す

展開後の`wnjpn.db`のSHA-256は
`A8E749C4A356BF93D0B5DE505BCA8B21E13746F5728F76819728E8B4C3305A12`
です。次のコマンドで同じCSVを再生成できます。

```bash
python -m scripts.build_bot_data \
  --wordnet-db PATH/TO/wnjpn.db \
  --output shiritori/bot_data/words.csv
```

生成済みCSVだけをアプリへ同梱するため、Botの実行時に外部通信は発生しません。

## Wikipedia pageviewsの扱い

Wikipedia pageviewsは、日本語WordNetから抽出した候補を一般的な語から順に並べるためのランキング用途だけに使用しています。WikipediaやWikipedia pageviewsはテーマ語彙の出典ではなく、そこから新しい単語や読みを追加しません。

## ユーザーが手入力した単語

次の3語は日本語WordNetからの抽出データとは別枠で、ユーザーが自分で入力したテーマ単語です。

- 林檎（りんご）
- 蜜柑（みかん）
- 西瓜（すいか）

この3語については、ユーザー入力であることが分かるように、生成されたテーマ単語CSVとは分けて管理します。
