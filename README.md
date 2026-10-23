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

Open the local address shown by Vite (normally `http://127.0.0.1:5173`). Choose a folder, analyze it, toggle any target groups you want to keep at the root, and explicitly confirm the remaining moves. The current React build is ready for local use; add Tauri packaging after validating the workflow so a packaged Windows app can launch the Python API as a sidecar.
Pick models with `-m` (repeat for fallbacks) or `ORGANIZER_MODELS` in `.env`:
`gemini/gemini-2.5-flash`, `ollama/llama3.2`, `openai/<model>`, `deepseek/deepseek-chat`.
Default: Gemini first, local Ollama as fallback.

## How it works
`scanner` (file facts) -> `extractor` (text snippet from txt/code/PDF/DOCX) -> `planner` (batches files to the LLM, validates output) -> `executor` (dry run / move / undo log).
`classifier` wraps litellm so one code path serves every provider, with model fallback and JSON repair.

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
`python -m unittest discover -s tests -v` (no API key needed)
