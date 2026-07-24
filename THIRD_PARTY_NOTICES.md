# 第三者ソフトウェアと辞書データ

このプロジェクトは、実在語と読みを判定するために次の依存関係を使用します。
依存パッケージ本体や辞書バイナリはこのリポジトリへ複製せず、`requirements.txt` で
固定した版をPyPIからインストールします。

## SudachiPy 0.6.11

- 提供者: Works Applications
- 用途: Sudachi辞書の完全一致検索と辞書項目の取得
- ライセンス: Apache License 2.0
- PyPI: <https://pypi.org/project/SudachiPy/0.6.11/>
- ソース: <https://github.com/WorksApplications/sudachi.rs/tree/develop/python>

## SudachiDict-core 20260428

- 提供者: Works Applications
- 用途: 表記、読み、品詞を含む日本語core辞書
- パッケージのライセンス表記: Apache License 2.0
- PyPI: <https://pypi.org/project/SudachiDict-core/20260428/>
- ソース: <https://github.com/WorksApplications/SudachiDict>
- 辞書素材の詳細: <https://github.com/WorksApplications/SudachiDict/blob/develop/LEGAL>

SudachiDictは複数の辞書素材を基に構築されています。再配布方法を変更したり、
独自辞書をリポジトリへ含めたりする場合は、上記の `LEGAL` と各素材の条件を
あらためて確認します。
## 日本語WordNet 1.1

- 提供者: NICT、Francis Bond、Takayuki Kuribayashi
- 用途: テーマ別単語候補の開発時抽出
- 公式配布案内: <https://bond-lab.github.io/wnja/eng/downloads.html>
- 使用したデータベース:
  <https://github.com/bond-lab/wnja/releases/download/v1.1/wnjpn.db.gz>
- 取得日: 2026-07-24
- SHA-256:
  `64A14DCFE3BA296566E91A70A2FC0616E85CF2EE7B7FD8CDCBC66C8B12A505A5`
- 公式ライセンス: <https://bond-lab.github.io/wnja/license.txt>
- ライセンス写し:
  [`licenses/JAPANESE_WORDNET_LICENSE.txt`](licenses/JAPANESE_WORDNET_LICENSE.txt)
- 詳細なデータ作成記録:
  [`docs/THEME_DATA_SOURCES.md`](docs/THEME_DATA_SOURCES.md)

[日本語ワードネット （1.1版）© 2009-2011 NICT, 2012-2015 Francis Bond and 2016-2024 Francis Bond, Takayuki Kuribayashi](https://bond-lab.github.io/wnja/index.ja.html)

テーマ単語CSVは開発時に抽出済みで、アプリ実行時の外部通信はありません。

## SQLAlchemy 2.0.51

- 用途: PostgreSQL / SQLite のトランザクションとORM
- ライセンス: MIT
- PyPI: <https://pypi.org/project/SQLAlchemy/2.0.51/>

## Alembic 1.18.5

- 用途: PostgreSQLスキーマのマイグレーション
- ライセンス: MIT
- PyPI: <https://pypi.org/project/alembic/1.18.5/>

## argon2-cffi 25.1.0

- 用途: Argon2idによるパスワードハッシュ
- ライセンス: MIT
- PyPI: <https://pypi.org/project/argon2-cffi/25.1.0/>

## Psycopg 3.3.4

- 用途: Neon PostgreSQLへの接続
- ライセンス: GNU Lesser General Public License v3
- PyPI: <https://pypi.org/project/psycopg/3.3.4/>