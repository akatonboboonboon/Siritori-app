# 廃止済みテーマ単語データの出典と生成履歴

テーマ機能は2026-07-26に廃止しました。現在のアプリは
`shiritori/theme_data/word_themes.csv`を読み込まず、すべての対局で一般辞書を使います。
この文書と旧CSVは、採用した出典・生成方法・AI活用の開発履歴を残すための資料です。
日本語WordNet、Sudachi、GitHub、Wikipediaなどへ対局中に通信することもありません。

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

## 廃止前に使用した統一CSV（履歴）

`word_themes.csv`は、1つの`surface,reading`へ複数テーマを付けられる疎な対応表です。
表にない一般Bot語は「指定なし」だけで利用できます。列は次の5つです。

- `surface`: 正規化済みの完全一致表記
- `reading`: Sudachiで確認したひらがな読み
- `theme_ids`: 定義順に`|`で連結した1個以上のテーマID
- `source_kind`: `auto`または`reviewed`
- `source_ref`: 自動分類規則の版（現在`wnja:1.1-compatible-roots-v2`）、旧WordNet synset、または人手確認ID

`auto`行は、一般Bot語彙の同じ`surface,reading`が必ず存在しなければなりません。
`reviewed`行は、一般語彙と別の読みを意図的に保てるため、一般CSVにない組も許可します。
ただし生成時に全件をSudachiの完全一致表記と指定読みで再検証します。

現在の生成結果は7,371 exact pairs、9,144 theme membershipsです。

| source_kind | exact pairs |
|---|---:|
| auto | 6,671 |
| reviewed | 700 |
| 合計 | 7,371 |

reviewed 700件のうち113件は一般Bot CSVにないテーマ専用pairです。テーマ別件数と
構造的dead-end率は次のとおりです。dead-end率は「ん」で終わらない語を分母にし、
ゲーム本体と同じ小書きかな・長音処理をした末尾から始まる同テーマ語が0件の割合です。

| ID | 表示名 | pairs | dead-end |
|---|---|---:|---:|
| food | 食べ物・飲み物 | 928 | 1.956% |
| animal | 動物 | 819 | 1.799% |
| plant | 植物 | 631 | 0% |
| sport | スポーツ | 95 | 47.826% |
| country | 国・地域 | 92 | 35.000% |
| instrument | 楽器 | 116 | 27.500% |
| vehicle | 乗り物 | 277 | 28.571% |
| fruit | 果物・木の実 | 121 | 27.885% |
| vegetable | 野菜・きのこ | 77 | 31.944% |
| person_job | 人物・職業 | 2,726 | 0% |
| nature | 自然 | 1,534 | 0.491% |
| place_building | 場所・建物 | 445 | 1.312% |
| body | 体・体の部位 | 499 | 3.741% |
| clothing | 服・身につけるもの | 255 | 10.331% |
| daily_tools | 道具・生活用品 | 260 | 11.441% |
| music | 音楽・楽器 | 269 | 10.480% |

## 自動分類の安全条件

`words.csv`の`source_ref`は、再生成を安定させる最小synsetの由来情報です。
表記の意味を選んだ証拠ではないため、テーマ判定には使いません。各表記について
日本語WordNetの全noun sensesを列挙し、テーマごとに独立して次を確認します。

1. 少なくとも1 senseが、そのテーマのtarget rootへ到達する。
2. 全noun sensesが、そのテーマ専用の固定`compatible roots`のどれかへ到達する。
3. blocklist、person subtree除外、multi-label hierarchy gateを通る。

`compatible roots`はtarget rootsと別の定数として固定します。将来target rootやテーマを
追加しても、既存compatible rootsを明示変更しない限り既存テーマの許容範囲は広がりません。

追加7テーマのtarget rootsと固定compatible rootsは同じ次のtupleです。コード上では
別々に宣言し、一方の編集が他方を暗黙に広げないようにしています。

| ID | fixed roots |
|---|---|
| person_job | `00007846-n`, `09632518-n`, `00582388-n` |
| nature | `00015388-n`, `00017222-n`, `09287968-n`, `09225146-n`, `11425580-n`, `11524662-n`, `09239740-n` |
| place_building | `08574314-n`, `09287968-n`, `09225146-n`, `02913152-n` |
| body | `05220461-n` |
| clothing | `03051540-n` |
| daily_tools | `03563967-n`, `03405265-n`, `03528263-n`, `04516672-n` |
| music | `07020895-n`, `07037465-n`, `03800933-n` |

`animal`と`nature`は`00007846-n` person subtreeを明示除外します。
`person_job`は人物を分類するテーマなので、この除外を適用しません。

### automatic multi-label

自動multi-labelは、次の監査済み階層だけをedgeとして許可します。edgeはタグを推論して
追加する規則ではなく、各テーマで独立に適格となったタグ同士の併記を許可する規則です。

- `food`、`plant`、`fruit`、`vegetable`からなる植物・食品family
- `nature`と`animal`、`plant`、`fruit`、`vegetable`
- `nature`と`place_building`
- `place_building`と`country`
- `music`と`instrument`

複数タグがこれらのedgeで連結しない場合は排他的な意味として自動採用しません。
たとえば`food|plant|fruit|nature`のように中間タグを含む正当なchainは許可します。
旧9テーマの自動・reviewedタグをanchorとして先に保持し、無関係な新タグだけを落とすため、
新テーマ追加によって旧タグが消えることはありません。旧9タグだけを
`surface\0reading\0theme_ids\n`としてUnicode順に投影した2,872 recordsのSHA-256は
`AC252ED28CDCB30215E06F6DA0053F3907483A6D945AF9CAC2F5CD184465F32F`
で固定し、回帰テストで確認します。

## blocklistとreviewed seed

テーマごとのblocklistは`shiritori/theme_rules.py`にあり、WordNet対応がroot条件を満たしても
UI上不適切な既知の多義語・taxonomy mismatchを除外します。追加7テーマでは、監査した
`ご存じ`、`母`、`可動`、`縫合`、`防水`、`主事`、`前文`などをテーマ別に拒否します。

旧`food.csv`など9枚のCSV（765 rows、694 unique pairs）は、実行時には読みません。
目視確認済みのbuild-time seedとしてだけ使い、同じexact pairのテーマをunionします。
追加7テーマ用の個別CSVは作りません。`reviewed_additions.csv`もbuild-time入力であり、
生成後はすべて`word_themes.csv`へ統合されます。これにより旧seedは、一般語彙と読みが
競合する110件を含めて保持されます。

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

## 廃止前の編集手順（履歴）

1. `shiritori/theme_rules.py`へtarget roots、別宣言のfixed compatible roots、blocklistを追加・変更する。
2. 正当なmulti-labelだけを許可edgeへ加える。テーマ間の継承や推測には使わない。
3. `shiritori/user_themes.py`へIDと表示名を追加する。
4. 人手確認済みexact pairだけが必要な場合は`reviewed_additions.csv`へ根拠ID付きで追加する。
5. 次節のコマンドで一般CSVと統一テーマCSVを同時生成し、旧投影hash、件数、代表語、deny、dead-end率をレビューする。

旧9枚の個別CSVは互換seed専用です。新テーマのruntime編集先として増やしません。

## 再生成と一致確認

一般Bot CSVと統一テーマCSVを同じコマンドで再生成します。入力DBとTKG indexの
SHA-256が記録値と違う場合は停止します。

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
