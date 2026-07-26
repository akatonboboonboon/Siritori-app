# テーマ単語データの出典と生成方法

実行時の9テーマは、`shiritori/theme_data/word_themes.csv`を1回だけ読み込みます。
日本語WordNet、Sudachi、GitHub、Wikipediaなどへ対局中に通信しません。

## 入力データ

### 日本語WordNet 1.1

- 公式配布案内: <https://bond-lab.github.io/wnja/eng/downloads.html>
- 使用したSQLiteデータベース:
  <https://github.com/bond-lab/wnja/releases/download/v1.1/wnjpn.db.gz>
- 取得日: 2026-07-24
- 展開後`wnjpn.db` SHA-256:
  `A8E749C4A356BF93D0B5DE505BCA8B21E13746F5728F76819728E8B4C3305A12`
- 公式ライセンス: <https://bond-lab.github.io/wnja/license.txt>
- リポジトリ内のライセンス写し:
  [`licenses/JAPANESE_WORDNET_LICENSE.txt`](../licenses/JAPANESE_WORDNET_LICENSE.txt)

一般Bot語彙の出典、TKGの固定commit、Sudachiの役割は
[`BOT_DATA_SOURCES.md`](BOT_DATA_SOURCES.md)に記録しています。

## 実行時の統一CSV

`word_themes.csv`は、1つの`surface,reading`に0個以上のテーマを付ける疎な対応表です。
表にない一般Bot語は「指定なし」だけで利用できます。列は次の5つです。

- `surface`: 正規化済みの完全一致表記
- `reading`: Sudachiで確認したひらがな読み
- `theme_ids`: 定義順に`|`で連結した1個以上のテーマID
- `source_kind`: `auto`または`reviewed`
- `source_ref`: 自動分類規則の版、旧WordNet synset、または人手確認ID

`auto`行は、一般Bot語彙の同じ`surface,reading`が必ず存在しなければなりません。
`reviewed`行は、一般語彙と別の読みを意図的に保てるため、一般CSVにない組も許可します。
ただし生成時に全件をSudachiの完全一致表記と指定読みで再検証します。
これにより、旧9 CSVの694 unique pairsを、一般語彙と読みが競合する110件も含めて落としません。

現在の生成結果は2,872 exact pairsです。

| source_kind | exact pairs |
|---|---:|
| auto | 2,172 |
| reviewed | 700 |
| 合計 | 2,872 |

reviewed 700件のうち113件は一般Bot CSVにないテーマ専用pairです。テーマ別件数は
multi-labelを各テーマへ1件ずつ数えます。

| theme | pairs |
|---|---:|
| food | 928 |
| animal | 819 |
| plant | 631 |
| sport | 95 |
| country | 92 |
| instrument | 116 |
| vehicle | 277 |
| fruit | 121 |
| vegetable | 77 |

## 自動分類の安全条件

`words.csv`の`source_ref`は、再生成を安定させる最小synsetの由来情報です。
表記の意味を選んだ証拠ではないため、テーマ判定には使いません。各表記について
日本語WordNetの全noun sensesを列挙し、テーマごとに独立して次を確認します。

1. 少なくとも1 senseが、そのテーマのtarget rootへ到達する。
2. 全noun sensesが、そのテーマ専用の固定`compatible roots`のどれかへ到達する。
3. blocklist、人を動物に含めない規則、multi-label review gateを通る。

`compatible roots`はtarget rootsと別の定数として固定します。将来target rootやテーマを
追加しても、既存compatible rootsを明示変更しない限り既存テーマの許容範囲は広がりません。

- `food`、`plant`、`fruit`、`vegetable`は、植物・食品系family内だけ相互互換です。
- `animal`はanimal rootだけを許可し、`00007846-n` person subtreeを明示除外します。
- `sport`、`country`、`instrument`、`vehicle`は、それぞれ自身の固定rootだけを許可します。
- 自動multi-labelは単一tagか植物・食品系family内だけを採用し、それ以外は要レビューとして自動除外します。

たとえば`バス`の楽器senseと乗り物sense、`スカッシュ`のスポーツsenseと野菜senseは、
any-senseだけでは誤分類を生むため自動採用しません。後述のreviewed exact-pairだけが
このgateを上書きできます。

## reviewed seedと明示語

旧`food.csv`など9枚のCSV（765 rows、694 unique pairs）は、実行時には読みません。
目視確認済みのbuild-time seedとしてだけ使い、同じexact pairのテーマをunionします。
`reviewed_additions.csv`もbuild-time入力であり、生成後はすべて`word_themes.csv`へ統合されます。

ユーザー指定のfood語は次の6件です。

- 林檎（りんご）
- 蜜柑（みかん）
- 西瓜（すいか）— `food|plant|fruit`
- ユーリンチー（ゆーりんちー）
- 油淋鶏（ゆーりんちー）
- 湯豆腐（ゆどうふ）

SudachiDict-full 20260723には`油淋鶏/ユーリンチー`がありますが、かな表記
`ユーリンチー`の完全一致見出しはありません。その1件だけ、`LexiconValidator`が
`ユーリンチー -> 油淋鶏/ゆーりんちー`を狭い明示aliasとして検証します。類似検索や
未知語の一般的な救済には使いません。

多義語をテーマ文脈で明示したreviewed overrideは次の2件です。

- バス（ばす）— `vehicle`
- スカッシュ（すかっしゅ）— `sport|vegetable`

## 再生成と一致確認

旧9 CSVをbuild-time seedとして残したまま、一般Bot CSVと統一テーマCSVを同じコマンドで
再生成します。入力DBとTKG indexのSHA-256が記録値と違う場合は停止します。

```bash
python -m scripts.build_bot_data \
  --wordnet-db PATH/TO/wnjpn.db \
  --tkg-index PATH/TO/entries_index.json \
  --output shiritori/bot_data/words.csv \
  --theme-output shiritori/theme_data/word_themes.csv
```

チェックイン済み2ファイルとの完全一致を、書き換えずに確認できます。

```bash
python -m scripts.build_bot_data \
  --wordnet-db PATH/TO/wnjpn.db \
  --tkg-index PATH/TO/entries_index.json \
  --output shiritori/bot_data/words.csv \
  --theme-output shiritori/theme_data/word_themes.csv \
  --check
```

使用時のクレジットは次のとおりです。

> [日本語ワードネット （1.1版）© 2009-2011 NICT, 2012-2015 Francis Bond and 2016-2024 Francis Bond, Takayuki Kuribayashi](https://bond-lab.github.io/wnja/index.ja.html)
