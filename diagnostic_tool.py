#!/usr/bin/env python3
"""
Diagnostic Tool for Media Organizer
==================================
Analyzes a folder of manually grouped images to determine their visual, 
temporal, and structural spread. This helps in tuning 'epsilon' and 
other weights for the main organizer.
"""

import os
import sys
import json
import logging
import hashlib
import re
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

import numpy as np
from PIL import Image
from tqdm import tqdm

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# --- Configuration (Mirrored from media_organizer.py) ---
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".heic"}

# --- Utility Functions ---

def get_exif_datetime(filepath: Path) -> Optional[datetime]:
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            exif = img.getexif()
            if exif:
                # 36867 = DateTimeOriginal, 306 = DateTime
                dt_str = exif.get(36867) or exif.get(306)
                if dt_str:
                    return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None

def get_file_datetime(filepath: Path) -> datetime:
    dt = get_exif_datetime(filepath)
    if dt:
        return dt
    return datetime.fromtimestamp(filepath.stat().st_mtime)

def get_features(folder_path: Path):
    import torch
    import clip
    from sklearn.preprocessing import StandardScaler
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.decomposition import TruncatedSVD

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info(f"Loading CLIP model on {device}...")
    model, preprocess = clip.load("ViT-B/32", device=device)

    image_paths = [p for p in folder_path.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not image_paths:
        log.error(f"No images found in {folder_path}")
        return None

    embeddings = []
    color_features = []
    hash_features = []
    timestamps = []
    stems = []
    valid_paths = []

    log.info(f"Analyzing {len(image_paths)} images...")
    for fpath in tqdm(image_paths, desc="Extracting features"):
        try:
            with Image.open(fpath) as img:
                img_rgb = img.convert("RGB")
                
                # 1. CLIP Visual Embedding
                tensor = preprocess(img_rgb).unsqueeze(0).to(device)
                with torch.no_grad():
                    feat = model.encode_image(tensor)
                feat = feat.cpu().numpy().flatten()
                feat /= (np.linalg.norm(feat) + 1e-10)
                embeddings.append(feat)

                # 2. Color extraction (average RGB)
                thumb_c = img_rgb.resize((1, 1))
                c_feat = np.array(thumb_c.getpixel((0,0)), dtype=float)
                c_feat /= (np.linalg.norm(c_feat) + 1e-10)
                color_features.append(c_feat)

                # 3. Visual Hash (16x16 layout)
                thumb_h = img_rgb.resize((16, 16)).convert("L")
                h_feat = np.array(thumb_h).flatten().astype(float)
                h_feat /= (np.linalg.norm(h_feat) + 1e-10)
                hash_features.append(h_feat)

                # 4. Temporal
                dt = get_file_datetime(fpath)
                timestamps.append(dt.timestamp())

                # 5. Filename
                stems.append(fpath.stem)
                valid_paths.append(fpath)

        except Exception as e:
            log.warning(f"Failed to process {fpath}: {e}")

    if not embeddings:
        return None

    # stack and scale
    visual_matrix = np.vstack(embeddings)
    color_matrix = np.vstack(color_features)
    hash_matrix = np.vstack(hash_features)
    
    ts_array = np.array(timestamps).reshape(-1, 1)
    ts_scaled = StandardScaler().fit_transform(ts_array)

    # Filename features
    vectorizer = TfidfVectorizer(analyzer='char', ngram_range=(2, 4))
    tfidf_matrix = vectorizer.fit_transform(stems)
    n_feats = tfidf_matrix.shape[1]
    n_comps = min(8, n_feats - 1) if n_feats > 1 else 0
    if n_comps > 0:
        svd = TruncatedSVD(n_components=n_comps)
        fn_features = svd.fit_transform(tfidf_matrix)
        fn_norms = np.linalg.norm(fn_features, axis=1, keepdims=True) + 1e-10
        fn_features /= fn_norms
    else:
        fn_features = np.zeros((len(valid_paths), 1))

    return {
        "paths": [str(p) for p in valid_paths],
        "visual": visual_matrix,
        "color": color_matrix,
        "hash": hash_matrix,
        "temporal": ts_scaled,
        "filename": fn_features
    }

def run_diagnostic(folder_path: Path, output_json: Optional[Path], temporal_weight=0.3, filename_weight=0.0, color_weight=0.0, hash_weight=0.0):
    data = get_features(folder_path)
    if not data:
        return

    # Create combined matrix (matching media_organizer.py logic)
    features = [
        data["visual"],
        temporal_weight * data["temporal"],
        color_weight * data["color"],
        hash_weight * data["hash"],
        filename_weight * data["filename"]
    ]
    combined = np.hstack(features)

    # Calculate distances
    from sklearn.metrics import pairwise_distances
    dist_matrix = pairwise_distances(combined, metric='euclidean')
    
    # Stats
    n = len(data["paths"])
    if n > 1:
        # Lower triangle only for pairwise stats (excluding diagonal zeros)
        tri_indices = np.triu_indices(n, k=1)
        flat_dists = dist_matrix[tri_indices]
        
        max_dist = float(np.max(flat_dists))
        mean_dist = float(np.mean(flat_dists))
        median_dist = float(np.median(flat_dists))
        min_dist = float(np.min(flat_dists))
    else:
        max_dist = mean_dist = median_dist = min_dist = 0.0

    # Centroid distance
    centroid = np.mean(combined, axis=0)
    dists_to_centroid = np.linalg.norm(combined - centroid, axis=1)
    max_centroid_dist = float(np.max(dists_to_centroid))
    mean_centroid_dist = float(np.mean(dists_to_centroid))

    # Density (N-th neighbor)
    if n > 1:
        sorted_dists = np.sort(dist_matrix, axis=1)
        nn1 = float(np.mean(sorted_dists[:, 1])) if n > 1 else 0
        nn3 = float(np.mean(sorted_dists[:, min(3, n-1)])) if n > 3 else nn1
    else:
        nn1 = nn3 = 0.0

    report = {
        "folder": str(folder_path),
        "timestamp": datetime.now().isoformat(),
        "item_count": n,
        "weights used": {
            "temporal": temporal_weight,
            "filename": filename_weight,
            "color": color_weight,
            "hash": hash_weight
        },
        "pairwise_stats": {
            "max": round(max_dist, 4),
            "mean": round(mean_dist, 4),
            "median": round(median_dist, 4),
            "min": round(min_dist, 4)
        },
        "centroid_stats": {
            "max_dist": round(max_centroid_dist, 4),
            "mean_dist": round(mean_centroid_dist, 4)
        },
        "density": {
            "avg_nn1": round(nn1, 4),
            "avg_nn3": round(nn3, 4)
        },
        "recommendations": {
            "suggested_epsilon": round(max_dist * 1.05, 4) if n > 1 else 0.1,
            "suggested_min_samples": max(2, min(5, n // 2)) if n > 1 else 1,
            "notes": "If pairwise MAX is high (>0.5), your group is diverse. Increase epsilon. If NN1 is high, group is sparse."
        }
    }

    # Print Report
    print("\n" + "="*40)
    print("      DIAGNOSTIC SPREAD REPORT")
    print("="*40)
    print(f"Folder: {report['folder']}")
    print(f"Items:  {report['item_count']}")
    print("-" * 20)
    print(f"Pairwise MAX (Diameter):  {report['pairwise_stats']['max']:.4f}")
    print(f"Pairwise MEAN:            {report['pairwise_stats']['mean']:.4f}")
    print(f"Centroid MAX Dist:        {report['centroid_stats']['max_dist']:.4f}")
    print(f"NN1 (Density):            {report['density']['avg_nn1']:.4f}")
    print("-" * 20)
    print(f"SUGGESTED EPSILON:       {report['recommendations']['suggested_epsilon']:.4f}")
    print(f"SUGGESTED MIN_SAMPLES:   {report['recommendations']['suggested_min_samples']}")
    print("="*40 + "\n")

    if output_json:
        with open(output_json, "w") as f:
            json.dump(report, f, indent=4)
        log.info(f"Report saved to {output_json}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Analyze spread of images in a folder.")
    parser.add_argument("--folder", required=True, help="Path to folder of grouped images")
    parser.add_argument("--output", help="Path to save JSON report")
    parser.add_argument("--temporal-weight", type=float, default=0.3)
    parser.add_argument("--filename-weight", type=float, default=0.0)
    parser.add_argument("--color-weight", type=float, default=0.0)
    parser.add_argument("--hash-weight", type=float, default=0.0)
    
    args = parser.parse_args()
    folder = Path(args.folder)
    if not folder.is_dir():
        log.error(f"Not a directory: {folder}")
        sys.exit(1)

    run_diagnostic(
        folder, 
        Path(args.output) if args.output else None,
        temporal_weight=args.temporal_weight,
        filename_weight=args.filename_weight,
        color_weight=args.color_weight,
        hash_weight=args.hash_weight
    )
