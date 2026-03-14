#!/usr/bin/env python3
"""
Media Organizer — Content-Aware Media Sorting & Grouping
=========================================================
Recursively scans a root directory, sorts videos by orientation, deduplicates
images via MD5, clusters similar images with CLIP + HDBSCAN, renames files
contextually, and writes an audit CSV log.

Usage:
    python media_organizer.py --root /path/to/media
    python media_organizer.py --root /path/to/media --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".heic"}

HASH_CHUNK_SIZE = 8192  # 8 KB reads for MD5
ORGANIZED_DIR_NAME = "Organized"

LOG_COLUMNS = [
    "Original_Path",
    "New_Path",
    "New_Filename",
    "Hash",
    "Cluster_ID",
    "Media_Type",
    "Reason",
    "Confidence",
    "Settings",
    "Near_Misses",
]

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("media_organizer")

# ---------------------------------------------------------------------------
# Parameter Schema — single source of truth for CLI, API, and UI
# ---------------------------------------------------------------------------

PARAM_SCHEMA = [
    # ── Core Pipeline (Plugins) ──────────────────────────────
    {
        "key": "enable_video_sorting", "cli": "--enable-video-sorting", "type": "bool",
        "default": True,
        "label": "Video Sorting",
        "tooltip": "Analyzes FFmpeg metadata to detect orientation; automatically sorts clips into Horizontal or Vertical subfolders.",
        "group": "core",
        "is_plugin": True, "plugin_icon": "video",
    },
    {
        "key": "enable_video_deduplication", "cli": "--enable-video-deduplication", "type": "bool",
        "default": True,
        "label": "Video De-duplication",
        "tooltip": "Uses fast binary hashing to identify exact duplicate video files, moving redundant copies to a dedicated folder.",
        "group": "core",
        "is_plugin": True, "plugin_icon": "copy",
    },
    {
        "key": "enable_deduplication", "cli": "--enable-deduplication", "type": "bool",
        "default": True,
        "label": "Image De-duplication",
        "tooltip": "Generates MD5 checksums for every image to identify byte-for-byte identical files regardless of their filename.",
        "group": "core",
        "is_plugin": True, "plugin_icon": "zap",
    },
    {
        "key": "enable_folder_flattening", "cli": "--enable-folder-flattening", "type": "bool",
        "default": False,
        "label": "Subfolder Flattening",
        "tooltip": "Overrides AI. Moves files from all subfolders into the root Organized folder. Useful for flattening complex directory structures into a single list.",
        "group": "core",
        "is_plugin": True, "plugin_icon": "layers",
    },
    {
        "key": "flatten_rename", "cli": "--flatten-rename", "type": "bool",
        "default": True,
        "label": "Contextual Rename",
        "tooltip": "When flattening, prepends the parent folder name and adds a sequence number (e.g., 'Folder - Name - 001.jpg') based on creation date.",
        "group": "core",
    },
    {
        "key": "enable_ai_clustering", "cli": "--enable-ai-clustering", "type": "bool",
        "default": True,
        "label": "AI Clustering Engine",
        "tooltip": "Analyzes visual content using CLIP embeddings to group images into semantic clusters (e.g., 'Beaches', 'Documents').",
        "group": "core",
        "is_plugin": True, "plugin_icon": "brain",
    },
    # ── General ──────────────────────────────────────────────
    {
        "key": "dry_run", "cli": "--dry-run", "type": "bool",
        "default": True,
        "label": "Dry Run",
        "tooltip": "Preview changes without moving or renaming any files",
        "group": "general",
    },
    {
        "key": "action", "cli": "--action", "type": "choice",
        "choices": ["copy", "move"], "default": "copy",
        "label": "File Action",
        "tooltip": "Copy keeps originals intact; Move deletes them after organizing",
        "group": "general",
    },
    # ── Clustering ───────────────────────────────────────────
    {
        "key": "ai_clustering_mode", "cli": "--ai-clustering-mode", "type": "choice",
        "choices": ["individual", "folder"], "default": "individual",
        "label": "Clustering Strategy",
        "tooltip": "Individual: Clusters every file separately. Folder: Aggregates subfolder contents first to find similar directories/backups.",
        "group": "clustering",
    },
    {
        "key": "min_cluster_size", "cli": "--cluster-min-size", "type": "int",
        "default": 3, "min": 2, "max": 20,
        "label": "Min Cluster Size",
        "tooltip": "Minimum items required to form a group. Higher values lead to larger, more robust clusters but more 'Unsorted' images.",
        "group": "clustering",
    },
    {
        "key": "min_samples", "cli": "--cluster-min-samples", "type": "int",
        "default": 0, "min": 0, "max": 10,
        "label": "Min Samples",
        "tooltip": "Controls noise rejection. 0 balances automatically; increase this to force more stringent grouping requirements.",
        "group": "clustering",
    },
    {
        "key": "epsilon", "cli": "--cluster-epsilon", "type": "float",
        "default": 0.05, "min": 0.0, "max": 1.0, "step": 0.01,
        "label": "Epsilon",
        "tooltip": "The distance threshold for merging clusters. Values above 0 will combine nearby groups found in the feature space.",
        "group": "clustering",
    },
    {
        "key": "method", "cli": "--cluster-selection-method", "type": "choice",
        "choices": ["eom", "leaf"], "default": "eom",
        "label": "Cluster Method",
        "tooltip": "EOM targets global density distribution for fewer large groups; Leaf targets local density for many tight groups.",
        "group": "clustering",
    },
    # ── Feature Weights (Plugins) ─────────────────────────────
    {
        "key": "temporal_weight", "cli": "--temporal-weight", "type": "float",
        "default": 0.3, "min": 0.0, "max": 5.0, "step": 0.05,
        "label": "Temporal Weight",
        "tooltip": "Weights capture time relative to visual content. High values force clusters to stay in chronological proximity.",
        "group": "weights",
        "is_plugin": True, "plugin_icon": "clock",
    },
    {
        "key": "filename_weight", "cli": "--filename-weight", "type": "float",
        "default": 0.0, "min": 0.0, "max": 5.0, "step": 0.05,
        "label": "Filename Weight",
        "tooltip": "Encodes filename similarity into the distance matrix. Use this to maintain existing sequential ordering.",
        "group": "weights",
        "is_plugin": True, "plugin_icon": "file-text",
    },
    {
        "key": "color_weight", "cli": "--color-weight", "type": "float",
        "default": 0.0, "min": 0.0, "max": 5.0, "step": 0.05,
        "label": "Color Similarity Weight",
        "tooltip": "Calculates global color histograms. Higher weights will group images based on their dominant color palettes.",
        "group": "weights",
        "is_plugin": True, "plugin_icon": "palette",
    },
    {
        "key": "near_duplicate_weight", "cli": "--near-duplicate-weight", "type": "float",
        "default": 0.0, "min": 0.0, "max": 5.0, "step": 0.05,
        "label": "Visual Hash Weight",
        "tooltip": "Uses perceptual hashing to bridge the gap between binary duplicates and AI, grouping near-identical burst shots.",
        "group": "weights",
        "is_plugin": True, "plugin_icon": "fingerprint",
    },
    {
        "key": "group_name_matches", "cli": "--group-name-matches", "type": "bool",
        "default": False,
        "label": "Group Exact Names",
        "tooltip": "A non-AI standalone feature. Immediately groups files that share the exact same case-insensitive base name.",
        "group": "name_grouping",
        "is_plugin": True, "plugin_icon": "copy",
    },
    {
        "key": "group_name_prefix", "cli": "--group-name-prefix", "type": "bool",
        "default": False,
        "label": "Group Name Sequences",
        "tooltip": "Detects shared word patterns in filenames to group collections exported with common naming schemas.",
        "group": "name_grouping",
        "is_plugin": True, "plugin_icon": "list-ordered",
    },
    {
        "key": "visual_weight", "cli": "--visual-weight", "type": "float",
        "default": 1.0, "min": 0.0, "max": 5.0, "step": 0.05,
        "label": "Visual Similarity (AI)",
        "tooltip": "The CLIP visual embedding weight. Dominates the grouping process by using AI to understand conceptual similarity.",
        "group": "weights",
        "is_plugin": True, "plugin_icon": "eye",
    },
]

# Build a quick lookup dict: key -> schema entry
_PARAM_MAP = {p["key"]: p for p in PARAM_SCHEMA}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def is_hidden(path: Path) -> bool:
    """Return True if *path* or any of its components is a hidden file/dir."""
    for part in path.parts:
        if part.startswith("."):
            return True
    return False


def safe_action(src: Path, dst: Path, dry_run: bool, action: str = "copy") -> None:
    """Copy or Move *src* -> *dst*, creating parent dirs as needed."""
    if dry_run:
        label = "MOVE" if action == "move" else "COPY"
        log.info("[DRY RUN] %s: %s  ->  %s", label, src, dst)
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    if action == "move":
        shutil.move(str(src), str(dst))
        log.info("Moved   %s  ->  %s", src, dst)
    else:
        shutil.copy2(str(src), str(dst))
        log.info("Copied  %s  ->  %s", src, dst)


def md5_hash(filepath: Path) -> str:
    """Return hex MD5 digest of *filepath*."""
    h = hashlib.md5()
    with open(filepath, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def get_exif_datetime(filepath: Path) -> Optional[datetime]:
    """Extract EXIF DateTimeOriginal from an image, or None."""
    try:
        from PIL import Image
        from PIL.ExifTags import Base as ExifBase

        with Image.open(filepath) as img:
            exif = img.getexif()
            if exif:
                # Tag 36867 = DateTimeOriginal
                dt_str = exif.get(36867) or exif.get(306)  # 306 = DateTime
                if dt_str:
                    return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def get_file_datetime(filepath: Path) -> datetime:
    """Return EXIF date or fall back to file modification time."""
    dt = get_exif_datetime(filepath)
    if dt is not None:
        return dt
    return datetime.fromtimestamp(filepath.stat().st_mtime)


def short_path_hash(path: Path, length: int = 4) -> str:
    """Return a short hex hash derived from the full path string."""
    return hashlib.md5(str(path).encode()).hexdigest()[:length]


# ---------------------------------------------------------------------------
# Phase 1 — Video Sorting
# ---------------------------------------------------------------------------


def detect_video_orientation(filepath: Path) -> str:
    """
    Use ffprobe to determine effective orientation.
    Returns 'landscape' or 'portrait'.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "stream_tags=rotate",
        "-show_entries", "stream_side_data=rotation",
        "-of", "json",
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
    except Exception as exc:
        log.warning("ffprobe failed for %s: %s — defaulting to landscape", filepath, exc)
        return "landscape", 0, 0

    streams = data.get("streams", [])
    if not streams:
        return "landscape", 0, 0

    stream = streams[0]
    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))

    # Check rotation in tags
    rotation = 0
    tags = stream.get("tags", {})
    if "rotate" in tags:
        rotation = int(tags["rotate"])

    # Also check side_data_list for rotation (newer FFmpeg)
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
            break

    # Swap dimensions if rotated 90° or 270°
    if rotation in (90, 270):
        width, height = height, width

    orientation = "landscape" if width >= height else "portrait"
    return orientation, width, height


def phase1_video_sorting(
    root: Path, dry_run: bool, action: str = "copy", enable_deduplication: bool = True
) -> tuple[list[dict], list[Path]]:
    """
    Move video files into videos/horizontal/ or videos/vertical/.
    Also performs exact binary de-duplication if enabled.
    Returns (log_entries, remaining_files_that_are_not_videos).
    """
    log.info("=" * 60)
    log.info("PHASE 1 — Video Sorting & De-duplication")
    log.info("=" * 60)

    entries: list[dict] = []
    non_video_files: list[Path] = []
    seen_hashes: dict[str, Path] = {}

    org_root = root / ORGANIZED_DIR_NAME
    dest_h = org_root / "videos" / "horizontal"
    dest_v = org_root / "videos" / "vertical"
    dup_dir = org_root / "duplicates"

    # 1. Pre-populate hashes from already organized videos to detect duplicates against them
    if enable_deduplication and (org_root / "videos").exists():
        log.info("Indexing existing organized videos for de-duplication...")
        for p in (org_root / "videos").rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                try:
                    h = md5_hash(p)
                    if h not in seen_hashes:
                        seen_hashes[h] = p
                except Exception:
                    pass

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if is_hidden(path):
            continue
        # Skip files already inside our output structure
        try:
            path.relative_to(org_root)
            continue
        except ValueError:
            pass

        ext = path.suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            # --- Check for duplicates if enabled ---
            if enable_deduplication:
                h = md5_hash(path)
                if h in seen_hashes:
                    dest = dup_dir / path.name
                    if dest.exists() or any(e["New_Filename"] == dest.name for e in entries):
                        stem = dest.stem
                        suffix = dest.suffix
                        dest = dup_dir / f"{stem}_{short_path_hash(path)}{suffix}"
                    
                    safe_action(path, dest, dry_run, action=action)
                    entries.append({
                        "Original_Path": str(path),
                        "New_Path": str(dest) if not dry_run else f"[DRY RUN] {dest}",
                        "New_Filename": dest.name,
                        "Hash": h,
                        "Cluster_ID": "duplicate",
                        "Media_Type": "video",
                        "Reason": f"Duplicate Video: Exact MD5 match ({h[:8]})",
                        "Confidence": "1.0 (Bit-for-bit)",
                    })
                    continue
                else:
                    seen_hashes[h] = path

            orientation, w, h = detect_video_orientation(path)
            if orientation == "portrait":
                dest = dest_v / path.name
                subfolder = "vertical"
                reason = f"Video: Vertical ({w}x{h})"
            else:
                dest = dest_h / path.name
                subfolder = "horizontal"
                reason = f"Video: Horizontal ({w}x{h})"

            # Handle name collisions in destination
            if dest.exists() and dest != path:
                stem = dest.stem
                suffix = dest.suffix
                counter = 1
                while dest.exists():
                    dest = dest.parent / f"{stem}_{counter}{suffix}"
                    counter += 1

            safe_action(path, dest, dry_run, action=action)
            entries.append(
                {
                    "Original_Path": str(path),
                    "New_Path": str(dest) if not dry_run else f"[DRY RUN] {dest}",
                    "New_Filename": dest.name,
                    "Hash": "",
                    "Cluster_ID": f"video/{subfolder}",
                    "Media_Type": "video",
                    "Reason": reason,
                    "Confidence": "1.0 (Geometry)",
                }
            )
        else:
            non_video_files.append(path)

    log.info("Phase 1 complete: %d videos sorted.", len(entries))
    return entries, non_video_files


# ---------------------------------------------------------------------------
# Phase 2 — Image De-duplication
# ---------------------------------------------------------------------------


def phase2_deduplication(
    root: Path, files: list[Path], dry_run: bool, action: str = "copy"
) -> tuple[list[dict], list[Path]]:
    """
    Hash images; move duplicates to /duplicates/.
    Returns (log_entries, unique_image_paths).
    """
    log.info("=" * 60)
    log.info("PHASE 2 — Image De-duplication")
    log.info("=" * 60)

    org_root = root / ORGANIZED_DIR_NAME
    dup_dir = org_root / "duplicates"
    entries: list[dict] = []
    unique: list[Path] = []
    seen_hashes: dict[str, Path] = {}

    image_files = [f for f in files if f.suffix.lower() in IMAGE_EXTENSIONS]
    non_image_files = [f for f in files if f.suffix.lower() not in IMAGE_EXTENSIONS]

    for fpath in image_files:
        # Skip files already in our output dirs
        try:
            fpath.relative_to(org_root)
            continue
        except ValueError:
            pass

        h = md5_hash(fpath)
        if h in seen_hashes:
            dest = dup_dir / fpath.name
            # Collision-safe naming
            if dest.exists() or any(
                e["New_Filename"] == dest.name for e in entries
            ):
                stem = dest.stem
                suffix = dest.suffix
                dest = dup_dir / f"{stem}_{short_path_hash(fpath)}{suffix}"

            safe_action(fpath, dest, dry_run, action=action)
            entries.append(
                {
                    "Original_Path": str(fpath),
                    "New_Path": str(dest) if not dry_run else f"[DRY RUN] {dest}",
                    "New_Filename": dest.name,
                    "Hash": h,
                    "Cluster_ID": "duplicate",
                    "Media_Type": "image",
                    "Reason": f"Duplicate: Exact MD5 match ({h[:8]})",
                    "Confidence": "1.0 (Bit-for-bit)",
                }
            )
        else:
            seen_hashes[h] = fpath
            unique.append(fpath)

    # Carry forward non-image, non-video files as-is (they won't be clustered)
    log.info(
        "Phase 2 complete: %d duplicates found, %d unique images remain.",
        len(entries),
        len(unique),
    )
    return entries, unique


def phase2b_folder_flattening(
    root: Path, images: list[Path], dry_run: bool, action: str = "copy", rename: bool = True
) -> tuple[list[dict], list[Path]]:
    """
    Flatten subfolders into the root of Organized.
    Returns (log_entries, remaining_files_not_flattened).
    """
    log.info("=" * 60)
    log.info("PHASE 2b — Subfolder Flattening")
    log.info("=" * 60)

    entries: list[dict] = []
    remaining: list[Path] = []
    
    org_root = root / ORGANIZED_DIR_NAME
    
    # 1. Group by parent folder relative to root
    # Exclusion: files already in Organized/ (shouldn't happen here due to earlier filters)
    folders = defaultdict(list)
    for p in images:
        try:
            # Check if it's in a subfolder relative to root
            rel = p.parent.relative_to(root)
            if str(rel) == ".":
                # In root already
                remaining.append(p)
                continue
            folders[rel].append(p)
        except ValueError:
            # Outside root? (unlikely)
            remaining.append(p)

    # 2. Process each folder
    for rel_path, paths in sorted(folders.items()):
        parent_name = rel_path.name
        # Sort by creation time
        paths.sort(key=lambda x: x.stat().st_ctime if x.exists() else 0)
        
        for i, fpath in enumerate(paths, 1):
            if rename:
                # {Folder} - {Original Name} - {Seq}
                new_name = f"{parent_name} - {fpath.stem} - {i:03d}{fpath.suffix}"
            else:
                new_name = fpath.name
                
            dest = org_root / new_name
            
            # Collision-safe naming
            if dest.exists() or any(e["New_Filename"] == dest.name for e in entries):
                stem = dest.stem
                suffix = dest.suffix
                dest = org_root / f"{stem}_{short_path_hash(fpath)}{suffix}"
            
            safe_action(fpath, dest, dry_run, action=action)
            
            entries.append({
                "Original_Path": str(fpath),
                "New_Path": str(dest) if not dry_run else f"[DRY RUN] {dest}",
                "New_Filename": dest.name,
                "Hash": md5_hash(fpath) if fpath.suffix.lower() in IMAGE_EXTENSIONS else "",
                "Cluster_ID": "flattened",
                "Media_Type": "image" if fpath.suffix.lower() in IMAGE_EXTENSIONS else "other",
                "Reason": f"Flattened from subfolder: {rel_path}",
                "Confidence": "1.0",
            })
            
    log.info("Phase 2b complete: %d files flattened.", len(entries))
    return entries, remaining


# ---------------------------------------------------------------------------
# Phase 3 Helpers — Name Grouping
# ---------------------------------------------------------------------------

def apply_name_grouping_overrides(
    valid_paths: list[Path],
    labels: np.ndarray,
    group_name_matches: bool,
    group_name_prefix: bool
) -> np.ndarray:
    """
    Reassign cluster labels based on exact filename matches or prefix patterns.
    This runs as a post-processing step after AI clustering, or as the primary
    logic when AI is disabled.
    """
    if not group_name_matches and not group_name_prefix:
        return labels

    import re
    from collections import Counter, defaultdict

    # 3d-post-1  Group Absolute Name Matches (case-insensitive stem)
    if group_name_matches:
        log.info("Applying absolute name grouping (case-insensitive stem sync) ...")
        # Build name -> [indices] map
        name_groups: dict[str, list[int]] = defaultdict(list)
        for idx, fpath in enumerate(valid_paths):
            key = fpath.stem.lower()
            name_groups[key].append(idx)

        merged_count = 0
        next_new_label = max(labels) + 1 if len(labels) > 0 else 0

        for name, indices in name_groups.items():
            if len(indices) < 2:
                continue 

            current_labels = [labels[idx] for idx in indices]
            real_labels = [l for l in current_labels if l != -1]

            if real_labels:
                target_label = Counter(real_labels).most_common(1)[0][0]
            else:
                target_label = next_new_label
                next_new_label += 1

            for idx in indices:
                if labels[idx] != target_label:
                    labels[idx] = target_label
                    merged_count += 1

        if merged_count > 0:
            n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
            n_noise = int(np.sum(labels == -1))
            log.info("Name-match grouping: %d file(s) reassigned. Now %d cluster(s), %d outlier(s).", merged_count, n_clusters, n_noise)

    # 3d-post-2  Group Name Prefix (sequence matching)
    if group_name_prefix:
        log.info("Applying signal-based name grouping (auto-detecting shared naming patterns) ...")
        all_tokens = []
        for fpath in valid_paths:
            tokens = [t.strip() for t in re.split(r'[-_\s]+', fpath.stem.lower()) if t.strip()]
            all_tokens.extend(tokens)
        
        token_counts = Counter(all_tokens)
        signals = {t for t, count in token_counts.items() if count > 1 and not t.isdigit() and len(t) > 1}

        def get_core_signals(stem: str) -> str:
            s = stem.lower().strip()
            s = re.sub(r'[\s_-]*(?:copy\s*)?(?:\(\d+\)\s*)?$', '', s)
            tokens = [t.strip() for t in re.split(r'[-_\s]+', s) if t.strip()]
            core_parts = [t for t in tokens if t in signals]
            return " ".join(core_parts)

        prefix_groups: dict[str, list[int]] = defaultdict(list)
        for idx, fpath in enumerate(valid_paths):
            core = get_core_signals(fpath.stem)
            if core and len(core) > 2:
                prefix_groups[core].append(idx)

        merged_count = 0
        next_new_label = max(labels) + 1 if len(labels) > 0 else 0

        for core, indices in prefix_groups.items():
            if len(indices) < 2:
                continue

            current_labels = [labels[idx] for idx in indices]
            real_labels = [l for l in current_labels if l != -1]

            if real_labels:
                target_label = Counter(real_labels).most_common(1)[0][0]
            else:
                target_label = next_new_label
                next_new_label += 1

            for idx in indices:
                if labels[idx] != target_label:
                    labels[idx] = target_label
                    merged_count += 1

        if merged_count > 0:
            n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
            n_noise = int(np.sum(labels == -1))
            log.info("Signal-based grouping: %d file(s) reassigned. Now %d cluster(s), %d outlier(s).", merged_count, n_clusters, n_noise)

    return labels

def phase3_standalone_grouping(
    root: Path,
    image_paths: list[Path],
    dry_run: bool,
    action: str = "copy",
    group_name_matches: bool = False,
    group_name_prefix: bool = False,
) -> tuple[list[dict], dict[int, list[Path]]]:
    """
    Run name-based grouping WITHOUT AI clustering.
    Moves files into sequential groups based on filename patterns.
    """
    if not image_paths:
        return [], {}

    log.info("=" * 60)
    log.info("PHASE 3 (NON-AI) — Name-Based Grouping")
    log.info("=" * 60)

    # All start as noise (-1)
    valid_paths = image_paths
    labels = np.full(len(valid_paths), -1)

    # Apply overrides
    labels = apply_name_grouping_overrides(valid_paths, labels, group_name_matches, group_name_prefix)

    # Move files and build entries
    org_root = root / ORGANIZED_DIR_NAME
    entries: list[dict] = []
    cluster_map: dict[int, list[Path]] = defaultdict(list)

    for fpath, label in zip(valid_paths, labels):
        h = md5_hash(fpath)
        if label == -1:
            dest_dir = org_root / "unsorted"
            cluster_id = "noise"
            reason = "Unsorted: No naming pattern matches found"
        else:
            dest_dir = org_root / "groups" / f"Group_{label:03d}"
            cluster_id = f"group_{label:03d}"
            reason = f"Pattern: Group_{label:03d} name/sequence match"

        dest = dest_dir / fpath.name
        if dest.exists() and dest != fpath:
            dest = dest_dir / f"{dest.stem}_{short_path_hash(fpath)}{dest.suffix}"

        safe_action(fpath, dest, dry_run, action=action)
        actual_dest = dest if not dry_run else fpath
        cluster_map[label].append(actual_dest)

        entries.append({
            "Original_Path": str(fpath),
            "New_Path": str(dest) if not dry_run else f"[DRY RUN] {dest}",
            "New_Filename": dest.name,
            "Hash": h,
            "Cluster_ID": cluster_id,
            "Media_Type": "image",
            "Reason": reason,
            "Confidence": "1.0000",
            "Near_Misses": "{}",
        })

    log.info("Non-AI grouping complete.")
    return entries, cluster_map

# ---------------------------------------------------------------------------
# Phase 3 — Spatiotemporal Clustering (AI)
# ---------------------------------------------------------------------------


def phase3_clustering(
    root: Path,
    image_paths: list[Path],
    dry_run: bool,
    action: str = "copy",
    min_cluster_size: int = 3,
    min_samples: int = None,
    cluster_selection_epsilon: float = 0.0,
    cluster_selection_method: str = "eom",
    temporal_weight: float = 0.3,
    filename_weight: float = 0.0,
    color_weight: float = 0.0,
    near_duplicate_weight: float = 0.0,
    visual_weight: float = 1.0,
    group_name_matches: bool = False,
    group_name_prefix: bool = False,
    ai_clustering_mode: str = "individual",
) -> tuple[list[dict], dict[int, list[Path]]]:
    """
    Generate CLIP embeddings + temporal features, cluster with HDBSCAN.
    Returns (log_entries, cluster_map {cluster_id: [paths]}).
    """
    log.info("=" * 60)
    log.info("PHASE 3 — Spatiotemporal Clustering (%s mode)", ai_clustering_mode.upper())
    log.info("=" * 60)

    if not image_paths:
        log.info("No images to cluster.")
        return [], {}

    # ------------------------------------------------------------------
    # 3a  Visual embeddings via CLIP
    # ------------------------------------------------------------------
    import torch
    import clip
    from PIL import Image
    from tqdm import tqdm

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("Loading CLIP model (ViT-B/32) on %s …", device)
    model, preprocess = clip.load("ViT-B/32", device=device)

    embeddings: list[np.ndarray] = []
    color_features: list[np.ndarray] = []
    hash_features: list[np.ndarray] = []
    valid_paths: list[Path] = []
    timestamps: list[float] = []

    log.info("Embedding images …")
    for fpath in tqdm(image_paths, desc="  Creating embeddings", unit="img", leave=False, file=sys.stdout):
        try:
            img = Image.open(fpath).convert("RGB")
            tensor = preprocess(img).unsqueeze(0).to(device)
            with torch.no_grad():
                feat = model.encode_image(tensor)
            feat = feat.cpu().numpy().flatten()
            feat = feat / (np.linalg.norm(feat) + 1e-10)  # L2 normalize
            embeddings.append(feat)

            # --- Color extraction (average RGB) ---
            thumb_c = img.resize((1, 1))
            c_feat = np.array(thumb_c.getpixel((0,0)), dtype=float)
            c_feat /= (np.linalg.norm(c_feat) + 1e-10)
            color_features.append(c_feat)

            # --- Visual Hash (perceptual/structural) ---
            # 16x16 grayscale thumbnail captures layout/structure
            thumb_h = img.resize((16, 16)).convert("L")
            h_feat = np.array(thumb_h).flatten().astype(float)
            h_feat /= (np.linalg.norm(h_feat) + 1e-10)
            hash_features.append(h_feat)

            valid_paths.append(fpath)

            dt = get_file_datetime(fpath)
            timestamps.append(dt.timestamp())
        except Exception as exc:
            log.warning("Skipping %s: %s", fpath, exc)

    if not embeddings:
        log.info("No valid embeddings produced.")
        return [], {}

    visual_matrix = np.vstack(embeddings)  # (N, 512)

    # ------------------------------------------------------------------
    # 3b  Temporal feature
    # ------------------------------------------------------------------
    from sklearn.preprocessing import StandardScaler

    ts_array = np.array(timestamps).reshape(-1, 1)
    ts_scaled = StandardScaler().fit_transform(ts_array)  # (N, 1)

    # ------------------------------------------------------------------
    # 3c  Color & Hash features
    # ------------------------------------------------------------------
    color_matrix = np.vstack(color_features)  # (N, 3)
    hash_matrix = np.vstack(hash_features)    # (N, 256)

    # ------------------------------------------------------------------
    # 3c  Filename similarity features
    # ------------------------------------------------------------------
    fn_features = None
    if filename_weight > 0:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD

        log.info("Computing filename vectors (weight=%.2f) …", filename_weight)
        stems = [p.stem for p in valid_paths]
        
        # Character n-grams catch sequential naming (IMG_001, IMG_002)
        vectorizer = TfidfVectorizer(analyzer='char', ngram_range=(2, 4))
        tfidf_matrix = vectorizer.fit_transform(stems)
        
        # Reduce dimensionality (LSA) to keep it manageable alongside CLIP
        # truncatedSVD requires n_components < n_features
        n_feats = tfidf_matrix.shape[1]
        n_comps = min(8, n_feats - 1) if n_feats > 1 else 0
        
        if n_comps > 0:
            svd = TruncatedSVD(n_components=n_comps)
            fn_features = svd.fit_transform(tfidf_matrix)
            # L2 normalize filename features so they are on same scale as CLIP
            fn_norms = np.linalg.norm(fn_features, axis=1, keepdims=True) + 1e-10
            fn_features = fn_features / fn_norms
        else:
            # Fallback if no n-grams or too few files
            fn_features = np.zeros((len(valid_paths), 1))

    # ------------------------------------------------------------------
    # 3d  Combine & cluster
    # ------------------------------------------------------------------
    features = [
        visual_weight * visual_matrix, 
        temporal_weight * ts_scaled,
        color_weight * color_matrix,
        near_duplicate_weight * hash_matrix
    ]
    if fn_features is not None:
        features.append(filename_weight * fn_features)
    
    combined = np.hstack(features)

    # Normalize HDBSCAN params: 0 or False should be None (auto)
    if not min_samples:
        min_samples = None

    import hdbscan

    if ai_clustering_mode == "folder":
        log.info("Aggregating folder centroids for clustering …")
        # Map folders to their image indices
        folder_to_indices = defaultdict(list)
        for idx, p in enumerate(valid_paths):
            folder_to_indices[str(p.parent.resolve())].append(idx)
        
        folder_paths = sorted(folder_to_indices.keys())
        folder_features = []
        for folder in folder_paths:
            indices = folder_to_indices[folder]
            # Mean centroid for this folder
            centroid = np.mean(combined[indices], axis=0)
            # Re-normalize visual part? Actually, combined is weighted, so just mean is fine for identity.
            folder_features.append(centroid)
        
        folder_matrix = np.vstack(folder_features)
        
        log.info("Running HDBSCAN on %d folders …", len(folder_matrix))
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=max(2, min_cluster_size // 2) if len(folder_matrix) > 2 else 1, # Folders are coarser
            min_samples=min_samples,
            cluster_selection_epsilon=cluster_selection_epsilon,
            cluster_selection_method=cluster_selection_method,
            metric="euclidean",
        )
        folder_labels = clusterer.fit_predict(folder_matrix)
        
        # Map folder labels back to individual image labels
        labels = np.full(len(valid_paths), -1)
        for f_idx, label in enumerate(folder_labels):
            folder = folder_paths[f_idx]
            for img_idx in folder_to_indices[folder]:
                labels[img_idx] = label
    else:
        log.info(
            "Running HDBSCAN (min_cluster_size=%d, min_samples=%s, eps=%.2f, method=%s) …",
            min_cluster_size,
            min_samples,
            cluster_selection_epsilon,
            cluster_selection_method,
            )
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            cluster_selection_epsilon=cluster_selection_epsilon,
            cluster_selection_method=cluster_selection_method,
            metric="euclidean",
        )
        labels = clusterer.fit_predict(combined)

    n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
    n_noise = int(np.sum(labels == -1))
    log.info("Found %d cluster(s), %d outlier(s).", n_clusters, n_noise)

    # ------------------------------------------------------------------
    # 3d-post  Name-Based Overrides
    # ------------------------------------------------------------------
    labels = apply_name_grouping_overrides(valid_paths, labels, group_name_matches, group_name_prefix)

    # ------------------------------------------------------------------
    # 3d  Geometric Diagnostics
    # ------------------------------------------------------------------
    cluster_stats = {}
    unique_labels = set(labels)
    if -1 in unique_labels:
        unique_labels.remove(-1)

    for label in unique_labels:
        indices = np.where(labels == label)[0]
        cluster_points = combined[indices]
        # Fast distance matrix calculation for small clusters
        diffs = cluster_points[:, np.newaxis, :] - cluster_points[np.newaxis, :, :]
        dists = np.linalg.norm(diffs, axis=2)
        avg_dists = dists.mean(axis=1)
        m_idx_in_c = np.argmin(avg_dists)
        cluster_stats[label] = {
            "medoid_point": combined[indices[m_idx_in_c]],
            "dispersion": avg_dists[m_idx_in_c],
        }

    # ------------------------------------------------------------------
    # 3e  Move files
    # ------------------------------------------------------------------
    org_root = root / ORGANIZED_DIR_NAME
    entries: list[dict] = []
    cluster_map: dict[int, list[Path]] = defaultdict(list)

    probs = clusterer.probabilities_
    for zip_idx, (fpath, label, prob) in enumerate(zip(valid_paths, labels, probs)):
        h = md5_hash(fpath)
        if label == -1:
            dest_dir = org_root / "unsorted"
            cluster_id = "noise"
            # Find closest cluster medoid for diagnostics
            best_dist = float("inf")
            closest_label = None
            current_point = combined[zip_idx] # We need the index
            for cl_label, stats in cluster_stats.items():
                d = np.linalg.norm(combined[zip_idx] - stats["medoid_point"])
                if d < best_dist:
                    best_dist = d
                    closest_label = cl_label
            
            if closest_label is not None:
                reason = f"Outlier: Strength below threshold (Closest: Group_{closest_label:03d} at d={best_dist:.3f})"
            else:
                reason = "Outlier: Strength below threshold (No clusters found)"
        else:
            dest_dir = org_root / "groups" / f"Group_{label:03d}"
            cluster_id = f"group_{label:03d}"
            disp = cluster_stats[label]["dispersion"]
            reason = f"Cluster: Group_{label:03d} assignment (Dispersion: {disp:.3f})"

        dest = dest_dir / fpath.name
        # Collision-safe
        if dest.exists() and dest != fpath:
            stem = dest.stem
            suffix = dest.suffix
            dest = dest_dir / f"{stem}_{short_path_hash(fpath)}{suffix}"

        safe_action(fpath, dest, dry_run, action=action)
        actual_dest = dest if not dry_run else fpath  # file hasn't moved in dry-run
        cluster_map[label].append(actual_dest if not dry_run else dest)

        # ------------------------------------------------------------------
        # Near-Miss Calculation (Distances to top 3 medoids)
        # ------------------------------------------------------------------
        near_misses = {}
        if cluster_stats:
            dists_to_medoids = []
            for cl_label, stats in cluster_stats.items():
                d = np.linalg.norm(combined[zip_idx] - stats["medoid_point"])
                d_val = float(d)
                dists_to_medoids.append((f"group_{cl_label:03d}", d_val))
            
            # Sort by distance and take top 3
            dists_to_medoids.sort(key=lambda x: x[1])
            near_misses = {k: round(v, 4) for k, v in dists_to_medoids[:3]}

        entries.append(
            {
                "Original_Path": str(fpath),
                "New_Path": str(dest) if not dry_run else f"[DRY RUN] {dest}",
                "New_Filename": dest.name,
                "Hash": h,
                "Cluster_ID": cluster_id,
                "Media_Type": "image",
                "Reason": reason,
                "Confidence": f"{prob:.4f}",
                "Near_Misses": json.dumps(near_misses),
            }
        )

    log.info("Phase 3 complete.")
    return entries, cluster_map


# ---------------------------------------------------------------------------
# Phase 4 — Contextual Naming & Attribution
# ---------------------------------------------------------------------------


def phase4_contextual_naming(
    root: Path,
    phase3_entries: list[dict],
    dry_run: bool,
) -> list[dict]:
    """
    Rename files inside /groups/*/ and /unsorted/ based on their *original*
    parent folder name + sequential numbering.  Returns updated log entries.
    """
    log.info("=" * 60)
    log.info("PHASE 4 — Contextual Naming & Attribution")
    log.info("=" * 60)

    # Group entries by their destination directory
    dir_entries: dict[str, list[dict]] = defaultdict(list)
    for entry in phase3_entries:
        new_path = entry["New_Path"].replace("[DRY RUN] ", "")
        dest_dir = str(Path(new_path).parent)
        dir_entries[dest_dir].append(entry)

    updated_entries: list[dict] = []

    for dest_dir_str, entries in dir_entries.items():
        dest_dir = Path(dest_dir_str)

        # Sort by EXIF / file date using original path
        def _sort_key(e: dict) -> float:
            orig = Path(e["Original_Path"])
            if orig.exists():
                return get_file_datetime(orig).timestamp()
            return 0.0

        entries.sort(key=_sort_key)

        # Detect base-name conflicts (same folder name from different source paths)
        base_name_sources: dict[str, set[str]] = defaultdict(set)
        for e in entries:
            orig = Path(e["Original_Path"])
            if orig.parent.resolve() == root.resolve():
                base = "root_files"
            else:
                base = orig.parent.name or "root"
            # Sanitize for filesystem safety
            base = re.sub(r'[<>:"/\\|?*]', "_", base)
            base_name_sources[base].add(str(orig.parent))

        needs_disambig: set[str] = {
            b for b, srcs in base_name_sources.items() if len(srcs) > 1 and b != "root_files"
        }

        # Assign sequential names within this destination folder
        counter: int = 1
        for entry in entries:
            orig = Path(entry["Original_Path"])
            
            if orig.parent.resolve() == root.resolve():
                # Keep original filename for root-level files
                new_name = orig.name
                # Simple collision check
                temp_path = dest_dir / new_name
                if temp_path.exists() and temp_path != Path(entry["New_Path"].replace("[DRY RUN] ", "")):
                    stem = temp_path.stem
                    suffix = temp_path.suffix
                    new_name = f"{stem}_{short_path_hash(orig)}{suffix}"
            else:
                base = orig.parent.name or "root"
                base = re.sub(r'[<>:"/\\|?*]', "_", base)

                if base in needs_disambig:
                    disambig = short_path_hash(orig.parent)
                    base = f"{base}_{disambig}"

                ext = Path(entry["New_Filename"]).suffix
                new_name = f"{base}_{counter:03d}{ext}"
                counter += 1

            current_path = Path(entry["New_Path"].replace("[DRY RUN] ", ""))
            final_path = dest_dir / new_name

            if not dry_run:
                if current_path.exists() and current_path != final_path:
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(current_path), str(final_path))
                    log.info("Renamed  %s  ->  %s", current_path.name, new_name)
            else:
                log.info("[DRY RUN] Rename  %s  ->  %s", current_path.name, new_name)

            entry["New_Path"] = (
                str(final_path) if not dry_run else f"[DRY RUN] {final_path}"
            )
            entry["New_Filename"] = new_name
            updated_entries.append(entry)
            counter += 1

    log.info("Phase 4 complete: %d files renamed.", len(updated_entries))
    return updated_entries


# ---------------------------------------------------------------------------
# Phase 5 — CSV Logging
# ---------------------------------------------------------------------------


def phase5_write_log(root: Path, all_entries: list[dict], dry_run: bool, settings: dict = None) -> Path:
    """Write migration log to the application 'logs' directory."""
    log.info("=" * 60)
    log.info("PHASE 5 — Writing Audit Log")
    log.info("=" * 60)

    # Centralized logs folder
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    # Dynamic filename: [FolderName]_MM.DD.YYYY_HH.mm.csv
    timestamp = datetime.now().strftime("%m.%d.%Y_%H.%M")
    root_name = root.name if root.name else "root"
    filename = f"{root_name}_{timestamp}.csv"
    csv_path = logs_dir / filename

    # Mix in settings to every row for the visualizer to detect
    settings_json = json.dumps(settings) if settings else "{}"
    for entry in all_entries:
        entry["Settings"] = settings_json

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
        writer.writeheader()
        writer.writerows(all_entries)

    log.info("Audit log written to %s  (%d entries).", csv_path, len(all_entries))
    return csv_path


# ---------------------------------------------------------------------------
# File collection helper  (used by individual phase runs)
# ---------------------------------------------------------------------------


def collect_all_files(root: Path) -> list[Path]:
    """Return all non-hidden files under *root*, excluding output directories."""
    org_root = root / ORGANIZED_DIR_NAME
    results: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if is_hidden(path):
            continue
        # Skip files already inside the Organized directory
        try:
            path.relative_to(org_root)
            continue
        except ValueError:
            pass
        results.append(path)
    return results


# ---------------------------------------------------------------------------
# Interactive Menu
# ---------------------------------------------------------------------------

BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║              M E D I A   O R G A N I Z E R              ║
║         Content-Aware Sorting & Grouping Tool           ║
╚══════════════════════════════════════════════════════════╝
"""

MENU = """
┌──────────────────────────────────────────────────────────┐
│  PHASES                                                  │
│                                                          │
│   [1]  Phase 1 — Video Sorting  (orientation detect)     │
│   [2]  Phase 2 — Image De-duplication  (MD5 hashing)     │
│   [F]  Phase 2b — Subfolder Flattening (Override AI)    │
│   [3]  Phase 3 — Spatiotemporal Clustering  (CLIP)       │
│   [4]  Phase 4 — Contextual Naming & Attribution         │
│   [5]  Phase 5 — Write Audit Log  (CSV)                  │
│                                                          │
│   [A]  ▶  Run ALL Phases in sequence                      │
│                                                          │
│   [Q]  Quit                                              │
└──────────────────────────────────────────────────────────┘
"""


def _prompt(text: str, default: str = "") -> str:
    """Prompt the user and return stripped input (or *default*)."""
    val = input(text).strip()
    return val if val else default


def _confirm_mode() -> tuple[bool, str]:
    """Ask for dry-run vs real, and move vs copy. Returns (is_dry_run, action)."""
    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │  EXECUTION MODE                         │")
    print("  │                                         │")
    print("  │   [1]  🔍 Dry Run  (preview only)       │")
    print("  │   [2]  🚀 Real Run (copy/move)          │")
    print("  │                                         │")
    print("  └─────────────────────────────────────────┘")
    while True:
        choice = _prompt("  Select mode [1/2]: ")
        if choice == "1":
            print("\n  ➜  Mode: DRY RUN — no files will be changed.\n")
            return True, "copy"
        elif choice == "2":
            print("\n  ┌─────────────────────────────────────────┐")
            print("  │  ACTION                                 │")
            print("  │                                         │")
            print("  │   [1]  📋 Copy (default)                │")
            print("  │   [2]  📦 Move                          │")
            print("  │                                         │")
            print("  └─────────────────────────────────────────┘")
            act_choice = _prompt("  Select action [1/2]: ", "1")
            action = "move" if act_choice == "2" else "copy"

            label = "MOVE" if action == "move" else "COPY"
            confirm = _prompt(f"  ⚠  This will {label} files. Continue? [y/N]: ")
            if confirm.lower() in ("y", "yes"):
                print(f"\n  ➜  Mode: REAL RUN ({label})\n")
                return False, action
            else:
                print("  Cancelled — returning to mode selection.\n")
        else:
            print("  Invalid choice.  Enter 1 or 2.")


def _get_cluster_params() -> dict:
    """Prompt for HDBSCAN parameters with sensible defaults."""
    print("\n  Clustering Strategy:")
    print("   [1]  📄 Individual Mode - fine-grained file grouping (default)")
    print("   [2]  📂 Folder Mode     - groups folders based on collective content")
    mode_choice = _prompt("  Select mode [1/2] (default 1): ", "1")
    mode = "folder" if mode_choice == "2" else "individual"

    raw_min = _prompt("  HDBSCAN min_cluster_size [3]: ", "3")
    try:
        min_size = int(raw_min)
    except ValueError:
        min_size = 3

    raw_samples = _prompt("  HDBSCAN min_samples (Enter for default) []: ", "")
    try:
        min_samples = int(raw_samples) if raw_samples else None
    except ValueError:
        min_samples = None

    raw_eps = _prompt("  HDBSCAN cluster_selection_epsilon [0.0]: ", "0.0")
    try:
        eps = float(raw_eps)
    except ValueError:
        eps = 0.0

    print("\n  Granularity Selection:")
    print("   [1]  🎯 Small & Accurate (Leaf) - finds more, tighter groups")
    print("   [2]  🌊 Large & Broad (EOM) - default grouping style")
    method_choice = _prompt("  Select method [1/2] (default 1): ", "1")
    method = "leaf" if method_choice == "1" else "eom"

    raw_tw = _prompt("  Temporal weight [0.3]: ", "0.3")
    try:
        tw = float(raw_tw)
    except ValueError:
        tw = 0.3

    raw_fw = _prompt("  Filename Similarity Weight [0.0]: ", "0.0")
    try:
        fw = float(raw_fw)
    except ValueError:
        fw = 0.0

    raw_cw = _prompt("  Color Similarity Weight [0.0]: ", "0.0")
    try:
        cw = float(raw_cw)
    except ValueError:
        cw = 0.0

    raw_nw = _prompt("  Visual Hash Weight [0.0]: ", "0.0")
    try:
        nw = float(raw_nw)
    except ValueError:
        nw = 0.0

    return {
        "min_cluster_size": min_size,
        "min_samples": min_samples,
        "epsilon": eps,
        "method": method,
        "temporal_weight": tw,
        "filename_weight": fw,
        "color_weight": cw,
        "near_duplicate_weight": ndw,
        "ai_clustering_mode": mode
    }


def interactive_main() -> None:
    """Interactive menu-driven entry point."""
    print(BANNER)

    # --- Root directory ---
    while True:
        raw = _prompt("  Enter root media directory: ")
        root = Path(raw).resolve()
        if root.is_dir():
            break
        print(f"  ✗  Not a valid directory: {root}\n")

    print(f"  ✓  Root: {root}")

    # --- Execution mode ---
    dry_run, action = _confirm_mode()

    # --- Shared state across phases ---
    all_entries: list[dict] = []
    remaining_files: list[Path] | None = None
    unique_images: list[Path] | None = None
    cluster_entries: list[dict] | None = None
    is_flattening = False

    while True:
        print(MENU)
        choice = _prompt("  Select an option: ").upper()

        # ---- Phase 1 ----
        if choice == "1":
            video_entries, remaining_files = phase1_video_sorting(root, dry_run, action=action)
            all_entries.extend(video_entries)

        # ---- Phase 2 ----
        elif choice == "2":
            if remaining_files is None:
                # Collect files fresh if Phase 1 was not run this session
                remaining_files = [
                    f for f in collect_all_files(root)
                    if f.suffix.lower() not in VIDEO_EXTENSIONS
                ]
            dup_entries, unique_images = phase2_deduplication(
                root, remaining_files, dry_run, action=action
            )
            all_entries.extend(dup_entries)

        # ---- Phase 3 ----
        elif choice == "3":
            if is_flattening:
                print("  ⚠  Subfolder Flattening has been run. Skipping Phase 3 (AI).\n")
                continue
            if unique_images is None:
                # Collect images fresh if prior phases were not run
                unique_images = [
                    f for f in collect_all_files(root)
                    if f.suffix.lower() in IMAGE_EXTENSIONS
                ]
            params = _get_cluster_params()
            clust_entries, _ = phase3_clustering(
                root, unique_images, dry_run,
                action=action,
                min_cluster_size=params["min_cluster_size"],
                min_samples=params["min_samples"],
                cluster_selection_epsilon=params["epsilon"],
                cluster_selection_method=params["method"],
                temporal_weight=params["temporal_weight"],
                filename_weight=params["filename_weight"],
                color_weight=params["color_weight"],
                near_duplicate_weight=params["near_duplicate_weight"],
                visual_weight=1.0,  # Default in interactive menu mode
                ai_clustering_mode=params.get("ai_clustering_mode", "individual"),
            )
            cluster_entries = clust_entries
            all_entries.extend(clust_entries)

        # ---- Phase 2b (Folder Flattening) ----
        elif choice == "F":
            if unique_images is None:
                if remaining_files is None:
                    remaining_files = [f for f in collect_all_files(root) if f.suffix.lower() not in VIDEO_EXTENSIONS]
                unique_images = remaining_files
            
            rename_confirm = _prompt("  Rename with parent folder name? [Y/n]: ", "y").lower() == "y"
            flat_entries, unique_images = phase2b_folder_flattening(
                root, unique_images, dry_run, action=action, rename=rename_confirm
            )
            all_entries.extend(flat_entries)
            if flat_entries:
                is_flattening = True

        # ---- Phase 4 ----
        elif choice == "4":
            if cluster_entries is None or len(cluster_entries) == 0:
                print("  ⚠  Phase 3 has not been run yet (no cluster entries).")
                print("     Run Phase 3 first, or run All Phases.\n")
                continue
            cluster_entries = phase4_contextual_naming(
                root, cluster_entries, dry_run
            )
            # Update the entries that were already added
            # (replace the Phase 3 entries with renamed versions)
            # Remove old phase-3 originals and add renamed ones
            orig_paths = {e["Original_Path"] for e in cluster_entries}
            all_entries = [
                e for e in all_entries if e["Original_Path"] not in orig_paths
            ]
            all_entries.extend(cluster_entries)

        # ---- Phase 5 ----
        elif choice == "5":
            phase5_write_log(root, all_entries, dry_run)

        # ---- Run ALL ----
        elif choice == "A":
            print("  ▶  Running ALL phases in sequence …\n")
            
            enable_flat = _prompt("  Enable Subfolder Flattening? [y/N]: ", "n").lower() == "y"
            params = {}
            if not enable_flat:
                params = _get_cluster_params()

            # Phase 1
            video_entries, remaining_files = phase1_video_sorting(root, dry_run, action=action)
            all_entries.extend(video_entries)

            # Phase 2
            dup_entries, unique_images = phase2_deduplication(
                root, remaining_files, dry_run, action=action
            )
            all_entries.extend(dup_entries)

            # Phase 2b
            is_flat = False
            if enable_flat:
                rename_flat = _prompt("  Rename with parent folder name? [Y/n]: ", "y").lower() == "y"
                flat_entries, unique_images = phase2b_folder_flattening(
                    root, unique_images, dry_run, action=action, rename=rename_flat
                )
                all_entries.extend(flat_entries)
                if flat_entries:
                    is_flat = True
                    is_flattening = True

            # Phase 3
            clust_entries = []
            if is_flat:
                log.info("[SKIP] Phase 3: AI Clustering skipped due to Flattening.")
            elif params.get("enable_ai_clustering", True):
                clust_entries, _ = phase3_clustering(
                    root, unique_images, dry_run,
                    action=action,
                    min_cluster_size=params.get("min_cluster_size", 3),
                    min_samples=params.get("min_samples"),
                    cluster_selection_epsilon=params.get("epsilon", 0.0),
                    cluster_selection_method=params.get("method", "leaf"),
                    temporal_weight=params.get("temporal_weight", 0.3),
                    filename_weight=params.get("filename_weight", 0.0),
                    color_weight=params.get("color_weight", 0.0),
                    near_duplicate_weight=params.get("near_duplicate_weight", 0.0),
                    ai_clustering_mode=params.get("ai_clustering_mode", "individual"),
                )
            elif params.get("group_name_matches", False) or params.get("group_name_prefix", False):
                log.info("[INFO] AI Clustering disabled, but Name Grouping is enabled. Running standalone.")
                clust_entries, _ = phase3_standalone_grouping(
                    root, unique_images, dry_run,
                    action=action,
                    group_name_matches=params.get("group_name_matches", False),
                    group_name_prefix=params.get("group_name_prefix", False),
                )
            else:
                log.info("[SKIP] Phase 3: AI Clustering and Name Grouping disabled.")
            
            cluster_entries = clust_entries

            # Phase 4
            if not is_flat and cluster_entries:
                cluster_entries = phase4_contextual_naming(
                    root, cluster_entries, dry_run
                )
                all_entries.extend(cluster_entries)

            # Phase 5
            run_settings = {
                "action": action,
                "min_cluster_size": params["min_cluster_size"],
                "min_samples": params["min_samples"],
                "epsilon": params["epsilon"],
                "method": params["method"],
                "temporal_weight": params["temporal_weight"],
                "filename_weight": params["filename_weight"],
                "dry_run": dry_run
            }
            log_file_path = phase5_write_log(root, all_entries, dry_run, settings=run_settings)

            log.info("=" * 60)
            log.info("Done. %d total file operations recorded.", len(all_entries))
            log.info("=" * 60)

            # --- Post-run cleanup (only if Real Run + Copy) ---
            if not dry_run and action == "copy" and all_entries:
                print("\n  ┌─────────────────────────────────────────┐")
                print("  │  POST-RUN VERIFICATION                  │")
                print("  │                                         │")
                print("  │   [1]  ✅ Keep organized, delete orign. │")
                print("  │   [2]  ↩️ Undo: Delete organized files  │")
                print("  │   [3]  💾 Keep both (default)           │")
                print("  │                                         │")
                print("  └─────────────────────────────────────────┘")
                cleanup = _prompt("  Select an option [1/2/3]: ", "3")

                if cleanup == "1":
                    confirm = _prompt("  ⚠  This will DELETE the original files. Continue? [y/N]: ")
                    if confirm.lower() in ("y", "yes"):
                        log.info("Cleaning up original files...")
                        for entry in all_entries:
                            orig = Path(entry["Original_Path"])
                            if orig.exists() and orig.is_file():
                                try:
                                    orig.unlink()
                                    log.info("  Deleted original: %s", orig.name)
                                except Exception as exc:
                                    log.warning("  Failed to delete %s: %s", orig, exc)
                        log.info("Original files cleaned up.")

                elif cleanup == "2":
                    confirm = _prompt("  ⚠  This will DELETE the entire 'Organized' folder. Continue? [y/N]: ")
                    if confirm.lower() in ("y", "yes"):
                        log.info("Undoing: Removing 'Organized' directory...")
                        org_root = root / ORGANIZED_DIR_NAME
                        if org_root.exists() and org_root.is_dir():
                            try:
                                shutil.rmtree(org_root)
                                log.info("  Removed directory: %s", ORGANIZED_DIR_NAME)
                            except Exception as exc:
                                log.warning("  Failed to remove %s: %s", ORGANIZED_DIR_NAME, exc)
                        
                        # Also delete the log
                        if log_file_path.exists():
                            log_file_path.unlink()
                        log.info("Organized content and log removed.")

            break  # Exit after full run

        # ---- Quit ----
        elif choice == "Q":
            print("\n  Goodbye!\n")
            break

        else:
            print("  Invalid selection. Try again.\n")


# ---------------------------------------------------------------------------
# CLI entry point (supports both interactive and direct modes)
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Content-aware media organizer with clustering & dedup.",
    )
    # Structural args (not part of the tuning schema)
    parser.add_argument("--root", type=Path, default=None,
                        help="Root directory containing disorganized media.")
    parser.add_argument("--interactive", action="store_true", default=False,
                        help="Force interactive menu mode.")

    # Auto-generate all tuning args from PARAM_SCHEMA
    for p in PARAM_SCHEMA:
        kwargs = {"help": p.get("tooltip", ""), "default": p["default"]}
        if p["type"] == "bool":
            kwargs["action"] = "store_true"
            kwargs["default"] = False  # bools are flags, absent = False
        elif p["type"] == "choice":
            kwargs["choices"] = p["choices"]
        elif p["type"] == "int":
            kwargs["type"] = int
        elif p["type"] == "float":
            kwargs["type"] = float
        parser.add_argument(p["cli"], **kwargs)

    args = parser.parse_args()

    # If no --root supplied, or --interactive flag is set, launch the menu
    if args.root is None or args.interactive:
        interactive_main()
        return

    # --- Direct (non-interactive) mode ---
    print("[STARTUP] Media Organizer initializing...", flush=True)
    root: Path = args.root.resolve()
    if not root.is_dir():
        log.error("Root path does not exist or is not a directory: %s", root)
        sys.exit(1)

    # Build a clean dict from schema keys -> parsed values
    def _arg(key):
        """Get argparse value by schema key (CLI flag -> dest)."""
        # 1. Find entry in schema
        entry = _PARAM_MAP.get(key)
        if not entry:
            return None
        
        # 2. Map CLI flag to argparse destination name
        # argparse converts --cluster-min-size -> cluster_min_size
        dest = entry["cli"].lstrip("-").replace("-", "_")
        return getattr(args, dest, entry.get("default"))

    dry_label = " [DRY RUN]" if _arg("dry_run") else ""
    log.info("Media Organizer starting%s — root: %s", dry_label, root)

    all_entries: list[dict] = []
    cluster_entries: list[dict] = []
    cluster_map: dict[int, list[Path]] = {}

    # Phase 1
    video_entries = []
    remaining_files = []
    if _arg("enable_video_sorting"):
        video_entries, remaining_files = phase1_video_sorting(
            root, _arg("dry_run"), action=_arg("action"), enable_deduplication=_arg("enable_video_deduplication")
        )
        all_entries.extend(video_entries)
    else:
        log.info("[SKIP] Phase 1: Video Sorting disabled.")
        # Need to collect all files if Phase 1 is skipped
        remaining_files = list(root.rglob("*"))
        remaining_files = [f for f in remaining_files if f.is_file() and not is_hidden(f)]

    # Phase 2
    unique_images = remaining_files
    if _arg("enable_deduplication"):
        dup_entries, unique_images = phase2_deduplication(
            root, remaining_files, _arg("dry_run"), action=_arg("action")
        )
        all_entries.extend(dup_entries)
    else:
        log.info("[SKIP] Phase 2: De-duplication disabled.")

    # Phase 2b: Folder Flattening (New)
    is_flattening = False
    if _arg("enable_folder_flattening"):
        flatten_entries, unique_images = phase2b_folder_flattening(
            root,
            unique_images,
            _arg("dry_run"),
            action=_arg("action"),
            rename=_arg("flatten_rename"),
        )
        all_entries.extend(flatten_entries)
        if flatten_entries:
            is_flattening = True
            log.info("[INFO] Subfolder Flattening active. AI Clustering and Name Grouping will be skipped.")

    # Phase 3
    if is_flattening:
        log.info("[SKIP] Phase 3 & 3b: Skipped due to Subfolder Flattening.")
        cluster_entries = []
    elif _arg("enable_ai_clustering"):
        cluster_entries, cluster_map = phase3_clustering(
            root,
            unique_images,
            _arg("dry_run"),
            action=_arg("action"),
            min_cluster_size=_arg("min_cluster_size"),
            min_samples=_arg("min_samples"),
            cluster_selection_epsilon=_arg("epsilon"),
            cluster_selection_method=_arg("method"),
            temporal_weight=_arg("temporal_weight"),
            filename_weight=_arg("filename_weight"),
            color_weight=_arg("color_weight"),
            near_duplicate_weight=_arg("near_duplicate_weight"),
            visual_weight=_arg("visual_weight"),
            group_name_matches=_arg("group_name_matches"),
            group_name_prefix=_arg("group_name_prefix"),
            ai_clustering_mode=_arg("ai_clustering_mode"),
        )
    elif _arg("group_name_matches") or _arg("group_name_prefix"):
        log.info("[INFO] AI Clustering disabled, but Name Grouping is enabled. Running standalone.")
        cluster_entries, cluster_map = phase3_standalone_grouping(
            root,
            unique_images,
            _arg("dry_run"),
            action=_arg("action"),
            group_name_matches=_arg("group_name_matches"),
            group_name_prefix=_arg("group_name_prefix"),
        )
    else:
        log.info("[SKIP] Phase 3 & 3b: AI Clustering and Name Grouping disabled.")

    # Phase 4 (Contextual Naming — applies to both AI or name-based clusters)
    if not is_flattening and cluster_entries:
        cluster_entries = phase4_contextual_naming(root, cluster_entries, _arg("dry_run"))
        all_entries.extend(cluster_entries)

    # Phase 5 — auto-generate run_settings from schema
    run_settings = {p["key"]: _arg(p["key"]) for p in PARAM_SCHEMA}
    phase5_write_log(root, all_entries, _arg("dry_run"), settings=run_settings)

    log.info("=" * 60)
    log.info("Done. %d total file operations recorded.", len(all_entries))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
