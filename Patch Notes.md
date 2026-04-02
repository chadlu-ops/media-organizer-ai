# Patch Notes

## [0.3.0] - 2026-04-02
### ✨ Commit from Log — Zero-Rescan Execution
- **Log-Based Commit**: Users can now execute file operations directly from a previously generated simulation (dry-run) log, completely bypassing AI clustering, deduplication, and orientation scanning. This is the fastest path to finalizing an organization plan.
- **MD5 Integrity Gate**: Before every move/copy, the engine validates the source file's MD5 hash against the value recorded in the log. Files that have been modified, moved, or deleted since the simulation are automatically skipped with a clear reason logged.
- **Pre-flight Confirmation Modal**: A new "Pre-flight Check" dialog displays the log filename, operation count, and action mode (Copy/Move) before execution, with a safety warning about file integrity verification.
- **Adaptive Commit Button**: The "Commit" button in the Action Grid now dynamically changes to "Execute Log" (emerald green) when a valid simulation log is loaded, providing clear visual feedback on the active execution path.
- **CLI Support**: New `--commit-log [PATH]` flag for headless/scripted usage. Example: `python media_organizer.py --commit-log logs/my_scan.csv`
- **Committed Log Output**: After execution, a new log is generated with `[COMMITTED]`, `[SKIPPED:MISSING]`, `[SKIPPED:HASH_MISMATCH]`, and `[SKIPPED:EXISTS]` status prefixes for full auditability.

### New API Endpoints
- `POST /api/commit_log` — Accepts `{ filename: "log.csv" }` and triggers the commit pipeline with real-time progress streaming.

### Fixed
- **Windows Console Encoding**: Replaced Unicode emoji in commit summary output with ASCII-safe tags to prevent `UnicodeEncodeError` on Windows cp1252 terminals.

## [0.2.5] - 2026-04-02
### ✨ Core Engine Stability & Dashboard Recovery
- **Safe File Hashing Pipeline**: Resolved a critical `FileNotFoundError` in Phase 2b (Subfolder Flattening). The engine now pre-calculates MD5 hashes before performing any file move/copy operations, ensuring data integrity even when the original file is relocated.
- **Optimized Hash Caching**: Replaced redundant and broken dictionary comprehensions with a high-performance local hash cache in `phase2b_folder_flattening`, preventing excessive disk I/O and potential crashes.
- **Dashboard State Restoration**: Fixed multiple `ReferenceError` crashes in the visualizer (`isRunning`, `gridSize`) and restored missing logic for `updateBackendRoot`.
- **VisiPics Row View Persistence**: Re-integrated the `DedupeRow` component to ensure the "Rows" view mode functions correctly without layout-breaking component mismatches.

### Fixed
- **Phase 2b Traceback**: Fixed "No such file or directory" crash occurring when `--action move` was paired with `--enable-folder-flattening`.
- **Visualizer Layout Crashes**: Resolved several runtime errors that blocked grid resizing and script execution buttons in the dashboard.

## [0.2.4] - 2026-04-02
### ✨ React Stability & Enhanced Deduplication Context
- **React-Safe Lucide Bridging**: Implemented a custom `LucideIcon` component in `visualizer.html` to wrap all icons. This isolates Lucide's DOM manipulation from React's Virtual DOM, permanently resolving the `DOMException: Node.removeChild` crash during re-renders and component unmounting.
- **Source-Aware Deduplication**: The backend now explicitly tracks the original filename of matched duplicates. The `Match_Source` field has been added to the CSV schema.
- **Improved Diagnostic Feedback**: Updated the "Reason" field for duplicate entries to show "Matched to: [filename]" instead of an MD5 hash, providing immediate clarity on which file was chosen as the original.
- **Dynamic UI Overlays**: The `MediaCard` component now detects the presence of a `Match_Source` and automatically toggles its overlay from "CRC32 Check" to "Matched To", displaying the full original filename.

### Fixed
- **Visualizer Lifecycle Crash**: Resolved persistent React crashes occurring when navigating between clusters or closing the Lightbox in `visualizer.html`.

## [0.2.3] - 2026-04-02
### ✨ Source Root Protection (Advanced)
- **Root-Native File Immunity**: Files already in the source root are now granted "Naming Immunity"—they will never be prefixed with a `[ROOT]` folder label or indexed unless specifically clustered.
- **Selective Sequence-Only Naming**: If a root-native file is clustered into an AI group, it now uses a simplified `OriginalName - Sequence` format, preserving its identity while identifying its group membership.
- **Flattening Bypass**: Subfolder flattening (Phase 2b) now correctly ignores root-native files, preventing redundant moves and renaming operations.

### Fixed
- **Phase 3/4 Call Signatures**: Resolved `TypeErrors` in the main script where `org_root` was missing from phase execution calls.
- **Video Orientation Crash**: Fixed a critical `TypeError` in `detect_video_orientation` where missing return logic caused a "cannot unpack non-iterable NoneType object" failure during Phase 1.
- **Type Hint Consistency**: Standardized `detect_video_orientation` signature to `tuple[str, int, int]`.

## [0.2.2] - 2026-04-02
### ✨ Smart Deployment & Base Root Support
- **Literal Base Root Support**: New option to organize files directly into the source directory (no `/Organized/` folder).
- **Deployment Structure Toggle**: Choose between `Structured` (folders) or `Flat Root` (all files in one place).
- **Copy-Safety Lock**: Automatically disables and forces the `/Organized/` subfolder when in `Copy` mode to prevent root clutter.
- **In-Place Move**: Enables high-speed, local reorganization when `Move` mode + `Base Root` are selected.
- **Total Root Visibility**: Loose files in the source root are now correctly included in all sequences and clusters.
- **UI Structure Preview**: New Info Icon in Phase 05 with a visual tree comparison and safety tooltips.

### Fixed
- **Flattening Discrepancy**: Resolved issue where files already in the root were skipped during subfolder flattening.
- **Interactive Wiring**: Fixed missing parameter propagation in CLI menu mode for video sorting and AI clustering.
- **Granular Sensitivity**: Added sliders for `Min Group Items` and `Name Pattern Sensitivity` for non-AI grouping.
- **Improved Data Gravity**: Files in deeper subfolders are now prioritized as "Originals" to preserve folder context.

## [v0.2.0] - 2026-03-29
### 🎨 UI Density & Stability Update

## Overview
This update focuses on professionalizing the "Tuning Lab" workspace. We've optimized the layout for higher density, improved UI reliability by eliminating React/Lucide DOM conflicts, and introduced a non-intrusive tooltip system for better clarity without clutter.

## New Features
- **Compact Action Grid (2x2)**:
    - Reorganized the main process buttons (`Simulate`, `Force Scan`, `Commit`, `Cleanup`) into a space-efficient 2x2 grid.
    - Reduced button height and padding by ~50% to maximize vertical space for tuning parameters.
- **Interactive Instruction Tooltips**:
    - Replaced the bulky "Simulate first..." paragraph with a sleek info icon next to the "Source Folder" label.
    - Hovering over the icon reveals detailed usage guidelines in a themed popover.
- **Force Rescan Toggle**:
    - Integrated a dedicated "Force Scan" button directly into the main action panel for quick re-indexing of modified directories without changing global parameters.

## Stability & Fixes
- **Console Toggle Reliability Logic**:
    - Resolved the `Uncaught DOMException: Node.removeChild` error by replacing external Lucide icon manipulation with pure React-managed SVG states.
    - The console chevron (^) now correctly flips in real-time when the tray state changes.
- **JSX Architecture Audit**:
    - Hardened the structural integrity of the `visualizer.html` side-panel, fixing accidental code duplication and tag mismatch errors.

---

# Patch Notes v0.1.0 — Perceptual Video Deduplication Plugin
## Overview
This patch introduces a robust, multi-level video deduplication system designed to identify visually identical content across different resolutions, bitrates, and filenames. By implementing a "Tri-Path" Filter, the media organizer can now detect redundant video files that standard MD5 hashing would miss.

## New Features
- **Tri-Path Deduplication Filter**:
    - **Level 1: Binary Check**: Instant MD5 hash comparison for exact bit-for-bit duplicates.
    - **Level 2: Keyframe 'Squint'**: Extracts a keyframe at the 20% mark, resizes it to 64x64, and generates a perceptual hash (pHash). Highly effective for catching resized or re-encoded versions of the same video.
    - **Level 3: Deep Temporal Hashing**: Generates a 64-bit temporal signature using the `videohash` library. This provides the highest accuracy for finding near-duplicates with temporal variations.
- **Parallel Processing**: Utilizes `multiprocessing.Pool` to scan video headers and generate hashes in parallel, significantly reducing processing time for large media libraries.
- **Real-Time Progress Tracking**: Integrated `tqdm` progress bars to provide visual feedback during the video analysis phase, showing estimated completion time and processing speed.
- **Review Queue Integration**: Videos flagged as potential matches in Level 2 or 3 are moved to `Organized/duplicates/review_needed/` and flagged in the audit log, ensuring no content is accidentally deleted.

## New Parameters
- `enable_perceptual_video_dedup` (--enable-perceptual-video-dedup): Toggle the Level 2 "Squint" and Level 3 "Deep" scans.
- `enable_deep_video_scan` (--enable-deep-video-scan): Enable the Level 3 temporal hashing (requires more processing time).
- `video_match_threshold` (--video-match-threshold): Adjust the sensitivity of the temporal match (Default: 8).

## Technical Requirements
- **FFmpeg**: Must be installed and available in the system PATH.
- **Python Libraries**: `imagehash`, `videohash`, `Pillow`.

## Impact on Audit Log
- A new column `Review_Needed` has been added to `migration_log.csv` to highlight files that require manual verification.
