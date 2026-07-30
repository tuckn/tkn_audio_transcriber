# tkn_audio_transcriber

[English](README.md)

`tkn_audio_transcriber` は、音声ファイルから Markdown 文字起こし、SRT 字幕、
JSONL セグメント、provenance manifest を作成するローカルCLIです。`ffmpeg`で
モノラル16 kHzへの正規化と分割を行い、`faster-whisper`で音声認識します。

元音声は読み取り専用で扱い、移動・削除・上書きをしません。出力を確定する直前にも
SHA-256を再確認します。会議メモの要約、用語補正、話者分離、生成AIの呼び出しは、
意図的にこのリポジトリの対象外としています。

## 必要なもの

- Windows 10/11 または Linux
- Python 3.12以上
- [`uv`](https://docs.astral.sh/uv/)
- `PATH`から実行できる`ffmpeg`、または設定した`ffmpeg_executable`
- 実行中のモノラル16 kHz WAVと分割チャンクを保存できる空き容量

最初はCPUと`small`モデルを推奨します。`medium`は一般に認識精度が上がる一方、
メモリ使用量と処理時間が増えます。

## インストール

リポジトリのルートで実行します。

```console
uv tool install -e .
tkn-audio-transcriber --help
tkn-audio-transcriber config show
```

依存関係、package metadata、entry point、リポジトリの場所を変えた後は、
`--force`付きで再インストールします。

```console
uv tool install -e . --force
```

## 初期設定

`.tkn/config.example.yaml`を次のいずれかへコピーし、編集します。

- ユーザー設定: `~/.tkn/audio_transcriber/config.yaml`
- 作業ディレクトリ固有の上書き: `./.tkn/config.yaml`

実設定の`./.tkn/config.yaml`はGitの除外対象です。最小設定は次のとおりです。

```yaml
schema_version: 1
output_dir: C:/path/to/transcripts
```

モデルは明示的にダウンロードします。通常のコマンドでネットワークを使うのは
この操作だけです。

```console
tkn-audio-transcriber model download small
```

書き込まずに計画だけ確認できます。

```console
tkn-audio-transcriber model download small --dry-run
```

## 最初の文字起こし

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac"
```

1回だけ出力先を指定することもできます。

```powershell
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac" `
  --output-dir "C:\path\to\transcripts"
```

入力fingerprintと出力予定を確認するだけで、output、state、cache、reportを
変更しない実行です。

```powershell
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac" `
  --output-dir "C:\path\to\transcripts" --dry-run
```

## コマンド

### `config show`

解決後の非secret設定と、各値がどのsourceから決まったかをJSONで表示します。
読み取り専用であり、ディレクトリを作成しません。

```console
tkn-audio-transcriber config show
tkn-audio-transcriber --config "C:\path\to\config.yaml" config show
```

### `model download`

`faster-whisper`モデルを設定済みのmodel directoryへダウンロードします。
Hugging Faceへのnetwork accessを使いますが、元音声や文字起こし出力には触れません。
完全なモデルがすでにある場合は`unchanged`を返します。

```console
tkn-audio-transcriber model download small
tkn-audio-transcriber model download medium --model-dir "D:\models"
```

### `transcribe`

元音声の検証とhash計算、派生音声の正規化、チャンク作成、checkpointからの再開、
音声認識、元音声が変わっていないことの再検証、出力確定を順に行います。

```powershell
tkn-audio-transcriber transcribe "C:\path\to\meeting.m4a" `
  --output-dir "C:\path\to\transcripts" `
  --model small --language ja --chunk-seconds 600
```

安全性に関する主なoptionは次のとおりです。

- `--dry-run`: output、state、cache、reportを一切変更しない
- `--overwrite`: 内容が異なる、または不完全な既存の最終出力だけを置換する。
  指定しない場合は停止する
- `--keep-working-files`: 検証成功後も正規化WAVとチャンクを保持する。
  監査・再開用checkpointは常に保持する

不足モデルは暗黙にダウンロードしません。先に`model download`を実行します。
中断した場合は同じ`transcribe`コマンドを再実行すると、完了済みチャンクを飛ばして
再開します。

### `validate`

manifest schemaと、全出力のfile size・SHA-256を検証します。
`--verify-source`を付けると元音声も再度hash検証します。

```powershell
tkn-audio-transcriber validate "C:\path\to\meeting_transcript.manifest.json"
tkn-audio-transcriber validate "C:\path\to\meeting_transcript.manifest.json" `
  --verify-source
```

## 出力とruntime保存先

`meeting.flac`の場合、選択した出力先に次を作成します。

```text
meeting_transcript.md
meeting_transcript.srt
meeting_transcript.jsonl
meeting_transcript.manifest.json
```

application管理のruntime dataは役割ごとに分離します。

```text
~/.tkn/audio_transcriber/state/          再起動後も使うjob checkpoint
~/.cache/audio_transcriber/models/       再取得可能なmodel
~/.cache/audio_transcriber/huggingface/  再取得可能なdownload cache
```

明示的に設定しない限り、リポジトリのルートへruntime fileを作りません。すべての
設定sourceにある相対pathは、current working directoryを基準に解決します。

## 冪等性と失敗時の動作

- 同一入力・同一設定で出力が有効: `unchanged`
- 新規作成: `created`
- `--overwrite`で異なる出力を置換: `replaced`
- `--overwrite`なしで異なる、または不完全な出力が存在: error
- dry-run: `planned`

最終結果は標準出力へJSONで出します。進捗と診断は標準エラーへ
`[LEVEL] message`形式で出します。`-q/--quiet`はerrorのみ、`-v/--verbose`は
debugも表示します。対話terminalでANSI colorを利用できる場合は`SUCCESS`を緑、
`ERROR`/`CRITICAL`を赤で表示します。redirect、`NO_COLOR`、`TERM=dumb`、
Windows console非対応時は無色です。

最終fileは生成・検証後に置換し、manifestを最後に確定します。複数fileの確定中に
停止した場合は`--overwrite`付きで再実行してください。checkpointがあるため、
完了済みチャンクは再処理しません。

## 設定の優先順位

後のsourceが前を上書きします。

1. built-in defaults
2. `~/.tkn/audio_transcriber/config.yaml`
3. `./.tkn/config.yaml`
4. 明示した`--config`
5. 個別CLI option

unknown key、不正な型、未対応`schema_version`はerrorです。`config show`で各値の
採用sourceを確認できます。

## 定期実行

`uv tool install -e .`でinstallし、元音声と出力に絶対pathを使えば、Windows Task
Schedulerまたはcronから同じ`transcribe`コマンドを実行できます。終了コードは、
成功`0`、想定内の設定・入力・検証error`2`、中断`130`、想定外error`1`です。

## 制約とprivacy

- 話者分離なし
- 会議要約・生成AI呼び出しなし
- 用語の自動補正なし
- model downloadにはHugging Faceへの接続が必要
- `faster-whisper`はprocess内で動くため、`ffmpeg`に適用する外部process timeoutの
  対象外
- provenanceのためmanifestへ元音声pathとhashを記録する。pathが機微な場合は
  manifestをlocal operational metadataとして扱う

## 開発と検証

```console
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

testは合成fileと偽の音声認識・ffmpeg adapterを使用します。modelのdownloadや
実音声の更新は行いません。
