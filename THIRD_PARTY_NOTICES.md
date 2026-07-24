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