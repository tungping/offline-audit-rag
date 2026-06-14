# Project Changes Log

This file records all code modifications, features, and fixes implemented in this project.

## [2026-06-14] Core Optimizations and Fixes

### 1. RAG Context Retrieval Query Optimization
- **File**: [app.py](file:///Users/tenan/Coding/projects/offline_auto_audit/app.py)
- **Change**: For inputs longer than 500 tokens, we now split them into chunks of size 400 and overlap 100, execute semantic queries for each chunk, and aggregate/deduplicate results to extract the top-k most relevant compliance standards. This significantly improves RAG precision for long meeting logs.

### 2. Service & Model Verification

- **Files**: [app.py](file:///Users/tenan/Coding/projects/offline_auto_audit/app.py) and [webui.py](file:///Users/tenan/Coding/projects/offline_auto_audit/webui.py)
- **Change**: 
  - Added a connection status helper `check_ollama_status()` in `app.py` to check Ollama and verify if required LLM/Embedding models are downloaded.
  - Runs this check at start-up in the CLI and logs helpful errors/warnings if anything is missing.
  - Added a status widget in the WebUI sidebar showing connection status and "Pull Model" buttons to download missing models directly from the UI.

### 3. File Write Stability Checks
- **File**: [app.py](file:///Users/tenan/Coding/projects/offline_auto_audit/app.py)
- **Change**: Integrated `wait_for_file_ready(file_path)` inside the file watcher execution loop, ensuring files are fully copied/written to the `inbox/` folder before commencing audit execution.

### 4. Robust JSON Parser & Fallback
- **File**: [app.py](file:///Users/tenan/Coding/projects/offline_auto_audit/app.py)
- **Change**: Enhanced `extract_json_object` to search for and remove trailing commas in objects or lists using regexes prior to calling `json.loads`. This prevents common parsing failures from small LLM format slips.

### 5. Audio Transcription Integration in WebUI
- **File**: [webui.py](file:///Users/tenan/Coding/projects/offline_auto_audit/webui.py)
- **Change**: Added an "上传音频" tab to upload audio files (MP3, WAV, FLAC, M4A, etc.). The WebUI checks local dependencies (`ffmpeg`, `ffprobe`, `whisper-cli`, model files) and automatically runs offline Whisper transcription and feeds the output into the auditing pipeline.

### 6. Compliance Rules Editor in WebUI
- **Files**: [app.py](file:///Users/tenan/Coding/projects/offline_auto_audit/app.py) and [webui.py](file:///Users/tenan/Coding/projects/offline_auto_audit/webui.py)
- **Change**:
  - Added `rebuild_knowledge_base()` to `app.py` to reset ChromaDB collection and trigger a fresh reload.
  - Added a "合规条款管理" tab in the WebUI where users can list, edit, save, or delete `.txt` compliance rule files, or create new ones, triggering an automatic sync of the vector store database.

### 7. WebUI Compliance Rules Safety Hardening
- **Files**: [webui.py](file:///Users/tenan/Coding/projects/offline_auto_audit/webui.py) and [tests/test_app.py](file:///Users/tenan/Coding/projects/offline_auto_audit/tests/test_app.py)
- **Change**:
  - Added `is_safe_rule_filename()` to reject path traversal, absolute paths, nested paths, empty names, and non-`.txt` filenames when creating compliance rule files from the WebUI.
  - Added `sync_knowledge_base_cache()` so WebUI rule save/delete/create actions rebuild ChromaDB and clear the cached Streamlit collection before the next audit.
  - Added regression tests covering unsafe rule filenames and cache clearing after knowledge base rebuilds.
