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
provider uploads one losslessly compressed mono/16 kHz FLAC only after the command includes
`--allow-cloud-upload`. Neither provider sends audio or transcript text to a
general-purpose LLM.

The boundary is:

- `ffmpeg`: extract the first audio stream and convert it to mono 16 kHz WAV; video
  frames are ignored
- Python code: manage jobs, resume, heartbeat, validation, and output artifacts
- `faster-whisper`: convert local audio chunks into text
- Azure Speech Fast Transcription: transcribe one complete FLAC file and optionally
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
  or a FLAC upload file for the Azure provider
- For `device: cuda` on Windows: an NVIDIA driver, CUDA Toolkit 12 with cuBLAS,
  cuDNN 9 for CUDA 12, and both DLL directories available on `PATH`
- For Azure: an Entra identity with the Speech User role, an interactive desktop with
  a default browser, and network access to Microsoft Entra and the configured Speech
  endpoint. The browser must be able to return to a local `localhost` callback;
  Azure CLI is not required

There is no extension allowlist. Any local audio or video file that `ffmpeg` can decode
and that contains at least one audio stream is accepted. This includes common inputs
such as `.wav`, `.flac`, `.mp3`, `.m4a`, and `.mp4`.

Use CPU with `small` for a lightweight installation check. For transcripts used
as downstream evidence, start with `large-v3` when resources permit. See
[local settings](#local-profile-settings) and the
[CPU/GPU benchmarks](#reference-cpugpu-benchmarks) for the trade-offs.

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

Transcript outputs default to
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

### Local profile settings

These keys belong under `transcription.modes.local.profiles.<name>`. They apply
to local `faster-whisper`, not Azure Speech. The examples below are starting
points, not a guarantee of maximum accuracy.

| Key               | Meaning and practical effect                                                                                                                                                                                                                                                                |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model`         | The recognition model. Prefer multilingual`large-v3` for quality; try `medium`, then `small`, if memory or elapsed time is unacceptable. A larger model does not guarantee every word is better.                                                                                      |
| `language`      | The expected spoken language, e.g.`ja` for Japanese. This CLI requires a non-empty string; blank/null is not an automatic-detection setting. It is not a translation target.                                                                                                              |
| `chunk_seconds` | External audio-file splitting and checkpoint interval, in positive integer seconds. Start at`600`. Shorter chunks reduce work lost on interruption but introduce more boundaries that may cut speech.                                                                                     |
| `beam_size`     | The number of candidate paths explored during decoding.`5` is a quality-oriented starting point; `3` or `1` can shorten processing. Larger beams do not guarantee fewer errors or hallucinations.                                                                                     |
| `compute_type`  | Numerical precision/quantization. Start with`float16` on CUDA and `int8` on CPU. CPU `float32` is supported, but uses more resources and can be slower. This can change recognized text, not only speed; higher numerical precision is not necessarily higher transcription accuracy. |
| `device`        | Where inference runs:`cpu` or NVIDIA `cuda`. CUDA requires the runtime below. A GPU enables faster execution of demanding settings; it does not inherently make the same model more accurate.                                                                                           |

| Use case                                   | Model                   | Beam | Compute     | Device   |
| ------------------------------------------ | ----------------------- | ---: | ----------- | -------- |
| CUDA, quality-oriented default             | `large-v3`            |    5 | `float16` | `cuda` |
| CPU, quality-oriented comparison candidate | `large-v3`            |    5 | `int8`    | `cpu`  |
| CPU, everyday resource/time balance        | `large-v3`            |    1 | `int8`    | `cpu`  |
| CPU, intermediate speed/quality experiment | `large-v3`            |    3 | `int8`    | `cpu`  |
| CPU, lower-memory or quick preview         | `medium` or `small` |    1 | `int8`    | `cpu`  |

Keep `language: ja` and `chunk_seconds: 600` for comparable Japanese tests.
These combinations do not add or rename packaged profiles. `large-v3` is a
reasonable starting point on a 32 GB CPU-only laptop, but sustained CPU load,
cooling, and other applications can make waiting time or responsiveness the
limiting factor. Reducing the beam mainly reduces decoding work and elapsed
time; it is not a CPU-usage cap. CPU threads are not currently a profile setting.
Use `float32` as an A/B reference when a specific error matters, not as an
automatic upgrade. Consult [CTranslate2&#39;s compute-type documentation](https://opennmt.net/CTranslate2/quantization.html)
for supported types and hardware-dependent fallbacks.

#### Chunking and downstream quality

The current implementation splits audio at fixed time boundaries without
overlap, rather than finding sentence boundaries. This is separate from
[Whisper&#39;s internal approximately 30-second windows](https://github.com/openai/whisper#python-usage).
The local adapter enables VAD (speech detection; minimum silence 500 ms), uses
`temperature=0`, and disables previous-text conditioning
(`condition_on_previous_text=False`). Consequently, longer external chunks do
not make the model consider the entire meeting as one context. Enabling previous
text would be a separate code change, with a consistency-versus-repetition
trade-off described in the [faster-whisper API](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py).

A follow-up on the 758.8-second benchmark audio compared `600` (two external
chunks) with `900` (one chunk), using `large-v3`, beam 5, float16, CUDA, and
Japanese. Both took 41 tracked seconds. The first 214 segments were identical
through 09:40.92, while the one-chunk result added suspicious name-like text near
the end that was absent with `600`. This single recording does not show a quality
benefit from `900`, so `600` remains the recommended starting point. The comparison
used application 0.7.0 for `600` and 0.7.1 for `900`, had no ground-truth
transcript, and therefore does not prove that `600` is universally better.
When testing another value, keep application/model versions fixed and review both
the boundary and the rest of the recording against the audio. `0` is not a
supported way to disable splitting.

Keep source audio and raw transcripts unchanged. Context/glossary-assisted
correction can improve domain terms downstream, but fluent text can also hide
incorrect additions or omissions. Save corrections separately with source
timestamps and an edit trail; verify names, numbers, dates, negation, and speaker
attribution against audio before using them for decisions. Local ASR does not
perform speaker separation: name-like prefixes in its text are not verified
speaker labels. Downstream AI must also respect the source's data-handling policy.

### Windows GPU runtime (`device: cuda`)

CPU profiles do not require CUDA, cuBLAS, or cuDNN. A Windows profile with
`device: cuda` runs `faster-whisper` through CTranslate2 and requires these NVIDIA
runtime DLL families:

| DLLs checked before transcription                                                            | Supplied by              |
| -------------------------------------------------------------------------------------------- | ------------------------ |
| `cublas64_12.dll`, `cublasLt64_12.dll`                                                   | CUDA Toolkit 12 (cuBLAS) |
| `cudnn_ops64_9.dll`, `cudnn_cnn64_9.dll`, `cudnn_adv64_9.dll`, `cudnn_graph64_9.dll` | cuDNN 9 for CUDA 12      |

Use NVIDIA's graphical installers rather than copying DLLs manually. Install
[CUDA Toolkit 12](https://developer.nvidia.com/cuda-toolkit-archive), which includes
cuBLAS, then install the Windows x86-64 FULL package from
[cuDNN Downloads](https://developer.nvidia.com/cudnn-downloads) with CUDA 12 selected.
See NVIDIA's [cuDNN Windows installation
guide](https://docs.nvidia.com/deeplearning/cudnn/installation/latest/windows.html)
for the supported installer choices.

Both directories containing the DLLs must be on `PATH`. For example, installer
versions 12.9 and 9.24 use paths like:

```text
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin
C:\Program Files\NVIDIA\CUDNN\v9.24\bin\12.9\x64
```

Use the paths created by the versions actually installed, and update `PATH` when those
versions or locations change. Open a new terminal after editing `PATH`, then verify:

```powershell
where.exe cublas64_12.dll
where.exe cudnn_ops64_9.dll
```

Before an actual CUDA transcription, the CLI checks that all DLLs in the table are
present on `PATH` and loadable. The check runs before source hashing, model resolution
or download, and media processing. A missing or unloadable DLL returns an actionable
expected error with exit code `2`; the CLI does not install the runtime or modify
`PATH`. A `--dry-run` does not load the GPU runtime.

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

Authentication is fixed to
[`InteractiveBrowserCredential`](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.interactivebrowsercredential?view=azure-python)
with the Cognitive Services token scope. No authentication settings or secrets need to
be added to `config.yaml`; subscription keys are unsupported.

1. Before each new Azure submission, the CLI opens the default browser and requests
   an account picker (`prompt=select_account`), even if the browser is already signed in.
2. Select the work or school account with access to the configured Speech resource and
   complete any sign-in, consent, or MFA required by Microsoft Entra. Account selection
   does not force password re-entry or a browser sign-out.
3. After authentication completes, the CLI obtains the token and submits the audio.
   HTTP retries within that submission reuse the selected identity without another picker.

The CLI does not use `DefaultAzureCredential`, Azure CLI sign-in, environment-based
credentials, or a shared token cache as fallback. Authentication failure, cancellation,
or timeout stops before upload. The callback wait is five minutes; closing the browser
may leave the CLI waiting until that timeout, so use Ctrl+C to stop immediately.
`--dry-run`, missing upload approval, and reuse of completed outputs or a completed
Azure response checkpoint do not open the browser.

Tokens and the SDK authentication record are kept only in process memory; the CLI
does not persist them or the selected account name to config, job state, manifests,
or logs. Browser cookies remain managed by the browser. Dry-run reports
`authentication_method: InteractiveBrowserCredential` and `account_selection_required: true`;
manifests for new submissions record the same authentication method. Resumed checkpoints
retain their recorded method; legacy checkpoints without that field report `unknown`
instead of claiming browser authentication for a past submission.

This implementation uses the SDK's default Azure development application, not a
private client or tenant ID embedded in this repository. Tenant consent policies can
block that application. A dedicated Entra public-client app registration is recommended
for production/distributed use; custom client/tenant configuration is not implemented
yet. The SDK default targets work or school accounts in Azure Public Cloud.

The CLI validates a local mono/16 kHz/16-bit PCM WAV, then losslessly encodes it to FLAC
for upload (`audio/flac`). Compression preserves the normalized samples; the sample rate
and channel count do not change. No extra configuration is required. The CLI retains its
existing limit of audio shorter than two hours; the 250 MB upload limit applies to the
encoded FLAC, not the intermediate WAV. Duration is checked before encoding, and FLAC
size is checked before credential or HTTP client creation. Azure receives no video frames,
intermediate WAV, or local chunks. The preview, job settings, and manifest settings report
`upload_format: flac`. Encoding failures stop before any authentication or upload; a retry
regenerates the FLAC rather than reusing a potentially partial file. A completed Azure
response checkpoint is reused without encoding or submitting again.

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
losslessly encoded FLAC in one request.

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
- `--keep-working-files`: retain the normalized WAV, cloud FLAC, and local chunks after successful
  verification; checkpoints are always retained for audit/resume
- `--allow-cloud-upload`: approve an Azure upload for this invocation only; it is never
  read from configuration

When the selected mode is `local`, if its known model (`tiny`,
`base`, `small`, `medium`, or `large-v3`)
is missing, the command downloads it automatically before audio processing.
`--dry-run` never downloads a model. If a run is interrupted, repeat the same
`transcribe` command; completed chunks are skipped.

`chunk_seconds` (`--chunk-seconds` on the CLI) is a positive integer that controls how
often the normalized WAV is split in local mode. It defaults to `600` seconds (10
minutes) and is not used in Azure mode. Shorter chunks create checkpoints more often and
reduce the amount repeated after an interruption, but increase splitting, recognition,
and persistence overhead. Longer chunks reduce that overhead but increase the amount
repeated when processing stops partway through a chunk. Omitting the setting does not
disable chunking or make processing faster; it uses the `600`-second default.

Use `600` for typical recordings. About `300` seconds can suit unstable environments or
frequent interruptions, while `900` to `1800` seconds can suit a stable GPU environment
where less frequent checkpoints are preferred. The value mainly trades processing
overhead against resumability, but chunks do not overlap, so values that are too short
can split words or sentences at boundaries and increase omissions or recognition errors.

Before processing, the CLI estimates scratch-space needs. After normalization, it
checks the actual WAV size before creating chunks or encoding cloud FLAC. It refuses to commit final output if
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

| File                           | Purpose                                                                                                                                                                                                                                                                                                                          |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `*_transcript.md`            | Primary human-readable transcript. Its YAML Frontmatter records the source, selected profile, model, engine, language, speaker-separation status, chunk length, transcriber name, and transcriber version. The body contains timestamped transcript text. Start with this file for reading, review, or downstream summarization. |
| `*_transcript.srt`           | Standard subtitle file for media players and video editors. Azure cues include a speaker label when returned; local output is unchanged.                                                                                                                                                                                         |
| `*_transcript.jsonl`         | Machine-readable segment data with one JSON object per line. Azure segments add optional`speaker`; local records retain the existing fields.                                                                                                                                                                                   |
| `*_transcript.manifest.json` | Schema 3 provenance and validation record. It stores the selected profile, provider, API version, region, locale, diarization, Entra method, source hash, decoded duration, attempts/retries, tool version, and output hashes. It never stores a token or Authorization header.                                                  |

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

For unattended operation, install the CLI with `uv tool install .`, use absolute
source-media/output paths, and run a local profile from Windows Task Scheduler or cron.
Cloud profiles require an interactive desktop and browser account selection for each
new submission; they are not suitable for unattended jobs. The
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

## Reference CPU/GPU benchmarks

### Desktop CPU and CUDA comparison (2026-09-05 review)

A 758.8-second (12:38.8) Japanese conversational FLAC was transcribed on an
Intel Core i9-13900K (24 physical cores / 32 logical processors), approximately
64 GB RAM, and an NVIDIA GeForce RTX 4070 Ti (12 GB VRAM). The operator confirmed
that the CPU and GPU runs came from this PC. All runs used `language: ja` and
`chunk_seconds: 600` (two external chunks).

| Profile                                     | Model        | Beam | Compute     | Device   | Tracked elapsed | Segments |
| ------------------------------------------- | ------------ | ---: | ----------- | -------- | --------------: | -------: |
| `local/gpu-quality`                       | `large-v3` |    5 | `float16` | `cuda` |             41s |      271 |
| `local/cpu-large-b1`                      | `large-v3` |    1 | `int8`    | `cpu`  |           3m41s |      276 |
| `local/cpu-large-b3`                      | `large-v3` |    3 | `int8`    | `cpu`  |           4m46s |      274 |
| `local/cpu-large-b5`                      | `large-v3` |    5 | `int8`    | `cpu`  |           5m30s |      271 |
| `local/cpu-large-b3c32`                   | `large-v3` |    3 | `float32` | `cpu`  |          11m25s |      278 |
| `local/cpu-small-b3`                      | `small`    |    3 | `int8`    | `cpu`  |             56s |      186 |
| `local/cpu-small-b5`                      | `small`    |    5 | `int8`    | `cpu`  |           1m28s |      170 |
| `local/cpu-fallback` (legacy, 2026-09-04) | `small`    |    1 | `int8`    | `cpu`  |           1m00s |      167 |

Two additional runs, `local/cpu-quality` (beam 3, 5m02s) and
`local/cpu-quality-b5` (beam 5, 5m40s), produced byte-identical JSONL to
`cpu-large-b3` and `cpu-large-b5`, respectively. The ten artifact sets therefore
contain eight distinct JSONL results. These CPU names are custom test profiles,
not additional packaged profiles. `gpu-fast` was not measured.

The manifests record the same source SHA-256 and duration. All 30 MD/SRT/JSONL
files matched their recorded hashes and sizes; segment counts and timestamp
ranges also passed checks. Job logs contained one start, no failures, and two
checkpoints per run. Elapsed time is `job.started_at` to `manifest.completed_at`,
not command-launch-to-exit time: source hashing and model resolution/download
before job start are excluded. GPU used application 0.7.0, the legacy small run
0.6.0, and the other runs 0.7.1. Per-run library/model revisions, peak RAM/VRAM,
CPU utilization, background load, and thermals were not recorded.

Text review, without an audio-verified reference transcript, suggests:

- `large-v3` is the stronger starting point than `small` for downstream evidence.
  The GPU profile is the first choice in this sample for its speed and fewer
  suspicious text artifacts, but domain terms still need review.
- More beam search is not uniformly better. CPU beam 1 avoided name-like
  prefixes found in beam 3 and 5; beam 5 also omitted some short responses
  retained by beam 1. CPU beam 3 had an additional suspicious closing phrase.
- CPU float32 changed some phrases plausibly for the better, but retained
  suspicious name-like prefixes and took about **2.40 times** as long as int8
  with the same beam 3. This is not evidence of a general accuracy improvement.
- CPU int8 beam 5 took about **15% longer** than beam 3 and **49% longer** than
  beam 1. GPU float16 beam 5 took roughly one eighth of CPU int8 beam 5's time;
  device, compute type, and application version differ in that comparison.

There is no accuracy score or proven CPU quality winner here. One private
recording, single runs, and segment counts cannot establish a universal optimum.
The legacy small timing is not a controlled beam-only comparison. Re-run on
representative audio and the actual target PC before adopting a profile.

### CPU-only laptop: historical reference (2026-09-04)

Retained from the earlier README measurement, not re-benchmarked in the desktop
review: Dell Latitude 7340, Core i7-1365U (10 physical cores / 12 logical
processors), 31.6 GB RAM, no CUDA GPU. The same-duration Japanese FLAC used
application 0.7.0, `int8`, `ja`, and `chunk_seconds: 600`.

| Profile at measurement time | Model        | Beam |                        Elapsed | Segments |
| --------------------------- | ------------ | ---: | -----------------------------: | -------: |
| `local-small`             | `small`    |    1 |                          3m24s |      167 |
| `local-large`             | `large-v3` |    1 | ~17m43s[^cpu-benchmark-resume] |      270 |
| `local-quality` (custom)  | `large-v3` |    3 |                         34m26s |      267 |

The earlier qualitative review reported a major gain from `small` to `large-v3`
and only a modest gain from beam 1 to 3, without a ground-truth transcript.
This does not establish that beam 3 is always better. CPU beam 5 and float32
have not been measured on this laptop; do not extrapolate their times directly
from the desktop. For everyday use, `large-v3` / int8 / beam 1 is a reasonable
starting point, with larger beams tested when extra waiting time is acceptable.

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

[^cpu-benchmark-resume]: Approximate cumulative active time: a 17m16s initial
       run reached a validation failure, followed by a 27s resume after the
       timestamp-validation fix. This was not a clean single-run benchmark.
