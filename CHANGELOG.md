# Changelog

利用者に影響する主な変更を記録します。
既存の履歴は Git のコミットと `pyproject.toml` のバージョン変更から整理しました。
日付は各バージョンが記録されたコミットの日付であり、公開日を保証するものではありません。
各バージョンの項目には、次のバージョン更新までの変更を含めています。
`Unreleased` は、最新のバージョン更新後の変更です。

## [Unreleased]

## [0.14.2] - 2026-10-21

### Fixed

- `transcribe --suggest-phrases` の保存先を通常の文字起こしと統一しました。余分な `transcript/` 階層を作らず、音声ごとのフォルダ内に文字起こしと `vocabulary/` をまとめます。既存の成果物は自動移動しません。

## [0.14.1] - 2026-10-21

### Fixed

- `transcribe --suggest-phrases` でも `folders.output` と既定の出力先を使えるよう修正しました。`--output-dir` の明示指定は不要です。

## [0.14.0] - 2026-10-21

### Changed

- `prepare` を `transcribe --suggest-phrases` に統合し、単体の `phrases suggest` を `suggest-phrases` に変更しました。旧コマンドは削除しました。候補ファイルを保存したらコマンドは終了します。

- `--profile` と `--config` を対象コマンドの後にも指定可能にし、README の例をコマンド先頭の順序に統一しました。

## [0.13.1] - 2026-10-21

### Changed

- 成果物名とメディアフォルダ名の項目区切りを `__` から `_` に変更しました。例: `meeting_<音声ID>/transcript_cloud_japanese-meeting_<生成ID>.jsonl`。元ファイル名・プロファイル名に含まれる文字は保持します。
- 設定schemaとmanifest schemaは変更しません。更新後は `uv tool install . --reinstall` を実行してください。

## [0.13.0] - 2026-10-21

### Changed

- `transcribe` は出力先ルート内に「元ファイル名＋元メディアSHA-256の先頭12桁」のフォルダを作成します。日本語名を保持し、同名・異内容の入力を分離します。旧出力は自動移動しません。
- local/cloud共通で全編の `audio.flac` を最終成果物として保存します。作業ファイルの削除や `cleanup` 後も残り、同じメディアの別プロファイルでは検証して共有します。
- 文字起こしは `transcript__<mode>__<profile>__<fingerprint先頭12桁>.*` に統一しました。設定・用語・ツール版の変更で新しい結果を追加し、過去の結果を残します。
- manifest schema 4にFLACのハッシュ・サイズ、音声トラックと変換条件を記録し、成果物パスを相対化しました。旧schema 1～3の検証は継続します。
- `prepare` の `transcript/` 内にも同じメディア単位の保存構成を適用します。`--overwrite` は同じ生成IDの文字起こしの修復に使い、共有FLACを上書きしません。
- 設定schemaの変更はありません。更新後は `uv tool install . --reinstall` で再インストールしてください。

## [0.12.0] - 2026-10-21

### Added

- `prepare` を追加しました。文字起こしと用語候補生成を一括実行し、人による用語確認で停止します。成果物は指定先の `transcript/` と `vocabulary/` に保存します。
- 生成設定を文字起こし前に検査し、候補生成失敗後は完成済みの文字起こしを再利用できます。既存の用語候補は上書きしません。
- `prepare --dry-run` による通信・保存なしの確認と、`--json` による両工程の結果出力に対応しました。

## [0.11.0] - 2026-10-20

### Added

- Azure用語リストにUTF-8外部ファイルを指定できる `phrase_list.files` と、実行ごとに追加する `transcribe --phrase-file` に対応しました。重複を除いて上限を検査し、用語の変更を再認識・再開判定に反映します。
- `phrases suggest` を追加しました。Tkn GenAI Bridgeを使って文字起こしJSONLから用語候補を生成し、引用照合済みの理由・時刻と編集用の `phrases.txt` を保存します。人による確認後に明示的に再文字起こしできます。
- `generation` 設定と `--generation-profile`、参考文書の `--context-file`、通信も保存もしない `--dry-run` に対応しました。確認済みファイルを守るため、候補出力は新規フォルダに限ります。

- `transcribe --json` を追加し、文字起こし結果を機械可読な JSON で取得できるようにしました。
- この CHANGELOG を追加しました。

### Changed

- 設定schemaを `3.1.0` にしました。既存の `3.0.x` は編集せず使えます。空の `phrase_list:` は用語指定なしとして扱います。

- Azure Speech Fast Transcription の受付上限を公式仕様に合わせ、音声は2時間未満から5時間未満、送信用FLACは250 MB未満から500 MB未満へ緩和しました。送信前の確認は維持し、localの処理条件は変更していません。

- 新規ノートにダブルクォート付きの `created` を追加しました。設定の生成・移行で日時とWindowsパスの引用符を統一し、設定読込時の日時表記と小数秒精度を保持します。

- `config show` を `config list` に変更し、既定表示を 1 行に 1 つの `key=value` 形式にしました。
  従来の JSON 出力を使うスクリプトは `config list --json` に変更してください。
  更新後は `uv tool install . --reinstall` で再インストールしてください。設定ファイルの変更は不要です。

- `transcribe` の標準出力を、既定では読みやすいテキスト形式に変更しました。
  従来の JSON 出力を利用するスクリプトでは `--json` の指定が必要です。
- README を日本語に統合し、phrase list の用途とベンチマークの評価条件を明確化しました。

### Fixed

- 設定例を `src/audio_transcriber/resources/config.example.yaml` に配置し、
  `config init` が参照するリソースのパスを修正しました。

## [0.10.0] - 2026-09-05

### Added

- Azure Speech の不適切語フィルター設定と phrase list に対応しました。
- Azure 認証情報のキャッシュと、破損した認証レコードからの復旧処理を追加しました。

## [0.9.0] - 2026-09-05

### Changed

- Azure 認証を `InteractiveBrowserCredential` に変更し、ブラウザーでアカウントを
  選択してサインインする方式にしました。

## [0.8.0] - 2026-09-05

### Changed

- Azure Speech へのアップロード形式を WAV から可逆圧縮のモノラル 16 kHz FLAC に
  変更し、送信用ファイルの生成と検証を追加しました。

## [0.7.1] - 2026-09-05

### Fixed

- Windows で CUDA を使用する際、文字起こし前に必要なランタイム DLL を検証し、
  不足時に設定方法を案内するようにしました。

## [0.7.0] - 2026-09-04

### Changed

- 出力ファイル名に実行 mode と profile を含め、設定ごとの成果物を区別できるようにしました。
- manifest のスキーマをバージョン 3 に更新し、profile 情報を記録するようにしました。

### Fixed

- ローカル文字起こしの時刻が音声チャンクの範囲を超える場合、境界で切り詰め、
  完全に範囲外のセグメントを除外するようにしました。

### Documentation

- チャンク長の動作と CPU での文字起こし参考ベンチマークを追記しました。

## [0.6.0] - 2026-09-04

### Changed

- 設定スキーマを `3.0.0` に更新し、`local` / `cloud` の mode ごとに provider と
  名前付き profile を管理する構成に変更しました。
- 実行 mode ごとの設定型を分離し、パイプライン、ジョブ設定、fingerprint、CLI を
  新しい設定構造に合わせて更新しました。

## [0.5.0] - 2026-09-03

### Changed

- 設定スキーマを `2.0.0` に更新し、文字起こし設定の profile 選択に対応しました。
- profile と設定キーの検証を追加し、不正な設定に対するエラー表示を改善しました。

## [0.4.0] - 2026-09-03

### Added

- Azure Speech Fast Transcription によるクラウド文字起こしと、任意の話者ラベルに対応しました。
- クラウドへの音声送信に明示的な `--allow-cloud-upload` を必要とする仕組みを追加しました。

## [0.3.0] - 2026-09-03

### Added

- `config init` と `config migrate`、設定変更の `--dry-run`、強制置換時のバックアップを追加しました。
- 配布パッケージに設定例を同梱しました。

### Changed

- 設定の `schema_version` を整数から SemVer 文字列の `"1.0.0"` に変更しました。
  このバージョンでは旧整数形式を読み取り時に変換し、ファイルの更新は明示的な移行操作で行います。

## [0.2.1] - 2026-08-03

### Changed

- Markdown 出力のメタデータを YAML Frontmatter に変更しました。
- 音声ファイルに加え、動画内の音声ストリームを処理できることを CLI とドキュメントに明記し、
  動画入力のテストを追加しました。

## [0.2.0] - 2026-07-30

### Added

- WAV の整合性検証、永続的なジョブ状態管理、heartbeat による進捗記録を追加しました。
- ジョブの状態確認とクリーンアップ機能を追加しました。

### Changed

- 既定の出力先をカレントディレクトリに変更しました。

## [0.1.0] - 2026-07-30

### Added

- `ffmpeg` と `faster-whisper` を使用する、再開可能なローカル文字起こし CLI を追加しました。
- Markdown、SRT、JSONL、provenance manifest の出力に対応しました。
- 元ファイルを読み取り専用で扱い、出力確定前に SHA-256 を再確認する仕組みを追加しました。
