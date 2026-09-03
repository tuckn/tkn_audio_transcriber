# tkn_audio_transcriber

[English](README.md)

`tkn_audio_transcriber` は、音声ファイル、または動画ファイル内の音声ストリームから
Markdown文字起こし、SRT字幕、JSONLセグメント、provenance manifestを作成する
CLIです。`ffmpeg`でモノラル16 kHzへ正規化し、既定のローカル
`faster-whisper`またはAzure Speech Fast Transcriptionで音声認識します。

元のメディアファイルは読み取り専用で扱い、移動・削除・上書きをしません。出力を確定する直前にも
SHA-256を再確認します。会議メモの要約、用語補正、汎用生成AIの呼び出しは、
意図的にこのリポジトリの対象外です。Azure Speechでは話者ラベルを任意で追加できます。

## スクリプト、音声認識モデル、生成AIの境界

文字起こしはスクリプトだけでは成立しません。スクリプトは音声変換、分割、再開、
検証、出力を制御し、実際の「音声から文字」への変換には音声認識モデルが必要です。
このCLIでは`provider`設定により、ローカルで動く`faster-whisper`（Whisper系ASR
モデル）またはAzure Speech Fast Transcriptionを選択します。

ローカルproviderは、モデル取得後に音声をuploadしません。Azure providerは、commandに
`--allow-cloud-upload`を指定したときだけ、派生したモノラル16 kHz WAVを1つuploadします。
どちらも汎用LLMへ音声や文字起こしtextを送りません。

役割の境界は次のとおりです。

- `ffmpeg`: 先頭の音声ストリームを抽出し、モノラル16 kHz WAVへ変換する。
  映像フレームは使用しない
- Pythonスクリプト: job管理、再開、heartbeat、検証、成果物作成を行う
- `faster-whisper`: ローカル音声チャンクを文字列へ変換する
- Azure Speech Fast Transcription: 正規化WAV全体を文字起こしし、任意で話者を識別する
- 生成AIまたは人: 必要に応じて、要約、議題整理、固有名詞補正、読みやすい文章化を行う

最後の工程はdownstream処理であり、このリポジトリの基本CLIには含めません。

## 必要なもの

- Windows 10/11 または Linux
- Python 3.12以上
- [`uv`](https://docs.astral.sh/uv/)
- `PATH`から実行できる`ffmpeg`、または設定した`ffmpeg_executable`
- モノラル16 kHz WAVと、ローカルproviderでは分割チャンクを保存できる空き容量
- Azureでは、Speech User roleを持つEntra identityと設定済みSpeech endpointへの接続

入力拡張子の固定リストは設けていません。`ffmpeg`がデコードでき、音声ストリームを
1つ以上含むローカルの音声・動画ファイルを受け付けます。代表例は`.wav`、`.flac`、
`.mp3`、`.m4a`、`.mp4`です。

最初はCPUと`small`モデルを推奨します。`medium`は一般に認識精度が上がる一方、
メモリ使用量と処理時間が増えます。

## インストール

リポジトリのルートで実行します。

```console
uv tool install .
tkn-audio-transcriber --help
tkn-audio-transcriber config show
```

source code、package resource、依存関係、package metadata、entry pointを変更した後は、
再インストールします。

```console
uv tool install . --reinstall
```

## 初期設定

applicationに同梱されたexampleからユーザー設定を作成し、編集します。

```console
tkn-audio-transcriber config init
```

このcommandは`~/.tkn/audio_transcriber/config.yaml`を作成して絶対pathを表示します。
既存内容がexampleと同じ場合は`unchanged`を返します。編集済み設定は`--force`なしでは
上書きせず、強制置換時にはbackupを作成します。作業ディレクトリ固有のoverrideを
作成する場合はpathを指定します。

```console
tkn-audio-transcriber config init .tkn/config.yaml
```

設定は次の場所から読み込めます。

- ユーザー設定: `~/.tkn/audio_transcriber/config.yaml`
- 作業ディレクトリ固有の上書き: `./.tkn/config.yaml`

実設定の`./.tkn/config.yaml`はGitの除外対象です。文字起こし結果は既定でcurrent
working directoryへ出力します。明示すると次の設定と同じです。

```yaml
schema_version: "1.1.0"
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

## Azure Speech Fast Transcription

実resource名はユーザー設定またはGit除外済みの作業ディレクトリ設定だけに保存します。
commitするexampleではplaceholderを使ってください。次は対応するMVP設定です。endpointの
placeholderだけを自分のresource endpointへ置換します。

```yaml
schema_version: "1.1.0"
provider: azure-speech-fast
azure_speech_endpoint: https://<speech-resource-name>.cognitiveservices.azure.com/
azure_speech_region: japaneast
azure_speech_api_version: "2025-10-15"
azure_speech_locale: ja-JP
azure_speech_diarization_enabled: true
azure_speech_max_speakers: 8
azure_speech_timeout_seconds: 600
azure_speech_max_retries: 3
```

previewは完全にlocalです。credential作成、token取得、Azure API、model download、
`ffmpeg`を呼び出さず、output、state、cache、report、temporary fileも作成しません。
予定provider、endpoint種別、region、API version、locale、cloud承認の要否を表示します。

```powershell
tkn-audio-transcriber --config "C:\path\to\azure.yaml" transcribe `
  "C:\path\to\meeting.mp4" --dry-run
```

Azureの実行にはYAMLへ保存できない実行単位の承認flagが必要です。

```powershell
tkn-audio-transcriber --config "C:\path\to\azure.yaml" transcribe `
  "C:\path\to\meeting.mp4" --allow-cloud-upload
```

CLIは録音内容の機密区分を判定できません。flagを付ける前に、選択したinputがcloud処理を
許可されたdataであることを利用者が確認します。正規化音声のPOST時点からAzure課金が
発生し得ます。dry-run、hash計算、local正規化はSpeech APIを呼びません。

認証には`DefaultAzureCredential`とCognitive Services token scopeを使い、subscription
keyは受け付けません。CLIから`az login`やbrowser対話認証は行わないため、許可された
Entra手段で事前にsign inしてください。正規化WAVは2時間未満かつ250 MB未満である必要が
あり、credentialやHTTP clientを作る前に検証します。映像frameとlocal chunkはAzureへ
送りません。

自動retryは429、retry可能な5xx、upload前の接続失敗、音声streamが未完了と確認できる
transport失敗だけです。`Retry-After`を尊重し、400/401/403/413はretryしません。uploadが
完了した可能性があるのにresponseがない場合は、課金対象requestの重複を避けるため
`submission_outcome_unknown`で停止します。
Azure error時に`faster-whisper`へfallbackしません。別途local実行する場合は利用者が
local providerを明示的に選びます。

## コマンド

### `config init`

application-ownedなpackage exampleから完全な設定を作成します。書き込まずに確認する場合は
`--dry-run`、編集済みfileをbackupして置換する場合は`--force`を指定します。

```console
tkn-audio-transcriber config init --dry-run
tkn-audio-transcriber config init
```

### `config show`

解決後の非secret設定、各値のsource、各設定sourceのschema version、effective schema
version、in-memory migrationの有無をJSONで表示します。読み取り専用であり、
ディレクトリを作成しません。

```console
tkn-audio-transcriber config show
tkn-audio-transcriber --config "C:\path\to\config.yaml" config show
```

### `config migrate`

legacy設定1つを現在のschemaへ移行します。移行後の設定を検証し、元fileの隣にbackupを作って
から原子的に置換します。`--dry-run`では書き込まずに計画を確認できます。pathを省略した
場合はユーザー設定が対象です。

```console
tkn-audio-transcriber config migrate --dry-run
tkn-audio-transcriber config migrate
tkn-audio-transcriber config migrate .tkn/config.yaml
```

### `model download`

`faster-whisper`モデルを設定済みのmodel directoryへダウンロードします。
Hugging Faceへのnetwork accessを使いますが、元のメディアや文字起こし出力には触れません。
完全なモデルがすでにある場合は`unchanged`を返します。文字起こし前に準備する場合や、
network接続が利用できる間に取得しておく場合に使用します。

```console
tkn-audio-transcriber model download small
tkn-audio-transcriber model download medium --model-dir "D:\models"
```

### `transcribe`

元メディアの検証とhash計算、派生音声の正規化、選択providerでの音声認識、元メディアが
変わっていないことの再検証、出力確定を順に行います。local modeは再開可能なchunkを作り、
Azure modeは正規化WAV全体を1 requestで送ります。

動画ファイルでは、`ffmpeg`が先頭の音声ストリーム（`0:a:0`）を選択し、映像ストリームを
破棄します。元動画は変更せず、出力名には元動画のファイル名（拡張子を除く）を使います。

```powershell
tkn-audio-transcriber transcribe "C:\path\to\meeting.m4a" `
  --output-dir "C:\path\to\transcripts" `
  --model small --language ja --chunk-seconds 600
```

MP4録画も同じコマンドで処理できます。

```powershell
tkn-audio-transcriber transcribe "C:\path\to\town-hall.mp4" `
  --output-dir "C:\path\to\transcripts" `
  --model small --language en
```

安全性に関する主なoptionは次のとおりです。

- `--dry-run`: output、state、cache、reportを一切変更しない
- `--overwrite`: 内容が異なる、または不完全な既存の最終出力だけを置換する。
  指定しない場合は停止する
- `--keep-working-files`: 検証成功後も正規化WAVとチャンクを保持する。
  監査・再開用checkpointは常に保持する
- `--allow-cloud-upload`: この実行だけAzure uploadを承認する。設定fileからは読まない

`provider: faster-whisper`で設定した既知モデル（`tiny`、`base`、`small`、`medium`、
`large-v3`）がなければ、
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
`--verify-source`を付けると元メディアも再度hash検証します。
新規実行はprovider・認証・API provenanceを含むmanifest schema 2を書きますが、
既存schema 1 manifestも引き続き検証できます。

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
| `*_transcript.md` | 人が読むための主成果物です。YAML Frontmatterに元メディア、model、engine、言語、話者分離の有無、chunk秒数、transcriber名、transcriber versionを記録し、本文にtimestamp付きの文字起こしを格納します。内容確認、レビュー、後続の要約では、まずこのfileを使用します。 |
| `*_transcript.srt` | media playerやvideo editorで利用する字幕fileです。Azureのcueは返却された場合だけ話者labelを含み、local出力は従来どおりです。 |
| `*_transcript.jsonl` | 1行1 JSON objectのsegment dataです。Azure segmentは任意の`speaker`を追加し、local recordは従来fieldを維持します。 |
| `*_transcript.manifest.json` | schema 2のprovenance・検証記録です。provider、API version、region、locale、話者分離、Entra方式、source hash、decode時間、試行・retry回数、tool version、output hashを格納し、tokenやAuthorization headerは保存しません。 |

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
採用sourceと各sourceのschema状態を確認できます。各設定fileはmerge前に個別検証され、
`schema_version`は上位sourceから上書きする設定値ではなくsource metadataとして扱われます。

application-owned設定の独立したschema versionは`"1.1.0"`です。3要素の文字列を必須とします。
現在のreaderはMajor 1かつMinor 1までを受け入れます。Patchは構造を変えない契約なので、
`"1.1.7"`のような新しいPatchも読み込めます。新しいMinorまたはMajor、test済みmigrationの
ない古いMajor、不正形式、version欠落は、必要なactionを示すerrorになります。

`1.0.x`と従来の整数`schema_version: 1`はin-memory migrationで引き続き読み込めますが、warningを
表示します。読込時にfileを暗黙更新しません。`config migrate`を実行すると、validation、
backup、atomic replacementを行って`schema_version: "1.1.0"`を永続化します。

## 定期実行

`uv tool install .`でinstallし、元メディアと出力に絶対pathを使えば、Windows Task
Schedulerまたはcronから同じ`transcribe`コマンドを実行できます。終了コードは、
成功`0`、想定内の設定・入力・検証error`2`、中断`130`、想定外error`1`です。

## 制約とprivacy

- 話者分離はAzure Speechでのみ利用でき、ローカル`faster-whisper`の出力には話者labelを
  追加しない
- 会議要約・生成AI呼び出しなし
- 用語の自動補正なし
- 不足モデルを使う初回実行にはHugging Faceへの接続が必要。offline環境では事前に
  `model download`を実行する
- Azure modeは派生音声を設定済みSpeech resourceへ送り、Azure利用料が発生し得る。
  sourceを確認して`--dry-run`後、実uploadごとに明示承認する
- logとAzure errorはHTTP status、Azure error code、request ID、試行回数など安全な診断だけを
  記録し、音声、文字起こしtext、bearer token、Authorization header、request/response bodyを
  記録しない
- `faster-whisper`はprocess内で動くため、`ffmpeg`に適用する外部process timeoutの
  対象外。heartbeatは出るが、現在のチャンクを外部から強制停止する機能はない
- provenanceのためmanifestへ元メディアpathとhashを記録する。pathが機微な場合は
  manifestをlocal operational metadataとして扱う

## 開発と検証

```console
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

testは合成fileと偽のcredential、HTTP client、音声認識、ffmpeg adapterを使用します。
Azure APIやmodel downloadを呼び出さず、実音声も更新しません。
