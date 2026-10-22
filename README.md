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
Pick models with `-m` (repeat for fallbacks) or `ORGANIZER_MODELS` in `.env`:
`gemini/gemini-2.5-flash`, `ollama/llama3.2`, `openai/<model>`, `deepseek/deepseek-chat`.
Default: Gemini first, local Ollama as fallback.

## How it works
`scanner` (file facts) -> `extractor` (text snippet from txt/code/PDF/DOCX) -> `planner` (batches files to the LLM, validates output) -> `executor` (dry run / move / undo log).
`classifier` wraps litellm so one code path serves every provider, with model fallback and JSON repair.

## Safety
- Dry run by default; `--apply` required to move anything.
- Folder/file names from the LLM are sanitized, so nothing can escape the output folder.
- Hallucinated paths are ignored; unclassified files go to `Misc/`.
- Name collisions get `_1`, `_2` suffixes; nothing is overwritten.
- Every `--apply` is logged for `--undo`.

## Tests
`python -m unittest discover -s tests -v` (no API key needed)
