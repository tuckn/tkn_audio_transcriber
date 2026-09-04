<<<<<<< HEAD
# tkn_audio_transcriber

[日本語](README_ja.md)

`tkn_audio_transcriber` is a CLI that turns an audio file, or the audio stream
inside a video file, into a Markdown transcript, SRT subtitles, JSONL segments, and a
provenance manifest. It uses `ffmpeg` for mono/16 kHz normalization, then runs either
local `faster-whisper` (the default) or Azure Speech Fast Transcription.

The source media file is opened read-only and is never moved, deleted, or overwritten.
The CLI verifies its SHA-256 hash again before committing outputs. Meeting-note
summarization, terminology correction, and general-purpose generative AI are
intentionally outside this repository. Azure Speech can optionally add speaker labels.

## Script, speech-recognition model, and generative-AI boundary

A script alone cannot turn speech into text. The script orchestrates audio conversion,
chunking, resume, validation, and output; an automatic speech-recognition (ASR) model
performs the actual speech-to-text inference. `transcription.active.mode` selects the
data-processing boundary (`local` or `cloud`), while `transcription.active.profile`
selects a named configuration within that mode. Each mode's `provider` identifies the
implementation: local `faster-whisper`, a Whisper-family ASR model, or Azure Speech
Fast Transcription.

The local provider does not upload audio after its model has been downloaded. The Azure
provider uploads one derived mono/16 kHz WAV only after the command includes
`--allow-cloud-upload`. Neither provider sends audio or transcript text to a
general-purpose LLM.

The boundary is:

- `ffmpeg`: extract the first audio stream and convert it to mono 16 kHz WAV; video
  frames are ignored
- Python code: manage jobs, resume, heartbeat, validation, and output artifacts
- `faster-whisper`: convert local audio chunks into text
- Azure Speech Fast Transcription: transcribe one complete normalized WAV and optionally
  identify speakers
- optional generative AI or a person: summarize, organize topics, correct domain terms,
  and polish prose downstream

That final downstream stage is not part of the base CLI.

## Requirements

- Windows 10/11 or Linux
- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` available on `PATH`, or an absolute `processing.ffmpeg.executable` in config
- Enough disk space for a mono 16 kHz WAV, plus split chunks for the local provider
- For Azure: an Entra identity with the Speech User role and network access to the
  configured Speech endpoint

There is no extension allowlist. Any local audio or video file that `ffmpeg` can decode
and that contains at least one audio stream is accepted. This includes common inputs
such as `.wav`, `.flac`, `.mp3`, `.m4a`, and `.mp4`.

CPU use with the `small` model is the recommended first run. `medium` generally
improves recognition at the cost of more memory and processing time.

## Install

From the repository root:

```console
uv tool install .
tkn-audio-transcriber --help
tkn-audio-transcriber config show
```

Reinstall after changing source code, packaged resources, dependencies, package
metadata, or the entry point:

```console
uv tool install . --reinstall
```

## Initial configuration

Create the user configuration from the example bundled with the application, then
edit it:

```console
tkn-audio-transcriber config init
```

The command writes `~/.tkn/audio_transcriber/config.yaml`, reports its absolute
path, and returns `unchanged` when its content already matches the example. It
does not overwrite edited content unless `--force` is specified; forced replacement
creates a backup. To create a working-directory override instead, pass its path:

```console
tkn-audio-transcriber config init .tkn/config.yaml
```

Configuration can be loaded from these locations:

- user setting: `~/.tkn/audio_transcriber/config.yaml`
- working-directory override: `./.tkn/config.yaml`

The real `./.tkn/config.yaml` is ignored by Git. Transcript outputs default to
the current working directory. The equivalent explicit setting is:

```yaml
schema_version: "3.0.0"
folders:
  output: .
```

The configuration is grouped by role:

- `transcription`: the local/cloud boundary, provider, and mode-specific profiles
- `folders`: output, local model, download cache, and durable state locations
- `processing`: `ffmpeg`, heartbeat, and retained-working-file behavior

The packaged example separates local and cloud settings. Local profiles are
`local-small`, `local-large`, `gpu-quality`, and `gpu-fast`; the cloud profile is
`azure-ja`. Select the normal mode and profile in YAML, or override both for one
invocation with `--profile MODE/NAME`:

```yaml
transcription:
  active:
    mode: local
    profile: local-small
```

```console
tkn-audio-transcriber config profiles
tkn-audio-transcriber --profile local/gpu-quality transcribe "C:\path\to\meeting.flac"
```

On the first transcription, a missing known model is downloaded automatically
from Hugging Face. The built-in models are public, so no Hugging Face account,
login, or access agreement is required. An internet connection is needed only
for the initial download. You can also download a model in advance:

```console
tkn-audio-transcriber model download small
```

`small` is an official multilingual OpenAI Whisper model-size name, not an
application-specific label. It has about 244 million parameters. This CLI maps
it to the CTranslate2-converted `Systran/faster-whisper-small` repository for
local inference.

Preview the download without writing:

```console
tkn-audio-transcriber model download small --dry-run
```

## First transcription

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac"
```

You can select the output for a single run instead of configuring it:

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac" ^
  --output-dir "C:\path\to\transcripts"
```

In PowerShell, use a backtick instead of `^` for multiline commands.

Preview all resolved paths and the input fingerprint without creating output,
state, cache, or report files. This example uses the current working directory
as the output directory:

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac" --dry-run
```

## Azure Speech Fast Transcription

Keep real resource names in a user or ignored working-directory config. A committed
example should use placeholders. The values below reflect the supported MVP contract;
replace only the endpoint placeholder with your own resource endpoint.

```yaml
schema_version: "3.0.0"
transcription:
  active:
    mode: cloud
    profile: azure-ja
  cloud:
    provider: azure-speech-fast
    profiles:
      azure-ja:
        endpoint: https://<speech-resource-name>.cognitiveservices.azure.com/
        region: japaneast
        api_version: "2025-10-15"
        locale: ja-JP
        diarization:
          enabled: true
          max_speakers: 8
        request:
          timeout_seconds: 600
          max_retries: 3
```

Azure Speech Fast Transcription does not expose a Whisper-style model-name setting.
Its profile therefore contains the endpoint, locale, diarization, and request behavior
instead of a `model` key.

Preview is completely local: it does not create credentials, get a token, call Azure,
download a model, invoke `ffmpeg`, or create output/state/cache/report/temporary files.
It reports the planned provider, endpoint type, region, API version, locale, and whether
cloud approval is required.

```console
tkn-audio-transcriber --profile cloud/azure-ja transcribe "C:\path\to\meeting.mp4" --dry-run
```

An actual Azure run requires an approval flag that cannot be saved in YAML:

```console
tkn-audio-transcriber --profile cloud/azure-ja transcribe ^
  "C:\path\to\meeting.mp4" --allow-cloud-upload
```

The CLI cannot determine a recording's confidentiality classification. The operator is
responsible for confirming that the selected input is permitted for cloud processing
before adding the flag. Azure charges can begin when the normalized audio POST is
submitted; dry-run, hashing, and local normalization do not call the Speech API.

Authentication uses `DefaultAzureCredential` and the Cognitive Services token scope.
Subscription keys are unsupported. The CLI never runs `az login` or interactive browser
authentication. Sign in beforehand with an approved Entra method. The normalized WAV
must be shorter than two hours and smaller than 250 MB; both limits are checked before
credential or HTTP client creation. Azure receives no video frames and no local chunks.

Automatic retries are limited to 429, retryable 5xx responses, pre-upload connection
failures, and transport failures for which the audio stream is confirmed incomplete.
`Retry-After` is honored. HTTP 400/401/403/413 are not retried. If the upload may have
completed but no response arrived, the command stops with `submission_outcome_unknown`
instead of risking a duplicate paid request.
Azure errors never fall back to `faster-whisper`; select a `local/NAME` profile
explicitly if a separate local run is desired.

## Commands

### `config init`

Creates a complete configuration from the application-owned packaged example.
Use `--dry-run` to preview without writing, or `--force` to back up and replace an
edited file.

```console
tkn-audio-transcriber config init --dry-run
tkn-audio-transcriber config init
```

### `config show`

Prints resolved non-secret settings, each value's source, every configuration
source's schema version, the effective schema version, and any in-memory migration
as JSON. It is read-only and does not create directories.

```console
tkn-audio-transcriber config show
tkn-audio-transcriber --config "C:\path\to\config.yaml" config show
tkn-audio-transcriber --profile local/gpu-quality config show
```

### `config profiles`

Lists named transcription profiles by local/cloud mode, their provider, and the active
selection. It is read-only. Use global `--profile MODE/NAME` to preview another
selection without editing YAML.

```console
tkn-audio-transcriber config profiles
tkn-audio-transcriber --profile local/gpu-quality config profiles
```

There is no independent `--provider` switch. Change providers by selecting a
`local/NAME` or `cloud/NAME` profile. Azure-only options on a local profile, or
Whisper-only options on a cloud profile, are configuration errors.

### `config migrate`

Migrates a flat schema 1.x configuration or schema 2's mixed profile list into schema
3's separated local/cloud hierarchy. The command validates the result, writes a backup
beside the original, and replaces the original atomically.
Preview the operation with `--dry-run`. Without a path it targets the user config.

```console
tkn-audio-transcriber config migrate --dry-run
tkn-audio-transcriber config migrate
tkn-audio-transcriber config migrate .tkn/config.yaml
```

### `model download`

Downloads a `faster-whisper` model into the configured model directory. It uses
Hugging Face network access but does not touch source media or transcript output.
An existing complete model returns `unchanged`. Use this command to prepare a
machine before transcription or while network access is available.

```console
tkn-audio-transcriber model download small
tkn-audio-transcriber model download medium --model-dir "D:\models"
```

### `transcribe`

The command validates and hashes the source, normalizes a derived copy, recognizes
speech with the selected provider, verifies the source is unchanged, then commits
validated outputs. Local mode creates resumable chunks; Azure mode sends the complete
normalized WAV in one request.

For a video file, `ffmpeg` selects the first audio stream (`0:a:0`) and discards the
video stream. The original video remains unchanged, and output names use its file stem.

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.m4a" ^
  --output-dir "C:\path\to\transcripts" ^
  --model small --language ja --chunk-seconds 600
```

An MP4 recording uses the same command:

```console
tkn-audio-transcriber transcribe "C:\path\to\town-hall.mp4" ^
  --output-dir "C:\path\to\transcripts" ^
  --model small --language en
```

Important safety options:

- `--dry-run`: no output, state, cache, or report changes
- `--overwrite`: replace only existing differing or incomplete final outputs;
  without it the command stops
- `--keep-working-files`: retain the normalized WAV and chunks after successful
  verification; checkpoints are always retained for audit/resume
- `--allow-cloud-upload`: approve an Azure upload for this invocation only; it is never
  read from configuration

When the selected mode is `local`, if its known model (`tiny`,
`base`, `small`, `medium`, or `large-v3`)
is missing, the command downloads it automatically before audio processing.
`--dry-run` never downloads a model. If a run is interrupted, repeat the same
`transcribe` command; completed chunks are skipped.

Before processing, the CLI estimates scratch-space needs. After normalization, it
checks the actual WAV size before creating chunks. It refuses to commit final output if
the normalized WAV format is invalid, total chunk duration differs from decoded audio,
or the last segment exceeds decoded duration. The manifest records these checks under
`decoded_audio`. Each stage and heartbeat is stored in the job's `job.json` and
`run.jsonl`.

### `cleanup`

Applies retention rules to completed job state. The default is a read-only plan;
`--apply` is required to delete anything.

```console
tkn-audio-transcriber cleanup --older-than-days 30
tkn-audio-transcriber cleanup --older-than-days 30 --apply
```

A job is eligible only when it is `completed`, older than the retention period, and its
final manifest plus every output size and SHA-256 validate successfully. Running,
failed, or invalid jobs are preserved. Model caches, the Hugging Face cache, and final
transcript outputs are outside cleanup scope. Applied checkpoint deletion is not
reversible, although the validated final outputs remain.

### `status`

Reads durable job state and prints the stage, current chunk, PID, latest heartbeat,
latest checkpoint, and run-log location as JSON. It is read-only.

```console
tkn-audio-transcriber status
tkn-audio-transcriber status --state-dir "D:\transcription-state"
```

The default heartbeat interval is 60 seconds and can be changed with
`--heartbeat-seconds` or config. A heartbeat shows that the in-process recognition call
is alive; it is not a hard ASR timeout.

### `validate`

Checks manifest schema plus the size and SHA-256 of every output. Add
`--verify-source` to re-hash the original recording as well.
New runs write manifest schema 3, including selected-profile and
provider/authentication/API provenance; validation remains compatible with existing
schema 1 and 2 manifests.

```console
tkn-audio-transcriber validate "C:\path\to\meeting__local__local-small_transcript.manifest.json"
tkn-audio-transcriber validate "C:\path\to\meeting__local__local-small_transcript.manifest.json" ^
  --verify-source
```

## Outputs and runtime locations

For `meeting.flac`, the selected output directory receives:

```text
meeting__local__local-small_transcript.md
meeting__local__local-small_transcript.srt
meeting__local__local-small_transcript.jsonl
meeting__local__local-small_transcript.manifest.json
```

The four files share one basename and form a single output set. The
`__local__local-small` portion records the selected `MODE/PROFILE`, so the same
source can be transcribed with another profile without replacing these files.
Existing profile-less output files from earlier versions are left untouched.

| File | Purpose |
| --- | --- |
| `*_transcript.md` | Primary human-readable transcript. Its YAML Frontmatter records the source, selected profile, model, engine, language, speaker-separation status, chunk length, transcriber name, and transcriber version. The body contains timestamped transcript text. Start with this file for reading, review, or downstream summarization. |
| `*_transcript.srt` | Standard subtitle file for media players and video editors. Azure cues include a speaker label when returned; local output is unchanged. |
| `*_transcript.jsonl` | Machine-readable segment data with one JSON object per line. Azure segments add optional `speaker`; local records retain the existing fields. |
| `*_transcript.manifest.json` | Schema 3 provenance and validation record. It stores the selected profile, provider, API version, region, locale, diarization, Entra method, source hash, decoded duration, attempts/retries, tool version, and output hashes. It never stores a token or Authorization header. |

Application-managed runtime data is separated by role:

```text
~/.tkn/audio_transcriber/state/      durable job checkpoints
~/.cache/audio_transcriber/models/  replaceable model files
~/.cache/audio_transcriber/huggingface/ replaceable download cache
```

Final transcript outputs default to the current working directory. State, model,
and cache data remain in the application-managed locations above. Relative paths
in all config sources are resolved from the current working directory.

## Idempotency and failure behavior

- Same input, profile, and transcription settings with valid outputs: `unchanged`
- Same input with a different profile: a separate profile-named output set is `created`
- New outputs: `created`
- Differing outputs with `--overwrite`: `replaced`
- Differing or incomplete outputs without `--overwrite`: error
- Dry run: `planned`

The final result is JSON on standard output. Progress and diagnostics use
standard error in `[LEVEL] message` form. `-q/--quiet` shows only errors;
`-v/--verbose` adds debug details. Interactive terminals render `SUCCESS` in
green and `ERROR`/`CRITICAL` in red when ANSI color is supported. Redirected
output, `NO_COLOR`, `TERM=dumb`, or unsupported Windows consoles use no color.

Final files are built and validated before replacement, and the manifest is
committed last. If the process stops during final multi-file commit, rerun with
`--overwrite`; the durable checkpoint prevents retranscribing completed chunks.

## Configuration precedence

Later sources override earlier sources:

1. built-in defaults
2. `~/.tkn/audio_transcriber/config.yaml`
3. `./.tkn/config.yaml`
4. explicit `--config`
5. configured `transcription.active.{mode,profile}`, or global `--profile MODE/NAME`
6. individual CLI options

Unknown keys, invalid types, and unsupported `schema_version` values are errors.
Every file is validated before deep-merging its nested settings, and `schema_version`
is source metadata, not an overridable setting. `config show` reports the selected
profile, available profiles, source selected for every effective value, and schema
status of every source.

Application-owned configuration uses the independent schema version `"3.0.0"`.
The three-part string is required. This reader accepts schema 3 Patch versions such as
`"3.0.7"` because Patch changes do not alter structure. It rejects newer Minor or Major
versions, older versions without a tested migration, malformed versions, and missing
versions with an actionable error.

Flat versions `1.0.x`, `1.1.x`, the former integer `schema_version: 1`, and mixed-profile
schema `2.0.x` remain readable through an in-memory structural migration and emit a
warning; reading never rewrites a file. Run `config migrate` to persist schema `"3.0.0"`
with validation, backup, and atomic replacement.

## Scheduled operation

Install the CLI with `uv tool install .`, use absolute source-media/output paths,
and run the same `transcribe` command from Windows Task Scheduler or cron. The
command returns `0` on success, `2` for expected configuration/input/validation
errors, `130` for interruption, and `1` for unexpected failures.

## Limitations and privacy

- Speaker diarization is available only with Azure Speech; local `faster-whisper`
  output remains unlabeled
- No meeting summary or generative-AI call
- No automatic terminology correction
- The first run for a missing model requires access to Hugging Face; use
  `model download` in advance for an offline transcription machine
- Azure mode sends derived audio to the configured Speech resource and can incur Azure
  charges. Review the source, run `--dry-run`, then explicitly approve each real upload
- Logs and Azure errors contain only safe diagnostics such as status, Azure error code,
  request ID, and attempt counts; they do not record audio, transcript text, bearer
  tokens, authorization headers, or request/response bodies
- `faster-whisper` runs in-process, so its recognition phase does not have the
  external-process timeout used for `ffmpeg`; heartbeat is available, but the current
  chunk cannot yet be forcibly cancelled from another command
- The manifest records the source media path and hashes for provenance; treat it as
  local operational metadata when paths are sensitive

## Development and verification

```console
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Tests use synthetic files and fake credentials, HTTP clients, speech-recognition, and
`ffmpeg` adapters. They do not call Azure, download a model, or modify a real recording.
=======
# tkn_audio_transcriber

[日本語](README_ja.md)

`tkn_audio_transcriber` is a CLI that turns an audio file, or the audio stream
inside a video file, into a Markdown transcript, SRT subtitles, JSONL segments, and a
provenance manifest. It uses `ffmpeg` for mono/16 kHz normalization, then runs either
local `faster-whisper` (the default) or Azure Speech Fast Transcription.

The source media file is opened read-only and is never moved, deleted, or overwritten.
The CLI verifies its SHA-256 hash again before committing outputs. Meeting-note
summarization, terminology correction, and general-purpose generative AI are
intentionally outside this repository. Azure Speech can optionally add speaker labels.

## Script, speech-recognition model, and generative-AI boundary

A script alone cannot turn speech into text. The script orchestrates audio conversion,
chunking, resume, validation, and output; an automatic speech-recognition (ASR) model
performs the actual speech-to-text inference. `transcription.active.mode` selects the
data-processing boundary (`local` or `cloud`), while `transcription.active.profile`
selects a named configuration within that mode. Each mode's `provider` identifies the
implementation: local `faster-whisper`, a Whisper-family ASR model, or Azure Speech
Fast Transcription.

The local provider does not upload audio after its model has been downloaded. The Azure
provider uploads one derived mono/16 kHz WAV only after the command includes
`--allow-cloud-upload`. Neither provider sends audio or transcript text to a
general-purpose LLM.

The boundary is:

- `ffmpeg`: extract the first audio stream and convert it to mono 16 kHz WAV; video
  frames are ignored
- Python code: manage jobs, resume, heartbeat, validation, and output artifacts
- `faster-whisper`: convert local audio chunks into text
- Azure Speech Fast Transcription: transcribe one complete normalized WAV and optionally
  identify speakers
- optional generative AI or a person: summarize, organize topics, correct domain terms,
  and polish prose downstream

That final downstream stage is not part of the base CLI.

## Requirements

- Windows 10/11 or Linux
- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` available on `PATH`, or an absolute `processing.ffmpeg.executable` in config
- Enough disk space for a mono 16 kHz WAV, plus split chunks for the local provider
- For Azure: an Entra identity with the Speech User role and network access to the
  configured Speech endpoint

There is no extension allowlist. Any local audio or video file that `ffmpeg` can decode
and that contains at least one audio stream is accepted. This includes common inputs
such as `.wav`, `.flac`, `.mp3`, `.m4a`, and `.mp4`.

CPU use with the `small` model is the recommended first run. `medium` generally
improves recognition at the cost of more memory and processing time.

## Install

From the repository root:

```console
uv tool install .
tkn-audio-transcriber --help
tkn-audio-transcriber config show
```

Reinstall after changing source code, packaged resources, dependencies, package
metadata, or the entry point:

```console
uv tool install . --reinstall
```

## Initial configuration

Create the user configuration from the example bundled with the application, then
edit it:

```console
tkn-audio-transcriber config init
```

The command writes `~/.tkn/audio_transcriber/config.yaml`, reports its absolute
path, and returns `unchanged` when its content already matches the example. It
does not overwrite edited content unless `--force` is specified; forced replacement
creates a backup. To create a working-directory override instead, pass its path:

```console
tkn-audio-transcriber config init .tkn/config.yaml
```

Configuration can be loaded from these locations:

- user setting: `~/.tkn/audio_transcriber/config.yaml`
- working-directory override: `./.tkn/config.yaml`

The real `./.tkn/config.yaml` is ignored by Git. Transcript outputs default to
the current working directory. The equivalent explicit setting is:

```yaml
schema_version: "3.0.0"
folders:
  output: .
```

The configuration is grouped by role:

- `transcription`: the local/cloud boundary, provider, and mode-specific profiles
- `folders`: output, local model, download cache, and durable state locations
- `processing`: `ffmpeg`, heartbeat, and retained-working-file behavior

The packaged example separates local and cloud settings. Local profiles are
`local-small`, `local-large`, `gpu-quality`, and `gpu-fast`; the cloud profile is
`azure-ja`. Select the normal mode and profile in YAML, or override both for one
invocation with `--profile MODE/NAME`:

```yaml
transcription:
  active:
    mode: local
    profile: local-small
```

```console
tkn-audio-transcriber config profiles
tkn-audio-transcriber --profile local/gpu-quality transcribe "C:\path\to\meeting.flac"
```

On the first transcription, a missing known model is downloaded automatically
from Hugging Face. The built-in models are public, so no Hugging Face account,
login, or access agreement is required. An internet connection is needed only
for the initial download. You can also download a model in advance:

```console
tkn-audio-transcriber model download small
```

`small` is an official multilingual OpenAI Whisper model-size name, not an
application-specific label. It has about 244 million parameters. This CLI maps
it to the CTranslate2-converted `Systran/faster-whisper-small` repository for
local inference.

Preview the download without writing:

```console
tkn-audio-transcriber model download small --dry-run
```

## First transcription

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac"
```

You can select the output for a single run instead of configuring it:

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac" ^
  --output-dir "C:\path\to\transcripts"
```

In PowerShell, use a backtick instead of `^` for multiline commands.

Preview all resolved paths and the input fingerprint without creating output,
state, cache, or report files. This example uses the current working directory
as the output directory:

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.flac" --dry-run
```

## Azure Speech Fast Transcription

Keep real resource names in a user or ignored working-directory config. A committed
example should use placeholders. The values below reflect the supported MVP contract;
replace only the endpoint placeholder with your own resource endpoint.

```yaml
schema_version: "3.0.0"
transcription:
  active:
    mode: cloud
    profile: azure-ja
  cloud:
    provider: azure-speech-fast
    profiles:
      azure-ja:
        endpoint: https://<speech-resource-name>.cognitiveservices.azure.com/
        region: japaneast
        api_version: "2025-10-15"
        locale: ja-JP
        diarization:
          enabled: true
          max_speakers: 8
        request:
          timeout_seconds: 600
          max_retries: 3
```

Azure Speech Fast Transcription does not expose a Whisper-style model-name setting.
Its profile therefore contains the endpoint, locale, diarization, and request behavior
instead of a `model` key.

Preview is completely local: it does not create credentials, get a token, call Azure,
download a model, invoke `ffmpeg`, or create output/state/cache/report/temporary files.
It reports the planned provider, endpoint type, region, API version, locale, and whether
cloud approval is required.

```console
tkn-audio-transcriber --profile cloud/azure-ja transcribe "C:\path\to\meeting.mp4" --dry-run
```

An actual Azure run requires an approval flag that cannot be saved in YAML:

```console
tkn-audio-transcriber --profile cloud/azure-ja transcribe ^
  "C:\path\to\meeting.mp4" --allow-cloud-upload
```

The CLI cannot determine a recording's confidentiality classification. The operator is
responsible for confirming that the selected input is permitted for cloud processing
before adding the flag. Azure charges can begin when the normalized audio POST is
submitted; dry-run, hashing, and local normalization do not call the Speech API.

Authentication uses `DefaultAzureCredential` and the Cognitive Services token scope.
Subscription keys are unsupported. The CLI never runs `az login` or interactive browser
authentication. Sign in beforehand with an approved Entra method. The normalized WAV
must be shorter than two hours and smaller than 250 MB; both limits are checked before
credential or HTTP client creation. Azure receives no video frames and no local chunks.

Automatic retries are limited to 429, retryable 5xx responses, pre-upload connection
failures, and transport failures for which the audio stream is confirmed incomplete.
`Retry-After` is honored. HTTP 400/401/403/413 are not retried. If the upload may have
completed but no response arrived, the command stops with `submission_outcome_unknown`
instead of risking a duplicate paid request.
Azure errors never fall back to `faster-whisper`; select a `local/NAME` profile
explicitly if a separate local run is desired.

## Commands

### `config init`

Creates a complete configuration from the application-owned packaged example.
Use `--dry-run` to preview without writing, or `--force` to back up and replace an
edited file.

```console
tkn-audio-transcriber config init --dry-run
tkn-audio-transcriber config init
```

### `config show`

Prints resolved non-secret settings, each value's source, every configuration
source's schema version, the effective schema version, and any in-memory migration
as JSON. It is read-only and does not create directories.

```console
tkn-audio-transcriber config show
tkn-audio-transcriber --config "C:\path\to\config.yaml" config show
tkn-audio-transcriber --profile local/gpu-quality config show
```

### `config profiles`

Lists named transcription profiles by local/cloud mode, their provider, and the active
selection. It is read-only. Use global `--profile MODE/NAME` to preview another
selection without editing YAML.

```console
tkn-audio-transcriber config profiles
tkn-audio-transcriber --profile local/gpu-quality config profiles
```

There is no independent `--provider` switch. Change providers by selecting a
`local/NAME` or `cloud/NAME` profile. Azure-only options on a local profile, or
Whisper-only options on a cloud profile, are configuration errors.

### `config migrate`

Migrates a flat schema 1.x configuration or schema 2's mixed profile list into schema
3's separated local/cloud hierarchy. The command validates the result, writes a backup
beside the original, and replaces the original atomically.
Preview the operation with `--dry-run`. Without a path it targets the user config.

```console
tkn-audio-transcriber config migrate --dry-run
tkn-audio-transcriber config migrate
tkn-audio-transcriber config migrate .tkn/config.yaml
```

### `model download`

Downloads a `faster-whisper` model into the configured model directory. It uses
Hugging Face network access but does not touch source media or transcript output.
An existing complete model returns `unchanged`. Use this command to prepare a
machine before transcription or while network access is available.

```console
tkn-audio-transcriber model download small
tkn-audio-transcriber model download medium --model-dir "D:\models"
```

### `transcribe`

The command validates and hashes the source, normalizes a derived copy, recognizes
speech with the selected provider, verifies the source is unchanged, then commits
validated outputs. Local mode creates resumable chunks; Azure mode sends the complete
normalized WAV in one request.

For a video file, `ffmpeg` selects the first audio stream (`0:a:0`) and discards the
video stream. The original video remains unchanged, and output names use its file stem.

```console
tkn-audio-transcriber transcribe "C:\path\to\meeting.m4a" ^
  --output-dir "C:\path\to\transcripts" ^
  --model small --language ja --chunk-seconds 600
```

An MP4 recording uses the same command:

```console
tkn-audio-transcriber transcribe "C:\path\to\town-hall.mp4" ^
  --output-dir "C:\path\to\transcripts" ^
  --model small --language en
```

Important safety options:

- `--dry-run`: no output, state, cache, or report changes
- `--overwrite`: replace only existing differing or incomplete final outputs;
  without it the command stops
- `--keep-working-files`: retain the normalized WAV and chunks after successful
  verification; checkpoints are always retained for audit/resume
- `--allow-cloud-upload`: approve an Azure upload for this invocation only; it is never
  read from configuration

When the selected mode is `local`, if its known model (`tiny`,
`base`, `small`, `medium`, or `large-v3`)
is missing, the command downloads it automatically before audio processing.
`--dry-run` never downloads a model. If a run is interrupted, repeat the same
`transcribe` command; completed chunks are skipped.

Before processing, the CLI estimates scratch-space needs. After normalization, it
checks the actual WAV size before creating chunks. It refuses to commit final output if
the normalized WAV format is invalid or total chunk duration differs from decoded audio.
For local transcription, segment timestamps that partially exceed their decoded chunk
are clipped to its boundary, while segments wholly outside decoded audio are discarded.
Adjustment counts and decoded audio checks are recorded under `decoded_audio` in the
manifest. Each stage and heartbeat is stored in the job's `job.json` and `run.jsonl`.

### `cleanup`

Applies retention rules to completed job state. The default is a read-only plan;
`--apply` is required to delete anything.

```console
tkn-audio-transcriber cleanup --older-than-days 30
tkn-audio-transcriber cleanup --older-than-days 30 --apply
```

A job is eligible only when it is `completed`, older than the retention period, and its
final manifest plus every output size and SHA-256 validate successfully. Running,
failed, or invalid jobs are preserved. Model caches, the Hugging Face cache, and final
transcript outputs are outside cleanup scope. Applied checkpoint deletion is not
reversible, although the validated final outputs remain.

### `status`

Reads durable job state and prints the stage, current chunk, PID, latest heartbeat,
latest checkpoint, and run-log location as JSON. It is read-only.

```console
tkn-audio-transcriber status
tkn-audio-transcriber status --state-dir "D:\transcription-state"
```

The default heartbeat interval is 60 seconds and can be changed with
`--heartbeat-seconds` or config. A heartbeat shows that the in-process recognition call
is alive; it is not a hard ASR timeout.

### `validate`

Checks manifest schema plus the size and SHA-256 of every output. Add
`--verify-source` to re-hash the original recording as well.
New runs write manifest schema 3, including selected-profile and
provider/authentication/API provenance; validation remains compatible with existing
schema 1 and 2 manifests.

```console
tkn-audio-transcriber validate "C:\path\to\meeting__local__local-small_transcript.manifest.json"
tkn-audio-transcriber validate "C:\path\to\meeting__local__local-small_transcript.manifest.json" ^
  --verify-source
```

## Outputs and runtime locations

For `meeting.flac`, the selected output directory receives:

```text
meeting__local__local-small_transcript.md
meeting__local__local-small_transcript.srt
meeting__local__local-small_transcript.jsonl
meeting__local__local-small_transcript.manifest.json
```

The four files share one basename and form a single output set. The
`__local__local-small` portion records the selected `MODE/PROFILE`, so the same
source can be transcribed with another profile without replacing these files.
Existing profile-less output files from earlier versions are left untouched.

| File | Purpose |
| --- | --- |
| `*_transcript.md` | Primary human-readable transcript. Its YAML Frontmatter records the source, selected profile, model, engine, language, speaker-separation status, chunk length, transcriber name, and transcriber version. The body contains timestamped transcript text. Start with this file for reading, review, or downstream summarization. |
| `*_transcript.srt` | Standard subtitle file for media players and video editors. Azure cues include a speaker label when returned; local output is unchanged. |
| `*_transcript.jsonl` | Machine-readable segment data with one JSON object per line. Azure segments add optional `speaker`; local records retain the existing fields. |
| `*_transcript.manifest.json` | Schema 3 provenance and validation record. It stores the selected profile, provider, API version, region, locale, diarization, Entra method, source hash, decoded duration, attempts/retries, tool version, and output hashes. It never stores a token or Authorization header. |

Application-managed runtime data is separated by role:

```text
~/.tkn/audio_transcriber/state/      durable job checkpoints
~/.cache/audio_transcriber/models/  replaceable model files
~/.cache/audio_transcriber/huggingface/ replaceable download cache
```

Final transcript outputs default to the current working directory. State, model,
and cache data remain in the application-managed locations above. Relative paths
in all config sources are resolved from the current working directory.

## Idempotency and failure behavior

- Same input, profile, and transcription settings with valid outputs: `unchanged`
- Same input with a different profile: a separate profile-named output set is `created`
- New outputs: `created`
- Differing outputs with `--overwrite`: `replaced`
- Differing or incomplete outputs without `--overwrite`: error
- Dry run: `planned`

The final result is JSON on standard output. Progress and diagnostics use
standard error in `[LEVEL] message` form. `-q/--quiet` shows only errors;
`-v/--verbose` adds debug details. Interactive terminals render `SUCCESS` in
green and `ERROR`/`CRITICAL` in red when ANSI color is supported. Redirected
output, `NO_COLOR`, `TERM=dumb`, or unsupported Windows consoles use no color.

Final files are built and validated before replacement, and the manifest is
committed last. If the process stops during final multi-file commit, rerun with
`--overwrite`; the durable checkpoint prevents retranscribing completed chunks.

## Configuration precedence

Later sources override earlier sources:

1. built-in defaults
2. `~/.tkn/audio_transcriber/config.yaml`
3. `./.tkn/config.yaml`
4. explicit `--config`
5. configured `transcription.active.{mode,profile}`, or global `--profile MODE/NAME`
6. individual CLI options

Unknown keys, invalid types, and unsupported `schema_version` values are errors.
Every file is validated before deep-merging its nested settings, and `schema_version`
is source metadata, not an overridable setting. `config show` reports the selected
profile, available profiles, source selected for every effective value, and schema
status of every source.

Application-owned configuration uses the independent schema version `"3.0.0"`.
The three-part string is required. This reader accepts schema 3 Patch versions such as
`"3.0.7"` because Patch changes do not alter structure. It rejects newer Minor or Major
versions, older versions without a tested migration, malformed versions, and missing
versions with an actionable error.

Flat versions `1.0.x`, `1.1.x`, the former integer `schema_version: 1`, and mixed-profile
schema `2.0.x` remain readable through an in-memory structural migration and emit a
warning; reading never rewrites a file. Run `config migrate` to persist schema `"3.0.0"`
with validation, backup, and atomic replacement.

## Scheduled operation

Install the CLI with `uv tool install .`, use absolute source-media/output paths,
and run the same `transcribe` command from Windows Task Scheduler or cron. The
command returns `0` on success, `2` for expected configuration/input/validation
errors, `130` for interruption, and `1` for unexpected failures.

## Limitations and privacy

- Speaker diarization is available only with Azure Speech; local `faster-whisper`
  output remains unlabeled
- No meeting summary or generative-AI call
- No automatic terminology correction
- The first run for a missing model requires access to Hugging Face; use
  `model download` in advance for an offline transcription machine
- Azure mode sends derived audio to the configured Speech resource and can incur Azure
  charges. Review the source, run `--dry-run`, then explicitly approve each real upload
- Logs and Azure errors contain only safe diagnostics such as status, Azure error code,
  request ID, and attempt counts; they do not record audio, transcript text, bearer
  tokens, authorization headers, or request/response bodies
- `faster-whisper` runs in-process, so its recognition phase does not have the
  external-process timeout used for `ffmpeg`; heartbeat is available, but the current
  chunk cannot yet be forcibly cancelled from another command
- The manifest records the source media path and hashes for provenance; treat it as
  local operational metadata when paths are sensitive

## Development and verification

```console
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Tests use synthetic files and fake credentials, HTTP clients, speech-recognition, and
`ffmpeg` adapters. They do not call Azure, download a model, or modify a real recording.
>>>>>>> 4890e99 (Handle Whisper timestamp overruns)
