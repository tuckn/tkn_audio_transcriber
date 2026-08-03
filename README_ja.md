# tkn_audio_transcriber

[English](README.md)

`tkn_audio_transcriber` は、音声ファイルから Markdown 文字起こし、SRT 字幕、
JSONL セグメント、provenance manifest を作成するローカルCLIです。`ffmpeg`で
モノラル16 kHzへの正規化と分割を行い、`faster-whisper`で音声認識します。

元音声は読み取り専用で扱い、移動・削除・上書きをしません。出力を確定する直前にも
SHA-256を再確認します。会議メモの要約、用語補正、話者分離、生成AIの呼び出しは、
意図的にこのリポジトリの対象外としています。

## スクリプト、音声認識モデル、生成AIの境界

文字起こしはスクリプトだけでは成立しません。スクリプトは音声変換、分割、再開、
検証、出力を制御し、実際の「音声から文字」への変換には音声認識モデルが必要です。
このCLIでは、ローカルで動く`faster-whisper`（Whisper系ASRモデル）を使用します。

一度モデルをダウンロードすれば、通常の文字起こしでCodex、Copilot、OpenAI API、
その他の生成AIサービスへ接続する必要はありません。Whisper自体は機械学習モデルですが、
ここでいう「生成AIとの連携」、すなわち汎用LLM/APIへ音声や文字列を送る処理はありません。

役割の境界は次のとおりです。

- `ffmpeg`: 音声をモノラル16 kHz WAVへ変換し、チャンクへ分割する
- Pythonスクリプト: job管理、再開、heartbeat、検証、成果物作成を行う
- `faster-whisper`: 音声チャンクを文字列へ変換する
- 生成AIまたは人: 必要に応じて、要約、議題整理、固有名詞補正、読みやすい文章化を行う

最後の工程はdownstream処理であり、このリポジトリの基本CLIには含めません。

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

実設定の`./.tkn/config.yaml`はGitの除外対象です。文字起こし結果は既定でcurrent
working directoryへ出力します。明示すると次の設定と同じです。

```yaml
schema_version: 1
output_dir: .
```

初回文字起こし時に既知のモデルがなければ、Hugging Faceから自動ダウンロードします。
事前に取得しておくこともできます。

```console
tkn-audio-transcriber model download small
```

`small`はapplication独自のラベルではなく、OpenAI Whisperの正式な多言語モデル
サイズ名です。約2.44億parameterのモデルで、このCLIはlocal推論用にCTranslate2変換
された`Systran/faster-whisper-small`へ対応付けます。

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
変更しない実行です。この例ではcurrent working directoryを出力先に使います。

```powershell
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac" --dry-run
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
完全なモデルがすでにある場合は`unchanged`を返します。文字起こし前に準備する場合や、
network接続が利用できる間に取得しておく場合に使用します。

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

設定した既知モデル（`tiny`、`base`、`small`、`medium`、`large-v3`）がなければ、
音声処理前に自動ダウンロードします。`--dry-run`ではダウンロードしません。中断した
場合は同じ`transcribe`コマンドを再実行すると、完了済みチャンクを飛ばして再開します。

開始前にscratch容量を概算し、正規化後は実際のWAV容量からチャンク作成分を再確認します。
正規化WAVの形式・再生時間と全チャンクの合計時間が一致しない場合、または最終segmentが
decode済み音声長を越える場合は、最終outputを確定しません。decode済み音声情報はmanifestの
`decoded_audio`へ記録します。各stageとheartbeatはjobの`job.json`と`run.jsonl`へ保存します。

### `cleanup`

完了したjob stateをretention条件に従って整理します。既定は読み取り専用のdry-runで、
削除には`--apply`が必要です。

```console
tkn-audio-transcriber cleanup --older-than-days 30
tkn-audio-transcriber cleanup --older-than-days 30 --apply
```

対象になるのは、`completed`で、retention期間を過ぎ、最終manifestと全outputのsize・
SHA-256検証に成功したjobだけです。`running`、`failed`、manifest不正のjobは削除せず、
model cache、Hugging Face cache、最終文字起こしoutputも対象外です。`--apply`後に消した
checkpointは元に戻せませんが、検証済みの最終outputは残ります。

### `status`

永続job stateを読み、実行段階、現在のチャンク、PID、最終heartbeat、
最終checkpoint、run logの場所をJSONで表示します。読み取り専用です。

```console
tkn-audio-transcriber status
tkn-audio-transcriber status --state-dir "D:\transcription-state"
```

heartbeatは既定60秒です。`--heartbeat-seconds`または設定fileで変更できます。
これは長いASR処理が生存していることを示しますが、ASRの強制timeoutではありません。

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

4つのfileは同じbasenameを持つ、1組の出力成果物です。

| file | 内容・用途 |
| --- | --- |
| `*_transcript.md` | 人が読むための主成果物です。YAML Frontmatterに元音声、model、engine、言語、話者分離の有無、chunk秒数、transcriber名、transcriber versionを記録し、本文にtimestamp付きの文字起こしを格納します。内容確認、レビュー、後続の要約では、まずこのfileを使用します。 |
| `*_transcript.srt` | media playerやvideo editorで利用できる標準字幕fileです。各字幕に連番、開始・終了時刻、認識textを格納します。 |
| `*_transcript.jsonl` | 1行に1つのJSON objectを格納する機械処理向けのsegment dataです。各segmentは`index`、`start`、`end`、`text`、処理元の`chunk`を持ち、script処理、分析、別形式への変換に使用できます。 |
| `*_transcript.manifest.json` | provenanceと検証用の記録です。元音声のpath・hash、decode後音声の検査結果、model・設定、各出力のfile size・SHA-256を格納します。`validate`にはこのfileを指定し、ほかの3fileと一緒に保管します。 |

application管理のruntime dataは役割ごとに分離します。

```text
~/.tkn/audio_transcriber/state/          再起動後も使うjob checkpoint
~/.cache/audio_transcriber/models/       再取得可能なmodel
~/.cache/audio_transcriber/huggingface/  再取得可能なdownload cache
```

最終文字起こしoutputは既定でcurrent working directoryへ作成します。state、model、
cacheは上記のapplication管理場所へ分離したままです。すべての設定sourceにある
相対pathは、current working directoryを基準に解決します。

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
- 不足モデルを使う初回実行にはHugging Faceへの接続が必要。offline環境では事前に
  `model download`を実行する
- `faster-whisper`はprocess内で動くため、`ffmpeg`に適用する外部process timeoutの
  対象外。heartbeatは出るが、現在のチャンクを外部から強制停止する機能はない
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
