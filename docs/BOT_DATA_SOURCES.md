# 一般Bot語彙の出典と生成方法

すべての対局のBotは、開発時に生成した
`shiritori/bot_data/words.csv`だけを実行時に読み込みます。
WordNet、TKG、Sudachiの配布元や外部Webサービスへ、対局中に通信することはありません。

## 役割を分けた3つのデータ

### 日本語WordNet 1.1

- 用途: 一般名詞の広い候補集合とsynset由来情報
- 配布元: <https://bond-lab.github.io/wnja/eng/downloads.html>
- 展開後`wnjpn.db` SHA-256:
  `A8E749C4A356BF93D0B5DE505BCA8B21E13746F5728F76819728E8B4C3305A12`
- ライセンス:
  [`licenses/JAPANESE_WORDNET_LICENSE.txt`](../licenses/JAPANESE_WORDNET_LICENSE.txt)

### TKG Japanese-English Learner's Dictionary

- 用途: 学習者向けの粗い自然さ順位と、Basic/Core名詞の補完候補
- リポジトリ: <https://github.com/tkgally/je-dict-1>
- 固定commit:
  `9dd2e89ef86212d249c013d77f843d59b110330c`
- 使用ファイル: `entries_index.json`
- SHA-256:
  `CD7A5D73465118EA484C9809C09DF61E6419C4D34689B5A78ACA3A4CF36B8A4B`
- ライセンス: CC0 1.0 Universal

TKGのデータ本体はこのリポジトリへ同梱していません。TKGはClaudeを中心に作成され、
現在も改善中の学習者向け辞書です。そのため、TKGへの収録だけを理由に単語を実在語として
許可せず、候補と自然さの補助情報に限定して使います。Basic/Core/Generalは粗い分類であり、
特にGeneral内の並びを使用頻度順とは解釈しません。

### SudachiDict-full 20260723

- 用途: 表記、品詞、ひらがな読みの最終検査
- PyPI: <https://pypi.org/project/SudachiDict-full/20260723/>

CSVへ出力する全行は、プレイヤー入力と同じ`LexiconValidator`で完全一致検索します。
最終的な実在語・品詞・読みのauthorityはSudachiです。

## TKG候補の安全条件

TKGから利用する行には、次の条件をすべて適用します。

1. `pos_tags`に`noun`を含む。
2. 表記をアプリと同じ方法でUnicode正規化する。
3. TKG指定読みをひらがなへ正規化する。
4. 表記をSudachiで完全一致検索する。
5. TKG指定読みがSudachiの候補読みに完全一致する。
6. Basic/CoreだけをWordNet外から補完し、GeneralはWordNet候補の順位付けだけに使う。
7. 複数読み語は指定読みがSudachi候補に存在する場合だけ採用し、推測で読みを選ばない。
8. 一文字表記は通常のLexicon規則を適用するため、一文字漢字の実在名詞は許可される。

同じ表記・読みのTKG行でtierが競合するときは
`Basic → Core → General`の順を採用します。同じ読みはしりとり上の同一語として扱うため、
最も上位の1表記だけをCSVへ残します。

## 順位とCSV schema

Botの自然さ順位は次の順です。

1. 応募者が確認したcurated語
2. TKG Basic
3. TKG Core
4. WordNetにも存在するTKG General
5. TKGと完全一致しないWordNet語

各層の中では従来のWordNet生成順位を安定したtie-breakとして維持します。
TKGだけから補完したBasic/Core語は、その層の既存WordNet候補の後ろへ安定追加します。

CSV列は次の4列です。

- `surface`: Botが表示する正規化済み表記
- `reading`: Sudachi候補と一致したひらがな読み
- `source_ref`: `curated`、`wnja:<synset>`、または`tkg:<entry_id>`
- `commonness_tier`: `curated`、`basic`、`core`、`general`、`wordnet`

現在の生成結果は30,643語です。旧テーマseedのうち、一般語彙の表記・読みの一意性を壊さない96 pairも同じSudachi規則で検証して追加しています。

| tier | 語数 |
|---|---:|
| curated | 80 |
| basic | 310 |
| core | 1,195 |
| general | 9,460 |
| wordnet | 19,598 |

## 再生成と一致確認

入力ファイルを別途取得し、次のコマンドで再生成します。

```bash
python -m scripts.build_bot_data \
  --wordnet-db PATH/TO/wnjpn.db \
  --tkg-index PATH/TO/entries_index.json \
  --output shiritori/bot_data/words.csv \
  --theme-output shiritori/theme_data/word_themes.csv
```

`--theme-output`は廃止済み分類データの再現性を開発履歴として保つためだけに残しています。
チェックイン済みの一般CSVと旧分類CSVの両方が再生成結果と完全一致するかは、書き換えずに確認できます。

```bash
python -m scripts.build_bot_data \
  --wordnet-db PATH/TO/wnjpn.db \
  --tkg-index PATH/TO/entries_index.json \
  --output shiritori/bot_data/words.csv \
  --theme-output shiritori/theme_data/word_themes.csv \
  --check
```

生成スクリプトは両入力のSHA-256が上記記録と一致しない場合に停止します。
