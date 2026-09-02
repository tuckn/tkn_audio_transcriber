# tkn_audio_transcriber

[日本語](README_ja.md)

`tkn_audio_transcriber` is a local CLI that turns an audio file, or the audio stream
inside a video file, into a Markdown transcript, SRT subtitles, JSONL segments, and a
provenance manifest. It uses
`ffmpeg` for mono/16 kHz normalization and chunking, then `faster-whisper` for
speech recognition.

The source media file is opened read-only and is never moved, deleted, or overwritten.
The CLI verifies its SHA-256 hash again before committing outputs. Meeting-note
summarization, terminology correction, speaker diarization, and generative AI are
intentionally outside this repository.

## Script, speech-recognition model, and generative-AI boundary

A script alone cannot turn speech into text. The script orchestrates audio conversion,
chunking, resume, validation, and output; an automatic speech-recognition (ASR) model
performs the actual speech-to-text inference. This CLI uses local `faster-whisper`, a
Whisper-family ASR model.

After the model has been downloaded, normal transcription does not require Codex,
Copilot, the OpenAI API, or another generative-AI service. Whisper is itself a machine-
learning model, but this pipeline does not send audio or transcript text to a general-
purpose LLM or API.

The boundary is:

- `ffmpeg`: extract the first audio stream, convert it to mono 16 kHz WAV, and split it
  into chunks; video frames are ignored
- Python code: manage jobs, resume, heartbeat, validation, and output artifacts
- `faster-whisper`: convert each audio chunk into text
- optional generative AI or a person: summarize, organize topics, correct domain terms,
  and polish prose downstream

That final downstream stage is not part of the base CLI.

## Requirements

- Windows 10/11 or Linux
- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` available on `PATH`, or an absolute `ffmpeg_executable` in config
- Enough disk space for a mono 16 kHz WAV and split chunks while a job is running

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
schema_version: "1.0.0"
output_dir: .
```

On the first transcription, a missing known model is downloaded automatically
from Hugging Face. You can also download it in advance:

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
```

### `config migrate`

Migrates one legacy configuration to the current schema. The command validates the
result, writes a backup beside the original, and replaces the original atomically.
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

The command validates and hashes the source, normalizes a derived copy, creates
chunks, resumes any matching checkpoint, recognizes speech, verifies the source
is unchanged, then commits validated outputs.

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

If the configured known model (`tiny`, `base`, `small`, `medium`, or `large-v3`)
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

```console
tkn-audio-transcriber validate "C:\path\to\meeting_transcript.manifest.json"
tkn-audio-transcriber validate "C:\path\to\meeting_transcript.manifest.json" ^
  --verify-source
```

## Outputs and runtime locations

For `meeting.flac`, the selected output directory receives:

```text
meeting_transcript.md
meeting_transcript.srt
meeting_transcript.jsonl
meeting_transcript.manifest.json
```

The four files share one basename and form a single output set:

| File | Purpose |
| --- | --- |
| `*_transcript.md` | Primary human-readable transcript. Its YAML Frontmatter records the source, model, engine, language, speaker-separation status, chunk length, transcriber name, and transcriber version. The body contains timestamped transcript text. Start with this file for reading, review, or downstream summarization. |
| `*_transcript.srt` | Standard subtitle file for media players and video editors. Each cue contains a sequence number, time range, and recognized text. |
| `*_transcript.jsonl` | Machine-readable segment data with one JSON object per line. Each segment contains `index`, `start`, `end`, `text`, and the source `chunk`; use it for scripts, analysis, or alternate rendering. |
| `*_transcript.manifest.json` | Provenance and validation record. It stores the source path and hash, decoded-audio checks, model and settings, and each output's size and SHA-256. Pass this file to `validate` and keep it with the other three outputs. |

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

- Same input and transcription settings with valid outputs: `unchanged`
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
5. individual CLI options

Unknown keys, invalid types, and unsupported `schema_version` values are errors.
Every file is validated before merging, and `schema_version` is source metadata,
not an overridable setting. `config show` reports the source selected for every
value and the schema status of every source.

Application-owned configuration uses the independent schema version `"1.0.0"`.
The three-part string is required. This reader accepts versions in the current
Major through Minor `0`, including newer Patch versions such as `"1.0.7"` because
Patch changes do not alter structure. It rejects newer Minor or Major versions,
older Major versions without a tested migration, malformed versions, and missing
versions with an actionable error.

The former integer `schema_version: 1` remains readable through an in-memory
migration and emits a warning; reading never rewrites a file. Run `config migrate`
to persist `schema_version: "1.0.0"` with validation, backup, and atomic replacement.

## Scheduled operation

Install the CLI with `uv tool install .`, use absolute source-media/output paths,
and run the same `transcribe` command from Windows Task Scheduler or cron. The
command returns `0` on success, `2` for expected configuration/input/validation
errors, `130` for interruption, and `1` for unexpected failures.

## Limitations and privacy

- No speaker diarization
- No meeting summary or generative-AI call
- No automatic terminology correction
- The first run for a missing model requires access to Hugging Face; use
  `model download` in advance for an offline transcription machine
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

Tests use synthetic files and fake speech-recognition/ffmpeg adapters. They do
not download a model or modify a real recording.
