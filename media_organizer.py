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
from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
from tqdm import tqdm

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
    "Match_Confidence",
    "Match_Source",
    "Settings",
    "Near_Misses",
    "Review_Needed",
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
# Feature Cache — persists visual embeddings to skip expensive AI re-runs
# ---------------------------------------------------------------------------

class FeatureCache:
    """
    JSON-based cache for expensive file features (CLIP, color, etc).
    Stored as '.organizer_cache.json' in the root directory.
    """
    CACHE_VERSION = "1.3"

    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".organizer_cache.json"
        self.data: dict[str, dict] = {}
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if loaded.get("version") == self.CACHE_VERSION:
                        self.data = loaded.get("features", {})
                        log.info("Feature cache loaded: %d entries.", len(self.data))
                    else:
                        log.info("Cache version mismatch or missing. Starting fresh.")
            except Exception as e:
                log.warning("Could not load feature cache: %s", e)

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": self.CACHE_VERSION,
                    "updated_at": datetime.now().isoformat(),
                    "features": self.data
                }, f, indent=2)
        except Exception as e:
            log.warning("Could not save feature cache: %s", e)

    def get(self, fpath: Path, md5: str) -> Optional[dict]:
        """Lookup features by MD5. Path is used for secondary validation."""
        entry = self.data.get(md5)
        if entry:
            # Check if dimensions match what we expect? 
            # For now, just trust the MD5.
            return entry
        return None

    def set(self, fpath: Path, md5: str, features: dict):
        self.data[md5] = features

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
        "is_plugin": True, "plugin_icon": "copy",
    },
    {
        "key": "enable_perceptual_video_dedup", "cli": "--enable-perceptual-video-dedup", "type": "bool",
        "default": False,
        "label": "Perceptual Video Deduplication",
        "tooltip": "Advanced 'Tri-Path' filter to identify visually similar videos at different bitrates/resolutions.",
        "group": "core",
        "is_plugin": True, "plugin_icon": "eye",
    },
    {
        "key": "enable_deep_video_scan", "cli": "--enable-deep-video-scan", "type": "bool",
        "default": False,
        "label": "Deep Video Scan (Temporal)",
        "tooltip": "Level 3 scan: Uses videohash to generate a 64-bit temporal signature. Slower but highly accurate.",
        "group": "core",
    },
    {
        "key": "video_match_threshold", "cli": "--video-match-threshold", "type": "int",
        "default": 8, "min": 0, "max": 64,
        "label": "Video Similarity Threshold",
        "tooltip": "Hamming distance limit (Default 8). Lower = stricter; Higher = looser matching.",
        "group": "core",
    },
    {
        "key": "video_max_workers", "cli": "--video-max-workers", "type": "int",
        "default": 4, "min": 1, "max": 32,
        "label": "Max Parallel Video Workers",
        "tooltip": "Limits concurrent video decodes. Lower values (e.g., 2-4) prevent memory exhaustion on large libraries.",
        "group": "core",
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
        "key": "dedup_priority", "cli": "--dedup-priority", "type": "choice",
        "choices": ["alphabetical", "path_depth"], "default": "alphabetical",
        "label": "Deduplication Strategy",
        "tooltip": "Alphabetical: First file A-Z is original. Path Depth: Deeper folders (context-rich) win over root files.",
        "group": "core",
    },
    {
        "key": "enable_color_features", "cli": "--enable-color-features", "type": "bool",
        "default": True,
        "label": "Spatial Color Signature",
        "tooltip": "Extracts a grid of color samples horizontally and vertically across the image. Excellent for grouping images by 'vibe', lighting, or overall composition.",
        "group": "core",
        "is_plugin": True, "plugin_icon": "palette",
    },
    {
        "key": "enable_folder_flattening", "cli": "--enable-folder-flattening", "type": "bool",
        "default": False,
        "label": "Subfolder Flattening",
        "tooltip": "Bypasses AI clustering to simply move all files out of subfolders and into a single directory, optionally renaming them by their original parent folder.",
        "group": "core",
        "is_plugin": True, "plugin_icon": "layers",
    },
    {
        "key": "use_organized_subfolder", "cli": "--use-organized-subfolder", "type": "bool",
        "default": True,
        "label": "Use '/Organized/' Subfolder",
        "tooltip": "Enabled (Default): Creates a clean '/Organized/' folder. Disabled: Organizes directly into your source root (Base Folder).",
        "group": "deployment",
    },
    {
        "key": "deployment_structure", "cli": "--deployment-structure", "type": "choice",
        "choices": ["structured", "flat"], "default": "structured",
        "label": "Deployment Structure",
        "tooltip": "Structured: Organizes into sub-directories (/groups, /videos). Flat Root: All unique files land directly in the target root.",
        "group": "deployment",
    },
    {
        "key": "flatten_rename", "cli": "--flatten-rename", "type": "bool",
        "default": True,
        "label": "Contextual Rename",
        "tooltip": "When flattening, prepends the parent folder name and adds a sequence number (e.g., 'Folder - Name - 001.jpg') based on creation date.",
        "group": "core",
    },
    {
        "key": "rename_style", "cli": "--rename-style", "type": "choice",
        "default": "folder_name",
        "choices": ["folder_name", "folder_only"],
        "label": "Rename Style",
        "tooltip": "'folder_name' = Folder - OriginalName - 001.jpg. 'folder_only' = Folder - 001.jpg.",
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
    {
        "key": "force_rescan", "cli": "--force-rescan", "type": "bool",
        "default": False,
        "label": "Force Full folder Scan",
        "tooltip": "Bypasses the feature cache and re-analyzes every file with AI. Slow, but useful for total re-evaluation.",
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
        "default": 0.3, "min": 0.0, "max": 5.0, "step": 0.05,
        "label": "Color Influence Weight",
        "tooltip": "How much 'color' matters vs other AI factors. Increase this to group by 'vibe' over 'subject'.",
        "group": "weights",
        "is_plugin": True, "plugin_icon": "palette",
    },
    {
        "key": "grid_detail", "cli": "--grid-detail", "type": "int",
        "default": 16, "min": 4, "max": 64,
        "label": "Color Grid Detail",
        "tooltip": "The N×N grid size. 16 is the 'sweet spot' for patterns; 8 is better for broad lighting; 32+ for fine detail.",
        "group": "clustering",
    },
    {
        "key": "use_lab_space", "cli": "--use-lab-space", "type": "bool",
        "default": True,
        "label": "Use CIELAB Color Space",
        "tooltip": "Converts Colors to CIELAB space to ensure mathematical distance matches human perceived difference.",
        "group": "clustering",
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
        "key": "name_grouping_min_size", "cli": "--name-grouping-min-size", "type": "int",
        "default": 2, "min": 1, "max": 12,
        "label": "Min Group Items",
        "tooltip": "The minimum number of files needed to form a group based on naming patterns.",
        "group": "name_grouping",
    },
    {
        "key": "name_grouping_sensitivity", "cli": "--name-grouping-sensitivity", "type": "float",
        "default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05,
        "label": "Name Pattern Sensitivity",
        "tooltip": "How loosely we consider two filenames a 'match'. Higher values require more significant words to overlap.",
        "group": "name_grouping",
    },
    {
        "key": "visual_weight", "cli": "--visual-weight", "type": "float",
        "default": 1.0, "min": 0.0, "max": 5.0, "step": 0.05,
        "label": "Visual Similarity (AI)",
        "tooltip": "The CLIP visual embedding weight. Dominates the grouping process by using AI to understand conceptual similarity.",
        "group": "weights",
        "is_plugin": True, "plugin_icon": "eye",
    },
    {
        "key": "similarity_sensitivity", "cli": "--similarity-sensitivity", "type": "float",
        "default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01,
        "label": "Similarity Sensitivity",
        "tooltip": "VisiPics-style sensitivity slider. 1.0 = Only identical images. 0.0 = Very broad clusters. Maps to Epsilon: (1.0 - Sensitivity).",
        "group": "clustering",
    },
]

# Build a quick lookup dict: key -> schema entry
_PARAM_MAP = {p["key"]: p for p in PARAM_SCHEMA}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


class ColorVibeSorter:
    """
    Expert-level color feature extractor for visual similarity.
    Provides a standardized CIELAB flattened grid with Euclidean-friendly normalization.
    """
    def __init__(self, grid_size: int = 16, use_lab: bool = True):
        self.grid_size = grid_size
        self.use_lab = use_lab

    def get_features(self, image_path: Path) -> np.ndarray:
        """
        Main entry point for feature extraction. 
        Returns a flattened vector normalized such that Max Euclidean Distance is 1.0.
        """
        from PIL import Image
        try:
            with Image.open(image_path) as img:
                return self.process_image(img)
        except Exception:
            # Fallback for broken images
            dim = 3 * (self.grid_size ** 2)
            return np.zeros(dim)

    def process_image(self, img_pil: Any) -> np.ndarray:
        """Internal image processing logic."""
        from PIL import Image
        
        # 1. Handle Transparency (Flatten to White)
        if img_pil.mode in ("RGBA", "LA") or (img_pil.mode == "P" and "transparency" in img_pil.info):
            alpha_img = img_pil.convert("RGBA")
            background = Image.new("RGBA", alpha_img.size, (255, 255, 255, 255))
            composite = Image.alpha_composite(background, alpha_img)
            img_pil = composite.convert("RGB")
        else:
            img_pil = img_pil.convert("RGB")
        
        # 2. Downsample to Grid (Area averaging)
        grid = img_pil.resize((self.grid_size, self.grid_size), resample=Image.Resampling.BOX)
        
        # 3. Convert to Array [0, 1]
        arr = np.array(grid).astype(np.float32) / 255.0
        
        # 4. Color Space Conversion (CIELAB)
        if self.use_lab:
            try:
                from skimage.color import rgb2lab
                # rgb2lab returns L [0, 100], a,b [-128, 127]
                lab = rgb2lab(arr)
                
                # Normalize individual elements to [0, 1]
                lab[..., 0] /= 100.0
                lab[..., 1:] = (lab[..., 1:] + 128.0) / 255.0
                arr = lab
            except ImportError:
                pass
        
        # 5. Flatten and Factor-Normalize
        # To ensure that Euclidean Distance (d) between any two vectors is in range [0, 1],
        # we divide the vector by sqrt(dim).
        # Proof: sqrt(sum(v1_i - v2_i)^2) <= sqrt(dim * 1^2) = sqrt(dim).
        # Thus, V_norm = V / sqrt(dim) ensures d <= 1.0.
        flat_vec = arr.flatten()
        dim = flat_vec.shape[0]
        return flat_vec / np.sqrt(dim)


def extract_spatial_color_signature(img_pil: Any, grid_size: int = 16, use_lab: bool = True) -> np.ndarray:
    """Legacy wrapper for backward compatibility."""
    sorter = ColorVibeSorter(grid_size=grid_size, use_lab=use_lab)
    return sorter.process_image(img_pil)


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


class VideoPerceptualHasher:
    """
    Implements the 'Tri-Path' video deduplication strategy.
    Levels:
      1. Binary MD5 (done upstream)
      2. Keyframe 'Squint' (pHash at 20%)
      3. Deep Temporal Hash (videohash library)
    """

    @staticmethod
    def get_duration(filepath: Path) -> float:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    @staticmethod
    def get_squint_hash(filepath: Path, duration: float) -> Optional[str]:
        """Level 2: Extract frame at 20% mark, 64x64, ImageHash pHash."""
        import imagehash
        from PIL import Image

        timestamp = duration * 0.2
        # Use ffmpeg to grab a single frame and pipe to memory
        cmd = [
            "ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", str(filepath),
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=15)
            if res.returncode == 0:
                from io import BytesIO
                img = Image.open(BytesIO(res.stdout))
                # Resize to 64x64 as per requirements
                img = img.resize((64, 64), Image.Resampling.LANCZOS)
                h = imagehash.phash(img)
                return str(h)
        except Exception as e:
            log.warning("Squint hash failed for %s: %s", filepath.name, e)
        return None

    @staticmethod
    def get_deep_hash(filepath: Path) -> Optional[str]:
        """Level 3: Full temporal hash using videohash (144x144 internal)."""
        try:
            from videohash import VideoHash
            vh = VideoHash(path=str(filepath))
            return vh.hash_hex
        except Exception as e:
            log.warning("Deep hash failed for %s: %s", filepath.name, e)
        return None


def process_video_worker(args):
    """Worker for Multiprocessing Pool."""
    fpath, enable_perceptual, enable_deep = args
    results = {"path": fpath, "md5": md5_hash(fpath)}
    
    if enable_perceptual:
        duration = VideoPerceptualHasher.get_duration(fpath)
        results["duration"] = duration
        results["squint"] = VideoPerceptualHasher.get_squint_hash(fpath, duration)
        if enable_deep:
            results["deep"] = VideoPerceptualHasher.get_deep_hash(fpath)
            
    return results


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


def detect_video_orientation(filepath: Path) -> tuple[str, int, int]:
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

    if rotation in (90, 270):
        width, height = height, width

    mode = "portrait" if height > width else "landscape"
    return mode, width, height


def phase1_video_sorting(
    root: Path, 
    dry_run: bool, 
    org_root: Path,
    action: str = "copy", 
    enable_deduplication: bool = True,
    enable_perceptual: bool = False,
    enable_deep: bool = False,
    video_match_threshold: int = 8,
    max_workers: int = 4,
    dedup_priority: str = "alphabetical",
    deployment_structure: str = "structured"
) -> tuple[list[dict], list[Path]]:
    """
    Move video files into videos/horizontal/ or videos/vertical/.
    Performs 'Tri-Path' de-duplication:
      - Level 1: Binary MD5
      - Level 2: Keyframe pHash (Squint)
      - Level 3: Temporal full-video hash
    """
    log.info("=" * 60)
    log.info("PHASE 1 — Video Sorting & Perceptual De-duplication")
    log.info("=" * 60)

    entries: list[dict] = []
    non_video_files: list[Path] = []
    
    # Define destinations
    dup_dir = org_root / "duplicates"
    review_dir = dup_dir / "review_needed"
    
    if deployment_structure == "flat":
        dest_h = org_root
        dest_v = org_root
    else:
        dest_h = org_root / "videos" / "horizontal"
        dest_v = org_root / "videos" / "vertical"

    all_potential_videos = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or is_hidden(path):
            continue
        try:
            path.relative_to(org_root)
            continue
        except ValueError:
            pass

        if path.suffix.lower() in VIDEO_EXTENSIONS:
            all_potential_videos.append(path)
        else:
            non_video_files.append(path)

    if not all_potential_videos:
        log.info("No videos found to sort.")
        return [], non_video_files

    # 1. Parallel Hashing
    log.info("Analyzing %d videos with parallel workers (cap: %d)...", len(all_potential_videos), max_workers)
    worker_args = [(v, enable_perceptual, enable_deep) for v in all_potential_videos]
    
    video_data = []
    # Use max_workers limit to prevent memory spikes
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        video_data = list(tqdm(
            executor.map(process_video_worker, worker_args),
            total=len(all_potential_videos),
            desc="Analyzing Videos",
            unit="video"
        ))

    # 2. Comparison Logic
    # Group results by various hashes
    md5_map: dict[str, list[dict]] = defaultdict(list)
    squint_map: dict[str, list[dict]] = defaultdict(list)
    deep_hashes: list[dict] = []

    for data in video_data:
        md5_map[data["md5"]].append(data)
        if data.get("squint"):
            squint_map[data["squint"]].append(data)
        if data.get("deep"):
            deep_hashes.append(data)

    processed_paths = set()

    def handle_video(data, cluster_id, reason, confidence, dest_base_dir, review_needed=False, match_source=None):
        fpath = data["path"]
        if fpath in processed_paths:
            return
        
        orientation, w, h = detect_video_orientation(fpath)
        if dest_base_dir is None: # Duplicate/Review path
            if review_needed:
                dest_dir = review_dir
            else:
                dest_dir = dup_dir
        else:
            dest_dir = dest_base_dir if orientation == "landscape" else dest_v if dest_base_dir == dest_h else dest_v
            # Wait, dest_base_dir logic was simpler:
            if orientation == "portrait":
                dest_dir = dest_v
            else:
                dest_dir = dest_h

        dest = dest_dir / fpath.name
        if dest.exists() or any(e["New_Path"] == str(dest) for e in entries):
            stem, suffix = dest.stem, dest.suffix
            dest = dest_dir / f"{stem}_{short_path_hash(fpath)}{suffix}"

        safe_action(fpath, dest, dry_run, action=action)
        entries.append({
            "Original_Path": str(fpath),
            "New_Path": str(dest) if not dry_run else f"[DRY RUN] {dest}",
            "New_Filename": dest.name,
            "Hash": data["md5"],
            "Cluster_ID": cluster_id,
            "Media_Type": "video",
            "Reason": reason,
            "Match_Confidence": confidence,
            "Match_Source": match_source,
            "Review_Needed": "Yes" if review_needed else "No"
        })
        processed_paths.add(fpath)

    # Level 1: Exact MD5
    if enable_deduplication:
        for h, matches in md5_map.items():
            if len(matches) > 1:
                # Keep the first one, mark others as duplicates
                # Sorting by path to be deterministic
                if dedup_priority == "path_depth":
                    sorted_matches = sorted(matches, key=lambda x: (-len(x["path"].parts), str(x["path"])))
                else:
                    sorted_matches = sorted(matches, key=lambda x: str(x["path"]))
                # The "original" (to keep)
                # Actually, in this script's flow, we move everything to 'Organized'.
                # So the first one goes to landscape/portrait, others go to duplicate.
                first = sorted_matches[0]
                handle_video(first, f"video/original", "Video: Unique (Binary)", "1.0", dest_h)
                
                for dup in sorted_matches[1:]:
                    handle_video(dup, "duplicate", f"Matched to: {first['path'].name}", "1.0 (Bit-for-bit)", None, match_source=first['path'].name)

    # Level 2: Squint (pHash)
    if enable_perceptual:
        for ph, matches in squint_map.items():
            unprocessed = [m for m in matches if m["path"] not in processed_paths]
            if len(unprocessed) >= 1:
                # If there's an existing md5_map entry that WAS processed and has same ph...
                # Actually, simpler: if phash matches and we haven't processed this file yet,
                # it's a high-probability match to SOMETHING.
                for match in unprocessed:
                    # Is there ANOTHER file (processed or not) with the same pHash?
                    others = [m for m in matches if m["path"] != match["path"]]
                    if others:
                        handle_video(match, "duplicate/review", f"Matched to: {others[0]['path'].name}", "0.9 (Visual)", None, review_needed=True, match_source=others[0]['path'].name)

    # Level 3: Deep (videohash)
    if enable_deep and deep_hashes:
        from imagehash import hex_to_hash
        for i, data1 in enumerate(video_data):
            if data1["path"] in processed_paths or not data1.get("deep"):
                continue
            
            h1 = hex_to_hash(data1["deep"])
            for j, data2 in enumerate(video_data):
                if i == j or not data2.get("deep"):
                    continue
                
                h2 = hex_to_hash(data2["deep"])
                distance = h1 - h2
                if distance <= video_match_threshold:
                    handle_video(data1, "duplicate/review", f"Matched to: {data2['path'].name}", f"0.8 (Temporal)", None, review_needed=True, match_source=data2['path'].name)
                    break

    # 3. Handle remaining unique videos
    for data in video_data:
        if data["path"] not in processed_paths:
            orientation, w, h = detect_video_orientation(data["path"])
            reason = f"Video: {'Vertical' if orientation == 'portrait' else 'Horizontal'} ({w}x{h})"
            handle_video(data, f"video/{orientation}", reason, "1.0", dest_h)

    log.info("Phase 1 complete: %d videos processed.", len(entries))
    return entries, non_video_files


# ---------------------------------------------------------------------------
# Phase 2 — Image De-duplication
# ---------------------------------------------------------------------------


def phase2_deduplication(
    root: Path, files: list[Path], dry_run: bool, org_root: Path, action: str = "copy",
    dedup_priority: str = "alphabetical"
) -> tuple[list[dict], list[Path], dict[str, str]]:
    """
    Hash images; move duplicates to /duplicates/.
    Returns (log_entries, unique_image_paths).
    """
    log.info("=" * 60)
    log.info("PHASE 2 — Image De-duplication")
    log.info("=" * 60)

    dup_dir = org_root / "duplicates"
    entries: list[dict] = []
    unique: list[Path] = []
    seen_hashes: dict[str, Path] = {}
    image_files = [f for f in files if f.suffix.lower() in IMAGE_EXTENSIONS]
    non_image_files = [f for f in files if f.suffix.lower() not in IMAGE_EXTENSIONS]

    # Pre-sort to respect dedup priority
    if dedup_priority == "path_depth":
        image_files = sorted(image_files, key=lambda p: (-len(p.parts), str(p)))
    else:
        image_files = sorted(image_files, key=lambda p: str(p))

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
                    "Reason": f"Matched to: {seen_hashes[h].name}",
                    "Match_Confidence": "1.0 (Bit-for-bit)",
                    "Match_Source": seen_hashes[h].name,
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
    # Return hash map for downstream AI cache lookups
    path_to_hash = {str(p): h for h, p in seen_hashes.items()}
    return entries, unique, path_to_hash


def phase2b_folder_flattening(
    root: Path, images: list[Path], dry_run: bool, org_root: Path, action: str = "copy",
    rename: bool = True, rename_style: str = "folder_name"
) -> tuple[list[dict], list[Path], dict[str, str]]:
    """
    Flatten subfolders into the root of Organized.
    Returns (log_entries, remaining_files_not_flattened, path_to_hash).
    """
    log.info("=" * 60)
    log.info("PHASE 2b — Subfolder Flattening")
    log.info("=" * 60)

    entries: list[dict] = []
    remaining: list[Path] = []
    
    dup_dir = org_root / "duplicates"
    
    # 1. Group by parent folder relative to root
    folders = defaultdict(list)
    for p in images:
        try:
            rel = p.parent.relative_to(root)
            # Store root files under a virtual "[ROOT]" parent
            folder_key = rel if str(rel) != "." else Path("[ROOT]")
            folders[folder_key].append(p)
        except ValueError:
            remaining.append(p)

    # Track hashes to return for Phase 3 clustering
    path_to_hash = {}

    # 2. Process each folder
    for rel_path, paths in sorted(folders.items()):
        parent_name = rel_path.name
        is_root = (rel_path == Path("[ROOT]"))
        # Sort by creation time
        paths.sort(key=lambda x: x.stat().st_ctime if x.exists() else 0)
        
        for i, fpath in enumerate(paths, 1):
            # Calculate hash BEFORE move (avoids FileNotFoundError)
            h = ""
            if fpath.suffix.lower() in IMAGE_EXTENSIONS:
                h = md5_hash(fpath)
                path_to_hash[str(fpath)] = h

            if rename and not is_root:
                if rename_style == "folder_only":
                    # {Folder} - {Seq}
                    new_name = f"{parent_name} - {i:03d}{fpath.suffix}"
                else:
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
                "Hash": h,
                "Cluster_ID": "flattened",
                "Media_Type": "image" if fpath.suffix.lower() in IMAGE_EXTENSIONS else "other",
                "Reason": f"Flattened from subfolder: {rel_path}",
                "Match_Confidence": "1.0",
            })
            
    log.info("Phase 2b complete: %d files flattened.", len(entries))
    return entries, remaining, path_to_hash


# ---------------------------------------------------------------------------
# Phase 3 Helpers — Name Grouping
# ---------------------------------------------------------------------------

def apply_name_grouping_overrides(
    valid_paths: list[Path],
    labels: np.ndarray,
    group_name_matches: bool,
    group_name_prefix: bool,
    min_size: int = 2,
    sensitivity: float = 0.5
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
            if len(indices) < min_size:
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
            # Similarity scales the minimum character length required for a match signal.
            # Sensitivity 0.0 -> threshold 0; Sensitivity 1.0 -> threshold 10
            threshold = int(sensitivity * 10)
            if core and len(core) >= threshold:
                prefix_groups[core].append(idx)

        merged_count = 0
        next_new_label = max(labels) + 1 if len(labels) > 0 else 0

        for core, indices in prefix_groups.items():
            if len(indices) < min_size:
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
    org_root: Path,
    action: str = "copy",
    group_name_matches: bool = False,
    group_name_prefix: bool = False,
    name_grouping_min_size: int = 2,
    name_grouping_sensitivity: float = 0.5,
    deployment_structure: str = "structured"
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

    labels = apply_name_grouping_overrides(
        valid_paths, labels, group_name_matches, group_name_prefix,
        min_size=name_grouping_min_size,
        sensitivity=name_grouping_sensitivity
    )

    # Move files and build entries
    entries: list[dict] = []
    cluster_map: dict[int, list[Path]] = defaultdict(list)

    for fpath, label in zip(valid_paths, labels):
        h = md5_hash(fpath)
        if label == -1:
            dest_dir = org_root if deployment_structure == "flat" else org_root / "unsorted"
            cluster_id = "noise"
            reason = "Outlier: No pattern match"
        else:
            dest_dir = org_root if deployment_structure == "flat" else org_root / "groups" / f"Group_{label:03d}"
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
            "Match_Confidence": "1.0000",
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
    org_root: Path,
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
    name_grouping_min_size: int = 2,
    name_grouping_sensitivity: float = 0.5,
    ai_clustering_mode: str = "individual",
    force_rescan: bool = False,
    hash_map: Optional[dict[str, str]] = None,
    enable_color_features: bool = True,
    grid_detail: int = 16,
    use_lab_space: bool = True,
    similarity_sensitivity: Optional[float] = None,
    deployment_structure: str = "structured"
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

    # 1. Initialize Cache
    fcache = FeatureCache(root)
    hash_map = hash_map or {}

    # ------------------------------------------------------------------
    # 3a  Visual embeddings via CLIP
    # ------------------------------------------------------------------
    import torch
    import clip
    from PIL import Image
    from tqdm import tqdm

    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    # Lazy load model only if cache misses exist
    model, preprocess = None, None

    embeddings: list[np.ndarray] = []
    color_features: list[np.ndarray] = []
    hash_features: list[np.ndarray] = []
    valid_paths: list[Path] = []
    timestamps: list[float] = []

    log.info("Embedding images …")
    for fpath in tqdm(image_paths, desc="  Creating embeddings", unit="img", leave=False, file=sys.stdout):
        try:
            md5 = hash_map.get(str(fpath)) or md5_hash(fpath)
            
            # 2. Check Cache (Skip if force_rescan is True)
            cached = fcache.get(fpath, md5) if fcache and not force_rescan else None
            
            if cached:
                # Use cached features
                feat = np.array(cached["clip"])
                h_feat = np.array(cached["vhash"])
                
                # Setting-Aware Color Cache: Only use if settings match
                c_feat = None
                if "color" in cached:
                    c_grid = cached.get("color_grid_size")
                    c_lab = cached.get("color_use_lab", True)
                    if c_grid == grid_detail and c_lab == use_lab_space:
                        c_feat = np.array(cached["color"])
                    
                if c_feat is None and (enable_color_features and color_weight > 0):
                    # Cache miss for color specifically or settings changed
                    img = Image.open(fpath)
                    c_feat = extract_spatial_color_signature(img, grid_detail, use_lab_space)
                    # Update cache entry in memory (will be saved later)
                    cached["color"] = c_feat.tolist()
                    cached["color_grid_size"] = grid_detail
                    cached["color_use_lab"] = use_lab_space
                elif c_feat is None:
                    # Color disabled or weight 0, use dummy/empty
                    c_feat = np.zeros(3 * (grid_detail**2))
            else:
                # 3. Cache Miss: Extract via AI
                img = Image.open(fpath)
                
                # --- CLIP Embedding ---
                if visual_weight > 0:
                    if model is None:
                        log.info("Cache miss. Loading CLIP model (ViT-B/32) on %s …", device)
                        model, preprocess = clip.load("ViT-B/32", device=device)

                    tensor = preprocess(img.convert("RGB")).unsqueeze(0).to(device)
                    with torch.no_grad():
                        feat_t = model.encode_image(tensor)
                    feat = feat_t.cpu().numpy().flatten()
                    feat = feat / (np.linalg.norm(feat) + 1e-10)  # L2 normalize
                else:
                    feat = np.zeros(512)

                # --- Spatial Color Signature ---
                if enable_color_features and color_weight > 0:
                    c_feat = extract_spatial_color_signature(img, grid_detail, use_lab_space)
                else:
                    c_feat = np.zeros(3 * (grid_detail**2))

                # --- Visual Hash (perceptual/structural) ---
                thumb_h = img.resize((16, 16)).convert("L")
                h_feat = np.array(thumb_h).flatten().astype(float)
                h_feat /= (np.linalg.norm(h_feat) + 1e-10)

                # Store in cache
                if fcache:
                    fcache.set(fpath, md5, {
                        "clip": feat.tolist(),
                        "color": c_feat.tolist(),
                        "color_grid_size": grid_detail,
                        "color_use_lab": use_lab_space,
                        "vhash": h_feat.tolist(),
                    })

            embeddings.append(feat)
            color_features.append(c_feat)
            hash_features.append(h_feat)
            valid_paths.append(fpath)

            dt = get_file_datetime(fpath)
            timestamps.append(dt.timestamp())
        except Exception as exc:
            log.warning("Skipping %s: %s", fpath, exc)

    # 4. Save Cache
    if fcache:
        fcache.save()

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

    # Map VisiPics-style Sensitivity Slider to Epsilon
    # Formula: Distance_Threshold = (1.0 - Sensitivity)
    if similarity_sensitivity is not None:
        log.info("Mapping Sensitivity %.2f -> Epsilon %.2f", similarity_sensitivity, 1.0 - similarity_sensitivity)
        cluster_selection_epsilon = 1.0 - similarity_sensitivity

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
    # 4. Integrate name grouping overrides (heuristic patterns)
    if group_name_matches or group_name_prefix:
        labels = apply_name_grouping_overrides(
            valid_paths, labels, group_name_matches, group_name_prefix,
            min_size=name_grouping_min_size,
            sensitivity=name_grouping_sensitivity
        )

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
        # Efficient distance matrix calculation using sklearn (prevents ArrayMemoryError)
        from sklearn.metrics import pairwise_distances
        dists = pairwise_distances(cluster_points, metric='euclidean')
        avg_dists = dists.mean(axis=1)
        m_idx_in_c = np.argmin(avg_dists)
        cluster_stats[label] = {
            "medoid_point": combined[indices[m_idx_in_c]],
            "dispersion": avg_dists[m_idx_in_c],
        }

    # ------------------------------------------------------------------
    # 3e  Move files
    # ------------------------------------------------------------------
    entries: list[dict] = []
    cluster_map: dict[int, list[Path]] = defaultdict(list)

    probs = clusterer.probabilities_
    for zip_idx, (fpath, label, prob) in enumerate(zip(valid_paths, labels, probs)):
        h = md5_hash(fpath)
        if label == -1:
            dest_dir = org_root if deployment_structure == "flat" else org_root / "unsorted"
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
            dest_dir = org_root if deployment_structure == "flat" else org_root / "groups" / f"Group_{label:03d}"
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
                "Match_Confidence": f"{prob:.4f}",
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
    org_root: Path,
    rename_style: str = "folder_name",
) -> list[dict]:
    """
    Rename files inside /groups/*/ and /unsorted/ based on their *original*
    parent folder name + sequential numbering.  Returns updated log entries.

    rename_style:
        'folder_name'  -> Folder - OriginalName - 001.jpg
        'folder_only'  -> Folder - 001.jpg
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
                # Detect if it's in a clustered group (works for both structured and flat)
                cluster_id = entry.get("Cluster_ID", "")
                is_in_group = cluster_id.startswith("group_") and cluster_id != "group_noise"
                
                if is_in_group:
                    # Sequence-only for root files in groups
                    ext = Path(entry["New_Filename"]).suffix
                    new_name = f"{orig.stem} - {counter:03d}{ext}"
                else:
                    # Keep original filename for root-level files elsewhere
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
                orig_stem = orig.stem
                if rename_style == "folder_name":
                    new_name = f"{base} - {orig_stem} - {counter:03d}{ext}"
                else:
                    new_name = f"{base} - {counter:03d}{ext}"

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
# Commit from Log — execute a dry-run log without re-scanning
# ---------------------------------------------------------------------------


def commit_from_log(log_path: Path, action_override: str = None) -> Path:
    """
    Read a previously generated dry-run CSV log and execute the file operations.
    
    For each entry:
      1. Verify Original_Path still exists.
      2. If a Hash is recorded, verify the file's current MD5 matches.
      3. Execute the move/copy operation via safe_action.
    
    Returns the path to the new "committed" log file.
    """
    log.info("=" * 60)
    log.info("COMMIT FROM LOG — Executing saved plan")
    log.info("=" * 60)
    log.info("Source log: %s", log_path)

    if not log_path.exists():
        log.error("Log file not found: %s", log_path)
        sys.exit(1)

    # 1. Read the CSV log
    with open(log_path, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        entries = list(reader)

    if not entries:
        log.warning("Log file is empty. Nothing to commit.")
        return log_path

    # 2. Extract settings from first row
    settings = {}
    try:
        settings_json = entries[0].get("Settings", "{}")
        settings = json.loads(settings_json) if settings_json else {}
    except (json.JSONDecodeError, TypeError):
        pass

    action = action_override or settings.get("action", "copy")
    log.info("Action mode: %s", action.upper())
    log.info("Entries to process: %d", len(entries))

    # 3. Validation & Execution
    committed = []
    skipped_missing = []
    skipped_hash = []
    skipped_exists = []
    errors = []

    log.info("-" * 60)
    log.info("Phase 1/2: Validating file integrity...")
    log.info("-" * 60)

    for i, entry in enumerate(entries, 1):
        orig_path_str = entry.get("Original_Path", "")
        new_path_str = entry.get("New_Path", "")
        recorded_hash = entry.get("Hash", "")

        # Strip dry-run prefixes from paths
        new_path_str = new_path_str.replace("[DRY RUN] ", "").replace("[COPY] ", "").replace("[MOVE] ", "")

        src = Path(orig_path_str)
        dst = Path(new_path_str)

        # --- Check 1: Source file exists ---
        if not src.exists():
            skipped_missing.append(entry)
            log.warning("  [SKIP] Missing: %s", src.name)
            continue

        # --- Check 2: Hash integrity (only for entries with a hash) ---
        if recorded_hash:
            try:
                current_hash = md5_hash(src)
                if current_hash != recorded_hash:
                    skipped_hash.append(entry)
                    log.warning("  [SKIP] Hash mismatch: %s (expected %s, got %s)",
                                src.name, recorded_hash[:12], current_hash[:12])
                    continue
            except Exception as e:
                errors.append({"entry": entry, "error": str(e)})
                log.error("  [ERROR] Hash check failed for %s: %s", src.name, e)
                continue

        # --- Check 3: Destination already exists ---
        if dst.exists():
            skipped_exists.append(entry)
            log.info("  [SKIP] Already exists: %s", dst.name)
            continue

        # --- Execute ---
        try:
            safe_action(src, dst, dry_run=False, action=action)
            entry["New_Path"] = str(dst)
            committed.append(entry)
        except Exception as e:
            errors.append({"entry": entry, "error": str(e)})
            log.error("  [ERROR] Failed to %s %s: %s", action, src.name, e)

        # Progress reporting
        if i % 50 == 0 or i == len(entries):
            log.info("  Progress: %d/%d processed", i, len(entries))

    # 4. Summary
    log.info("=" * 60)
    log.info("COMMIT SUMMARY")
    log.info("=" * 60)
    log.info("  [OK] Committed:          %d", len(committed))
    log.info("  [--] Skipped (missing):  %d", len(skipped_missing))
    log.info("  [--] Skipped (hash):     %d", len(skipped_hash))
    log.info("  [--] Skipped (exists):   %d", len(skipped_exists))
    log.info("  [!!] Errors:             %d", len(errors))

    # 5. Write a new "committed" log
    committed_settings = {**settings, "dry_run": False, "committed_from": str(log_path)}
    all_result_entries = []
    for e in committed:
        e["Reason"] = f"[COMMITTED] {e.get('Reason', '')}"
        all_result_entries.append(e)
    for e in skipped_missing:
        e["Reason"] = f"[SKIPPED:MISSING] {e.get('Reason', '')}"
        e["Review_Needed"] = "true"
        all_result_entries.append(e)
    for e in skipped_hash:
        e["Reason"] = f"[SKIPPED:HASH_MISMATCH] {e.get('Reason', '')}"
        e["Review_Needed"] = "true"
        all_result_entries.append(e)
    for e in skipped_exists:
        e["Reason"] = f"[SKIPPED:EXISTS] {e.get('Reason', '')}"
        all_result_entries.append(e)

    # Determine root from original entries
    root_guess = Path(entries[0]["Original_Path"]).parent
    result_log_path = phase5_write_log(root_guess, all_result_entries, dry_run=False, settings=committed_settings)

    log.info("Committed log written to: %s", result_log_path)
    return result_log_path


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


def _get_video_params() -> dict:
    """Prompt for Video Sorting & Deduplication parameters."""
    print("\n  Video Deduplication Settings:")
    enable_sorting = _prompt("  Enable Video Orientation Sorting? [Y/n]: ", "y").lower() == "y"
    enable_dedup = _prompt("  Enable Binary (MD5) Deduplication? [Y/n]: ", "y").lower() == "y"
    enable_perceptual = _prompt("  Enable Perceptual (Squint) Deduplication? [y/N]: ", "n").lower() == "y"
    
    enable_deep = False
    match_threshold = 8
    if enable_perceptual:
        enable_deep = _prompt("  Enable Deep Temporal Scan (videohash)? [y/N]: ", "n").lower() == "y"
        raw_thresh = _prompt("  Match Threshold (0-64) [8]: ", "8")
        try:
            match_threshold = int(raw_thresh)
        except ValueError:
            match_threshold = 8

    max_workers = 4
    raw_workers = _prompt("  Max Concurrent Video Workers [4]: ", "4")
    try:
        max_workers = int(raw_workers)
    except ValueError:
        max_workers = 4

    return {
        "enable_video_sorting": enable_sorting,
        "enable_video_deduplication": enable_dedup,
        "enable_perceptual_video_dedup": enable_perceptual,
        "enable_deep_video_scan": enable_deep,
        "video_match_threshold": match_threshold,
        "video_max_workers": max_workers
    }


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

    raw_sens = _prompt("  Similarity Sensitivity [0.95]: ", "0.95")
    try:
        sens = float(raw_sens)
    except ValueError:
        sens = 0.95

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

    enable_color = _prompt("  Enable Spatial Color Signature? [Y/n]: ", "y").lower() == "y"
    grid_size = 16
    use_lab = True
    if enable_color:
        raw_gs = _prompt("  Color Grid Detail [16]: ", "16")
        try:
            grid_size = int(raw_gs)
        except ValueError:
            grid_size = 16
        use_lab = _prompt("  Use CIELAB Color Space? [Y/n]: ", "y").lower() == "y"

    return {
        "min_cluster_size": min_size,
        "min_samples": min_samples,
        "epsilon": eps,
        "similarity_sensitivity": sens,
        "method": method,
        "temporal_weight": tw,
        "filename_weight": fw,
        "color_weight": cw,
        "near_duplicate_weight": nw,
        "ai_clustering_mode": mode,
        "enable_color_features": enable_color,
        "grid_detail": grid_size,
        "use_lab_space": use_lab
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
    is_flattening = False
    master_hash_map: dict[str, str] = {}

    while True:
        print(MENU)
        choice = _prompt("  Select an option: ").upper()

        # ---- Phase 1 ----
        if choice == "1":
            v_params = _get_video_params()
            use_sub = v_params.get("use_organized_subfolder", True)
            
            # Safety: Force subfolder for Copy mode
            if action == "copy" and not use_sub:
                print("  [SAFETY] 'Copy' mode detected. Forcing '/Organized/' subfolder.")
                use_sub = True
                
            org_root = root / ORGANIZED_DIR_NAME if use_sub else root
            
            video_entries, remaining_files = phase1_video_sorting(
                root, dry_run, org_root, action=action,
                enable_deduplication=v_params["enable_video_deduplication"],
                enable_perceptual=v_params["enable_perceptual_video_dedup"],
                enable_deep=v_params["enable_deep_video_scan"],
                video_match_threshold=v_params["video_match_threshold"],
                max_workers=v_params["video_max_workers"],
                dedup_priority=v_params.get("dedup_priority", "alphabetical"),
                deployment_structure=v_params.get("deployment_structure", "structured")
            )
            all_entries.extend(video_entries)

        # ---- Phase 2 ----
        elif choice == "2":
            if remaining_files is None:
                # Collect files fresh if Phase 1 was not run this session
                remaining_files = [
                    f for f in collect_all_files(root)
                    if f.suffix.lower() not in VIDEO_EXTENSIONS
                ]
            dup_entries, unique_images, hashes = phase2_deduplication(
                root, remaining_files, dry_run, org_root, action=action,
                dedup_priority=v_params.get("dedup_priority", "alphabetical")
            )
            all_entries.extend(dup_entries)
            master_hash_map.update(hashes)

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
                root, unique_images, dry_run, org_root,
                action=action,
                min_cluster_size=params["min_cluster_size"],
                min_samples=params["min_samples"],
                cluster_selection_epsilon=params["epsilon"],
                cluster_selection_method=params["method"],
                similarity_sensitivity=params.get("similarity_sensitivity"),
                temporal_weight=params["temporal_weight"],
                filename_weight=params["filename_weight"],
                color_weight=params["color_weight"],
                near_duplicate_weight=params["near_duplicate_weight"],
                visual_weight=1.0,  # Default in interactive menu mode
                ai_clustering_mode=params.get("ai_clustering_mode", "individual"),
                use_feature_cache=True, # Default to True in interactive mode
                hash_map=master_hash_map,
                enable_color_features=params["enable_color_features"],
                grid_detail=params["grid_detail"],
                use_lab_space=params["use_lab_space"],
                group_name_matches=params.get("group_name_matches", False),
                group_name_prefix=params.get("group_name_prefix", False),
                name_grouping_min_size=params.get("name_grouping_min_size", 2),
                name_grouping_sensitivity=params.get("name_grouping_sensitivity", 0.5),
                deployment_structure=params.get("deployment_structure", "structured"),
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
            rs_choice = _prompt("  Rename Style [1] Folder - Name - 001  [2] Folder - 001: ", "1")
            rs = "folder_name" if rs_choice == "1" else "folder_only"
            flat_entries, unique_images, flat_hashes = phase2b_folder_flattening(
                root, unique_images, dry_run, org_root, action=action, rename=rename_confirm, rename_style=rs
            )
            all_entries.extend(flat_entries)
            master_hash_map.update(flat_hashes)
            if flat_entries:
                is_flattening = True

        # ---- Phase 4 ----
        elif choice == "4":
            if cluster_entries and not is_flattening:
                rename_style = _prompt("  Rename Style [1] Folder - Name - 001  [2] Folder - 001: ", "1")
                rs = "folder_name" if rename_style == "1" else "folder_only"
                cluster_entries = phase4_contextual_naming(
                    root, cluster_entries, dry_run, org_root, rename_style=rs
                )
                # Update the entries that were already added
                # (replace the Phase 3 entries with renamed versions)
                # Remove old phase-3 originals and add renamed ones
                orig_paths = {e["Original_Path"] for e in cluster_entries}
                all_entries = [
                    e for e in all_entries if e["Original_Path"] not in orig_paths
                ]
                all_entries.extend(cluster_entries)
            else:
                print("  ⚠  Phase 3 has not been run yet (no cluster entries) or Flattening is active.")

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
            v_params = _get_video_params()
            video_entries, remaining_files = phase1_video_sorting(
                root, dry_run, action=action,
                enable_deduplication=v_params["enable_video_deduplication"],
                enable_perceptual=v_params["enable_perceptual_video_dedup"],
                enable_deep=v_params["enable_deep_video_scan"],
                video_match_threshold=v_params["video_match_threshold"],
                max_workers=v_params["video_max_workers"],
                dedup_priority=v_params.get("dedup_priority", "alphabetical")
            )
            all_entries.extend(video_entries)

            # Phase 2
            dup_entries, unique_images, phase2_hashes = phase2_deduplication(
                root, remaining_files, dry_run, org_root, action=action,
                dedup_priority=v_params.get("dedup_priority", "alphabetical")
            )
            all_entries.extend(dup_entries)
            master_hash_map.update(phase2_hashes)

            # Phase 2b
            is_flat = False
            if enable_flat:
                rename_flat = _prompt("  Rename with parent folder name? [Y/n]: ", "y").lower() == "y"
                rs_choice_flat = _prompt("  Rename Style [1] Folder - Name - 001  [2] Folder - 001: ", "1")
                rs_flat = "folder_name" if rs_choice_flat == "1" else "folder_only"
                flat_entries, unique_images, f_hashes = phase2b_folder_flattening(
                    root, unique_images, dry_run, org_root, action=action, rename=rename_flat, rename_style=rs_flat
                )
                all_entries.extend(flat_entries)
                master_hash_map.update(f_hashes)
                if flat_entries:
                    is_flat = True
                    is_flattening = True

            # Phase 3
            clust_entries = []
            # Modified: Even if is_flat, we still check for Name Grouping (Smart Flattening)
            if not is_flat and (params.get("enable_ai_clustering", True) or params.get("enable_color_features", True)):
                clust_entries, _ = phase3_clustering(
                    root, unique_images, dry_run, org_root,
                    action=action,
                    min_cluster_size=params.get("min_cluster_size", 3),
                    min_samples=params.get("min_samples"),
                    cluster_selection_epsilon=params.get("epsilon", 0.0),
                    cluster_selection_method=params.get("method", "leaf"),
                    similarity_sensitivity=params.get("similarity_sensitivity"),
                    temporal_weight=params.get("temporal_weight", 0.3),
                    force_rescan=False,
                    hash_map=master_hash_map,
                    filename_weight=params.get("filename_weight", 0.0),
                    color_weight=params.get("color_weight", 0.0),
                    near_duplicate_weight=params.get("near_duplicate_weight", 0.0),
                    ai_clustering_mode=params.get("ai_clustering_mode", "individual"),
                    enable_color_features=params.get("enable_color_features", True),
                    grid_detail=params.get("grid_detail", 16),
                    use_lab_space=params.get("use_lab_space", True),
                    group_name_matches=params.get("group_name_matches", False),
                    group_name_prefix=params.get("group_name_prefix", False),
                    name_grouping_min_size=params.get("name_grouping_min_size", 2),
                    name_grouping_sensitivity=params.get("name_grouping_sensitivity", 0.5),
                    deployment_structure=params.get("deployment_structure", "structured"),
                )
            elif params.get("group_name_matches", False) or params.get("group_name_prefix", False):
                # Smart Flattening path or Standalone Grouping
                prefix = "[SMART FLATTEN] " if is_flat else "[INFO] "
                log.info("%sName Grouping is enabled. Running heuristic scan.", prefix)
                clust_entries, _ = phase3_standalone_grouping(
                    root, unique_images, dry_run, org_root,
                    action=action,
                    group_name_matches=params.get("group_name_matches", False),
                    group_name_prefix=params.get("group_name_prefix", False),
                    name_grouping_min_size=params.get("name_grouping_min_size", 2),
                    name_grouping_sensitivity=params.get("name_grouping_sensitivity", 0.5),
                    deployment_structure=params.get("deployment_structure", "structured"),
                )
            elif is_flat:
                log.info("[SKIP] Phase 3: AI Clustering and Name Grouping skipped (Flattening Only).")
            else:
                log.info("[SKIP] Phase 3: AI Clustering and Name Grouping disabled.")
            
            cluster_entries = clust_entries

            # Phase 4
            if not is_flat and cluster_entries:
                rename_style_choice = _prompt("  Rename Style [1] Folder - Name - 001  [2] Folder - 001: ", "1")
                rs = "folder_name" if rename_style_choice == "1" else "folder_only"
                cluster_entries = phase4_contextual_naming(
                    root, cluster_entries, dry_run, org_root, rename_style=rs
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
    parser.add_argument("--commit-log", type=Path, default=None,
                        help="Execute file operations from a previously generated log (bypasses all phases).")

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

    # --- Commit from Log mode (fast path) ---
    if args.commit_log is not None:
        log_file = Path(args.commit_log)
        if not log_file.is_absolute():
            log_file = Path(__file__).parent / log_file
        print("[STARTUP] Commit-from-Log mode", flush=True)
        commit_from_log(log_file)
        return

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

    # Global Org Root logic
    use_sub = _arg("use_organized_subfolder")
    
    # Safety: If action is 'copy', we MUST use an organized subfolder to avoid root clutter
    if _arg("action") == "copy" and not use_sub:
        log.info("[SAFETY] 'Copy' mode detected. Forcing use of '/Organized/' subfolder to prevent root clutter.")
        use_sub = True

    org_root = root / ORGANIZED_DIR_NAME if use_sub else root

    if not use_sub:
        log.warning("!")
        log.warning("!!! CAUTION: Base Root Organization active. Files will be dumped DIRECTLY into: %s", root)
        log.warning("!!! This makes 'Undo' difficult. Ensure you are using --action copy for safety.")
        log.warning("!")

    # Phase 1
    video_entries = []
    remaining_files = []
    if _arg("enable_video_sorting"):
        video_entries, remaining_files = phase1_video_sorting(
            root, _arg("dry_run"), org_root, action=_arg("action"), 
            enable_deduplication=_arg("enable_video_deduplication"),
            enable_perceptual=_arg("enable_perceptual_video_dedup"),
            enable_deep=_arg("enable_deep_video_scan"),
            video_match_threshold=_arg("video_match_threshold"),
            max_workers=_arg("video_max_workers"),
            dedup_priority=_arg("dedup_priority"),
            deployment_structure=_arg("deployment_structure")
        )
        all_entries.extend(video_entries)
    else:
        log.info("[SKIP] Phase 1: Video Sorting disabled.")
        # Need to collect all files if Phase 1 is skipped
        remaining_files = list(root.rglob("*"))
        remaining_files = [f for f in remaining_files if f.is_file() and not is_hidden(f)]

    # Phase 2
    unique_images = remaining_files
    master_hash_map = {}
    if _arg("enable_deduplication"):
        dup_entries, unique_images, master_hash_map = phase2_deduplication(
            root, remaining_files, _arg("dry_run"), org_root, action=_arg("action"),
            dedup_priority=_arg("dedup_priority")
        )
        all_entries.extend(dup_entries)
    else:
        log.info("[SKIP] Phase 2: De-duplication disabled.")

    # Phase 2b: Folder Flattening (New)
    is_flattening = False
    if _arg("enable_folder_flattening"):
        flatten_entries, unique_images, flatten_hashes = phase2b_folder_flattening(
            root,
            unique_images,
            _arg("dry_run"),
            org_root,
            action=_arg("action"),
            rename=_arg("flatten_rename"),
            rename_style=_arg("rename_style"),
        )
        all_entries.extend(flatten_entries)
        master_hash_map.update(flatten_hashes)
        if flatten_entries:
            is_flattening = True
            log.info("[INFO] Subfolder Flattening active. AI Clustering and Name Grouping will be skipped.")

    # Phase 3
    if is_flattening:
        log.info("[SKIP] Phase 3 & 3b: Skipped due to Subfolder Flattening.")
        cluster_entries = []
    elif _arg("enable_ai_clustering") or _arg("enable_color_features"):
        cluster_entries, cluster_map = phase3_clustering(
            root,
            unique_images,
            _arg("dry_run"),
            org_root,
            action=_arg("action"),
            min_cluster_size=_arg("min_cluster_size"),
            min_samples=_arg("min_samples"),
            cluster_selection_epsilon=_arg("epsilon"),
            cluster_selection_method=_arg("method"),
            similarity_sensitivity=_arg("similarity_sensitivity"),
            temporal_weight=_arg("temporal_weight"),
            filename_weight=_arg("filename_weight"),
            color_weight=_arg("color_weight"),
            near_duplicate_weight=_arg("near_duplicate_weight"),
            visual_weight=_arg("visual_weight"),
            group_name_matches=_arg("group_name_matches"),
            group_name_prefix=_arg("group_name_prefix"),
            name_grouping_min_size=_arg("name_grouping_min_size"),
            name_grouping_sensitivity=_arg("name_grouping_sensitivity"),
            ai_clustering_mode=_arg("ai_clustering_mode"),
            force_rescan=_arg("force_rescan"),
            hash_map=master_hash_map,
            enable_color_features=_arg("enable_color_features"),
            grid_detail=_arg("grid_detail"),
            use_lab_space=_arg("use_lab_space"),
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
            name_grouping_min_size=_arg("name_grouping_min_size"),
            name_grouping_sensitivity=_arg("name_grouping_sensitivity"),
        )
    else:
        log.info("[SKIP] Phase 3 & 3b: AI Clustering and Name Grouping disabled.")

    # Phase 4 (Contextual Naming — applies to both AI or name-based clusters)
    if not is_flattening and cluster_entries:
        cluster_entries = phase4_contextual_naming(
            root, cluster_entries, _arg("dry_run"), org_root, rename_style=_arg("rename_style")
        )
        all_entries.extend(cluster_entries)

    # Phase 5 — auto-generate run_settings from schema
    run_settings = {p["key"]: _arg(p["key"]) for p in PARAM_SCHEMA}
    phase5_write_log(root, all_entries, _arg("dry_run"), settings=run_settings)

    log.info("=" * 60)
    log.info("Done. %d total file operations recorded.", len(all_entries))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
