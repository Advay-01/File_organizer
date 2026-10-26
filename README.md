# File Organizer

Sorts files into topic folders (`AI/`, `DSA/`, `Projects/`, ...) by reading their **content**, using any LLM provider.

## Setup
```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # then add your key(s)
```

## Usage
```
python -m app.main C:\path\to\folder                 # dry run (safe default)
python -m app.main C:\path\to\folder --apply         # move files
python -m app.main C:\path\to\folder -o C:\Sorted --apply
python -m app.main C:\path\to\folder --recursive
python -m app.main C:\path\to\folder --undo          # revert last --apply
```

## Desktop UI
```
python -m app.gui
```
1. Click **Browse** and select the folder you want to organize.
2. Click **Preview Plan**. This only scans files and asks the configured model for suggestions; it does not move anything.
3. Review the destination and reason for every file, then click **Organize**.
4. Accept the confirmation dialog to move the files. If you do not confirm, nothing changes.

The UI creates topic folders inside the selected folder and uses the same safe planner, executor, collision handling, and undo log as the command-line interface. Leave the Models field blank to use `ORGANIZER_MODELS` from `.env` or the normal defaults; otherwise enter comma-separated model names in fallback order.

The plan creates a dynamic `Category/Subcategory` path from each file's name, format, and content. It creates broad useful subcategories on the spot—not from a fixed list—while avoiding one-off folders for tiny differences. For example, Physics files might become `Physics/Lab Reports` or `Physics/Formula Sheets`. Both folder levels are validated and shown in the preview before confirmation.

Every proposed target subfolder starts **Selected** in the preview. Select any file in a target and click **Toggle Target** (or double-click its row) to switch it to **Keep in root**. That entire target group is excluded from organization, so its files are not moved.

## React Desktop UI
The `frontend/` folder contains the dark, electric-violet Sortly interface. It communicates only with the local API in `app/api.py`; the API stores the exact preview plan and never re-runs classification when you confirm.

In one PowerShell window, start the local API from the project root:
```
.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8765
```

In a second PowerShell window, start the React interface:
```
cd frontend
npm install
npm run dev
```

Open the local address shown by Vite (normally `http://127.0.0.1:5173`). Choose a folder, analyze it, toggle any target groups you want to keep at the root, and explicitly confirm the remaining moves.
Pick models with `-m` (repeat for fallbacks) or `ORGANIZER_MODELS` in `.env`:
`gemini/gemini-2.5-flash`, `ollama/llama3.2`, `openai/<model>`, `deepseek/deepseek-chat`.
Default: Gemini first, local Ollama as fallback.

### Refine a preview with chat

After **Analyze folder**, use **Refine with chat** to ask questions or give optional
instructions such as “Put DSA notes and LeetCode solutions together” or “Leave
Projects alone”. Chat uses the models configured in **Providers**, including their
fallback order. It sends non-sensitive filenames, folder counts, proposed categories,
and short redacted evidence excerpts; sensitive files are omitted from model context
and chat rules leave them unchanged. Chat does not create permission-protected folders.

The local `POST /api/chat` endpoint accepts `plan_id`, `message`, and optional current
per-file review decisions (`files`, with the same shape as Apply). The model returns
`ChatResult`: `reply`, `rules`, and `needs_clarification`. Each rule matches exactly
one topic, extension, or relative glob and either moves matching files to a validated
one- or two-level folder or excludes them. Questions and clarification return no rules.
Topic matches are resolved against known non-sensitive file IDs; paths and extension/
glob matches are checked locally. Newer instructions override earlier rules for the
same files and conflicts are reported in the reply.

The response contains `result` and an optional revised `preview`. Review that preview,
then choose **Review & organize** and **Confirm moves**. Chat never executes moves;
Apply uses the stored plan and existing safety checks and undo support. Chat rules
and conversation context belong to the current preview session and are cleared on
a fresh scan or service restart. Chat refinement is limited to files included in
the preview, so enable **Include subfolders** before analyzing nested files.

### Tauri 2 desktop app

The Windows desktop shell launches the bundled Python API on a dynamically selected
loopback port and gives the React frontend the actual address after a health check.

#### Clean Windows build prerequisites

- Windows with PowerShell and Python 3.10 or newer available as `python.exe`.
- Node.js and npm. The repository does not pin a Node.js version; use a current
  supported LTS release.
- Visual Studio Build Tools with the **Desktop development with C++** workload,
  including the MSVC x64 tools. The build locates them with `vswhere.exe`.
- Internet access for npm packages, Python packages, project-local Rust, and
  NSIS tools on the first build.

#### Build

Run these commands from the repository root in PowerShell:

```powershell
npm install
cd frontend
npm install
cd ..
npm run rust:setup
```

```powershell
npm run desktop:build
```

The build automatically creates the project-local `.pyinstaller-venv` if it is
missing, installs `requirements-build.txt` (the application runtime
dependencies from `requirements.txt` plus pinned PyInstaller), and runs
PyInstaller with that environment's interpreter. It then removes only known
generated outputs, rebuilds and stages the sidecar, prepares local NSIS tools,
builds the frontend, and packages the Tauri application. Rust is kept in the
project-local `.cargo` and `.rustup` directories.

The generated NSIS installer is under
`src-tauri\target\release\bundle\nsis\` (the filename includes the product
version and architecture). The sidecar is bundled from
`src-tauri\binaries\sortly-api\`. Provider credentials are never bundled. For
a packaged build, place `sortly.env` in the Tauri application configuration
directory or set `SORTLY_ENV_FILE` to an external environment file before
launch.

### Provider Studio

Open **Provider Studio** in the desktop app to choose the provider and editable
model ID used by Analyze. API keys are write-only in the UI, encrypted with
Windows DPAPI in the app data directory, and never returned to the frontend.

- Use **Refresh models** to retrieve IDs from the selected provider when its API
  supports model discovery. A discovered ID is not treated as verified until
  **Test active configuration** completes a real model request.
- Fallback providers are optional. Add them explicitly and order them in the
  dialog; no provider is selected silently.
- Ollama uses no API key. Start it with `ollama serve`, then refresh models.
- Hugging Face defaults to its OpenAI-compatible router. Enter a full legacy
  `/models/<repository>` endpoint to use text-generation inference instead.
- Environment variables in `.env` remain supported when a Provider Studio key
  has not been stored for that provider.

## How it works
`scanner` (file facts) -> `extractor` (text snippet from txt/code/PDF/DOCX) -> `planner` (batches files to the LLM, validates output) -> `executor` (dry run / move / undo log).
`classifier` wraps litellm so one code path serves every provider, with model fallback and JSON repair.

### Content Intelligence

The React desktop workflow uses a content-first preview:

1. Files are content-fingerprinted and text/metadata evidence is extracted.
2. Common file signatures are compared with extensions.
3. The active Provider Studio configuration classifies bounded batches and proposes
   grounded filenames.
4. Later batches are shown folders selected earlier in the run. Files with identical
   non-sensitive content converge on one supported folder, while each file keeps its
   own filename, extension, and sensitivity result.
5. Code verifies every evidence phrase against extracted content/metadata and caps
   confidence when evidence is weak.
6. Each file can be accepted, rejected, re-categorized, or renamed independently
   before one confirmed move/rename operation.

Evidence extraction and analysis results are cached separately. Extraction is keyed
by a full content hash, so renames reuse extraction while changed content cannot
reuse stale analysis. Analysis keys also include the selected models, known folder
set, custom endpoints, rename mode, and cache format version.

OCR is optional: image OCR runs only when both `pytesseract` and the external
Tesseract executable are available. They are not bundled or currently installed,
so image OCR and OCR for scanned PDFs must be treated as unavailable in this build.
Audio/video tag extraction also remains unsupported because no media-tag runtime is
installed.

### Guarded scanning and sensitive content

Desktop scans reject protected roots and detected project roots, skip `.git`,
`node_modules`, reparse points/junctions, hidden or system files, partial-download
names, files above the configured size, and files modified within the configured
recent-file window. Hidden directories and Sortly's own undo log stay excluded.
Set `ORGANIZER_INCLUDE_HIDDEN_FILES=1` only when hidden files such as `.env`
need deliberate local review; detected credentials remain redacted and
unselected by default. Archive listings are bounded; archives are never extracted.

Likely Aadhaar, PAN, payment-card, API-key, private-key, and financial-document
content is detected locally. Detection is heuristic and can produce false positives
or false negatives. Suspected sensitive files are kept unselected by default, never
go to cloud models unless explicitly enabled, and are redacted before any permitted
model request. Their raw evidence is not persisted in the content cache.

Unusual plans with excessive folders, concentrated destinations, or excessive
`Misc` assignments display warnings and require an additional confirmation.

Files left untouched by scanning are reported with plain-language notice text rather
than an error, including why they were not analyzed. Local heuristics also flag bounded
command patterns such as downloaded-and-executed scripts, encoded commands, destructive
root-folder commands, and reverse-shell indicators. Detection only inspects text:
Sortly never executes inspected file contents. These checks are heuristic and can
produce false positives or false negatives.

## Recognized file types
The organizer scans all normal files. It can give the model content or metadata from:

- Text and subtitles: TXT, SRT, VTT, SVG, and common source-code/text formats.
- Documents: PDF, DOCX, PPTX, XLSX/XLSM, and EPUB.
- Archives: ZIP, TAR, GZIP/TGZ, and RAR. Archives are not extracted; only their file lists are read.
- Images: JPEG, PNG, HEIC, WebP, TIFF, BMP, and GIF. The model receives format, dimensions, available EXIF description, and the filename. For visual subject recognition inside an image, configure a vision-capable model separately.

Classic binary Office formats (`.doc`, `.ppt`, `.xls`) are still scanned and can be sorted by filename, but they do not yet provide readable content to the model.

## Safety
- Dry run by default; `--apply` required to move anything.
- Folder/file names from the LLM are sanitized, so nothing can escape the output folder.
- Hallucinated paths are ignored; unclassified files go to `Misc/`.
- Name collisions get `_1`, `_2` suffixes; nothing is overwritten.
- A destination that appears after the preview is skipped rather than overwritten.
- Every `--apply` is logged for `--undo`.

## Tests
`python -m unittest discover -s tests -v` (no API key needed). Install
`requirements-dev.txt` instead of `requirements.txt` when running the test
suite; it adds test-only dependencies without putting them in the packaged
sidecar environment.
