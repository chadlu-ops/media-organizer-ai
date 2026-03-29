# Patch Notes — Perceptual Video Deduplication Plugin

## Overview
This patch introduces a robust, multi-level video deduplication system designed to identify visually identical content across different resolutions, bitrates, and filenames. By implementing a "Tri-Path" Filter, the media organizer can now detect redundant video files that standard MD5 hashing would miss.

## New Features
- **Tri-Path Deduplication Filter**:
    - **Level 1: Binary Check**: Instant MD5 hash comparison for exact bit-for-bit duplicates.
    - **Level 2: Keyframe 'Squint'**: Extracts a keyframe at the 20% mark, resizes it to 64x64, and generates a perceptual hash (pHash). Highly effective for catching resized or re-encoded versions of the same video.
    - **Level 3: Deep Temporal Hashing**: Generates a 64-bit temporal signature using the `videohash` library. This provides the highest accuracy for finding near-duplicates with temporal variations.
- **Parallel Processing**: Utilizes `multiprocessing.Pool` to scan video headers and generate hashes in parallel, significantly reducing processing time for large media libraries.
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
