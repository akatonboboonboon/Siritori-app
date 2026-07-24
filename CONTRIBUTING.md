# 共同開発ガイド

このリポジトリは、応募者本人と OpenAI Codex が同じ GitHub リポジトリを使い、変更内容と担当範囲を確認しながら開発するためのものです。GitHub を唯一の共有元とし、ローカルだけに残した変更や、誰が行ったか分からない変更を作らないことを基本方針とします。

## 絶対に守ること

1. 初期リポジトリ作成後は `main` へ直接 commit / push しない
2. 作業ごとに短命なブランチを作り、Pull Request（PR）経由で統合する
3. 作業開始前に、担当者・目的・編集予定ファイルを Issue または Draft PR で宣言する
4. 同じファイルを複数人・複数エージェントが同時に編集しない
5. API キー、パスワード、Cookie、Render の Deploy Hook、個人情報などの秘密情報を commit しない
6. AI を使った変更は、同じ PR 内で `AI_USAGE.md` に記録する
7. 動作確認できていないものを「完成」「公開済み」と記載しない

## ブランチ

ブランチは最新の `main` から作成し、1つの目的だけを扱います。

- ユーザー本人の実装: `user/<issue番号>-<短い説明>`
- Codex の実装: `agent/<短い説明>`
- 不具合修正: `fix/<issue番号>-<短い説明>`
- 文書のみ: `docs/<issue番号>-<短い説明>`

例:

```text
user/12-hiragana-validation
agent/nicegui-layout
fix/24-reset-after-game-over
docs/31-readme-deployment
```

ブランチは PR のマージ後に削除します。レビュー開始後の force push は、レビュー履歴を追いにくくするため原則として行いません。

## 担当ファイルの宣言

作業開始時に Issue または Draft PR へ、次の形式で記載します。

```md
## 作業宣言

- 担当者: ユーザー / Codex
- 目的:
- 編集予定ファイル:
  - `path/to/file.py`
- 編集しないファイル:
  - `path/to/other.py`
- 完了条件:
- AI利用予定: なし / あり（用途を記載）
```

すでに他の担当者が同じファイルを宣言している場合は、先にその変更をマージするか、ファイルを分割してから着手します。やむを得ず同じファイルを変更する場合は、同時進行せず作業順を決めます。

## 標準的な作業手順

`main` が作成済みであることを確認してから、次の流れで進めます。

```bash
git switch main
git pull --ff-only origin main
git switch -c agent/nicegui-layout
```

1. 担当範囲を宣言する
2. 小さな単位で実装・確認・commitする
3. 早い段階でリモートへ push し、Draft PR を作る
4. 変更が増えたら PR 本文と `AI_USAGE.md` を更新する
5. 自動テストと手動操作を行う
6. Draft を解除し、担当者以外がレビューする
7. 確認済みの PR だけを `main` へマージする

ユーザーは Codex のブランチへ直接 push せず、レビューコメントまたは別ブランチで修正を提案します。Codex も、明示的な依頼なしにユーザーのブランチへ push しません。

## Draft PR

大きな実装が完了するまで待たず、最初の意味のある commit 後に Draft PR を作ります。PR 本文には最低限、次を記載します。

```md
## 変更内容

## 担当
- [ ] ユーザー本人が実装
- [ ] Codex が生成・実装
- [ ] Codex の案をユーザー本人が修正

## 編集したファイル

## AIを使った範囲

## 確認方法と結果

## スクリーンショット
<!-- UI変更時 -->

## マージ前チェック
- [ ] 必須仕様への影響を確認した
- [ ] ローカルで起動した
- [ ] 関連テストが成功した
- [ ] ブラウザで手動操作した
- [ ] `AI_USAGE.md` を更新した
- [ ] 秘密情報を含まない
```

## commit の粒度

1 commit は、レビュー時に1つの変更として説明できる大きさにします。機能追加、リファクタリング、文書変更を無関係に混ぜません。

例:

```text
chore: add NiceGUI project setup
feat(rules): reject words ending with ん
feat(ui): show played-word history
test(rules): cover duplicate word game over
chore(render): add production start configuration
docs: record AI-assisted setup work
```

AI が生成または大幅に修正した commit には、必要に応じて次の trailer を付けられます。

```text
AI-assisted-by: OpenAI Codex
```

ただし、AI を人間の共同作者として見せる目的で `Co-authored-by` を使いません。AI 利用の正式な記録は `AI_USAGE.md` と PR 本文です。

## 競合が起きた場合

- 作業を止め、どちらの変更が先かを PR 上で確認する
- 機能の担当者が競合を解消する
- `ours` / `theirs` を一括適用せず、差分を1か所ずつ確認する
- 解消後は双方の機能を再テストする
- 競合解消のために他者の変更を削除した場合は、PR 本文へ明記する

## 秘密情報

次の情報は GitHub へ push しません。

- `.env` の実値
- GitHub、Render、その他サービスのアクセストークン
- Render の Deploy Hook URL
- パスワード、Cookie、秘密鍵
- 個人情報や応募に不要なローカルパス

設定値が必要な場合は環境変数を使い、リポジトリにはダミー値だけを記載した `.env.example` を置きます。誤って公開した場合は、単に履歴から削除するだけでなく、該当するキーを直ちに無効化・再発行し、担当者へ共有します。

## レビューと公開

マージ前に、少なくとも次を確認します。

- しりとりの必須仕様を壊していない
- 追加機能の正常系・異常系を確認した
- 別タブまたは別利用者の状態が混ざらない
- PC幅とスマートフォン幅で主要操作ができる
- Render 用の秘密情報が含まれていない
- READMEへ記載する内容と実際の動作が一致している

Render の本番サービスは原則として `main` をデプロイ元にします。PR ブランチの未確認コードを本番扱いにせず、デプロイ成功と実際のゲーム操作を確認してから公開URLを記録します。
