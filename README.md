# Media Organizer AI 🧠📸 — v0.3.0 (Commit from Log Update)

A sophisticated, content-aware media management suite that uses CLIP embeddings, Perceptual Hashing, and HDBSCAN clustering to recursively organize massive image and video collections.

---

## 🚀 The Five-Phase Pipeline

The tool operates in a strictly ordered pipeline to ensure maximum data integrity and organization accuracy.

### 1. Phase 01: Video Sorting & Orientation
- **Video Sorting**: Automatically detects orientation (Horizontal/Vertical) and organizes clips accordingly.
- **Perceptual Video Deduplication**: A advanced "Tri-Path" filter to find visually identical videos across different resolutions and bitrates.
- **Image De-duplication**: MD5-based exact match detection for images.
- **AI Clustering**: Content-aware grouping of images using CLIP embeddings and HDBSCAN.

---

## 🎥 Perceptual Video Deduplication 

The Media Organizer now features a robust perceptual deduplication engine for videos. This goes beyond simple file-size or binary matching.

### How it Works:
1. **Level 1 (Binary)**: Checks MD5 hashes for exact bit-for-bit copies.
2. **Level 2 (Squint)**: Extracts a frame at the 20% mark, resizes it to 64x64, and generates a perceptual hash (pHash). Perfect for catching resized 4K/720p versions.
3. **Level 3 (Deep)**: Uses the `videohash` library to generate a 64-bit temporal signature, identifying matches even across transcodes.

### Tips & Gotchas:
- **Processing Time**: Deep video scans (Level 3) are CPU-intensive and can take significantly longer than MD5 checks. Use the `enable_deep_video_scan` toggle cautiously.
- **Review Queue**: Any potential matches found at Level 2 or 3 are moved to `Organized/duplicates/review_needed/` instead of the main duplicates folder. Refer to the `Review_Needed` column in your migration log.
- **FFmpeg**: Ensure FFmpeg is installed and accessible in your system's PATH.

---

## ⚙️ Core Parameters

### 2. Phase 02: Binary De-duplication
- **Logic**: Performs a rapid MD5 byte-by-byte hash of every file.
- **Deduplication Strategy**: 
    - **Alphabetical (Legacy)**: The first file A-Z is kept as the original.
    - **Path Depth (Context-Rich)**: Files in deeper subfolders are prioritized as originals. This prevents "stranding" a file in a generic root folder when a more descriptive version exists.
- **Result**: Identifies exact bit-for-bit duplicates. The "Original" is kept; all others are moved to a `duplicates/` directory.
- **Benefit**: Immediate storage reduction and noise cleanup before AI processing.

### 3. Phase 03: AI & Spatiotemporal Clustering
- **Logic**: The "Brain" of the operation. Encodes files into a multi-dimensional "feature space" using:
    - **Visual (CLIP)**: Semantic understanding (e.g., "beaches" vs. "documents").
    - **Color (CIELAB)**: Normalized 16x16 grid "vibe" mapping.
    - **Temporal (EXIF)**: Chronological proximity.
    - **Perceptual (pHash)**: Visual similarity of near-duplicate burst shots.
- **Result**: Groups similar items into `group_XXX/` folders and places outliers in `unsorted/`.

### 4. Phase 04: Contextual Naming & Attribution
- **Logic**: Assigns meaningful names based on the original source folder's name.
- **Smart Flattening**: If enabled, the system uses **Name Sequencing** patterns (like `Vacation_001`, `Vacation_002`) to unify related files even if AI clustering is disabled.
- **Heuristic Sensitivity**: Users can tune the `Min Group Items` and `Name Pattern Sensitivity` to control how strictly the system matches sequential filename patterns.
- **Result**: Renames files using a sequential but descriptive schema: `[OriginalParentName]_[Counter].jpg`.
- **Conflict Prevention**: Uses path-based hashing to ensure no two files ever collide.

### 5. Phase 05: Audit & Logging
- **Logic**: Records every single file movement into a CSV database.
- **Result**: Generates a `migration_log.csv` containing hashes, cluster IDs, confidence scores, and reasons for every move. This CSV is the data source for the **Visualizer UI**.

---

## ⚡ Commit from Log — Zero-Rescan Execution

After running a simulation (dry-run), you can **commit the results directly** without re-running the entire AI pipeline.

### How it Works:
1. **Simulate** your organization with a dry-run.
2. **Review** the results in the Visualizer dashboard.
3. **Commit** by clicking "Execute Log" — the engine reads the CSV, validates each file's MD5 hash, and performs the move/copy operations.

### Safety Features:
- **MD5 Integrity Gate**: Every file is hash-verified before it is moved. Modified or deleted files are automatically skipped.
- **Status Tracking**: Each entry in the committed log is tagged with `[COMMITTED]`, `[SKIPPED:MISSING]`, `[SKIPPED:HASH_MISMATCH]`, or `[SKIPPED:EXISTS]`.
- **Pre-flight Check**: The dashboard shows a confirmation modal with operation count and action mode before executing.

### CLI Usage:
```bash
# Step 1: Simulate
python media_organizer.py --root "E:/My/Media" --dry-run --enable-deduplication

# Step 2: Commit the simulation log
python media_organizer.py --commit-log logs/My_Media_04.02.2026_15.30.csv
```

---

## 🎛️ AI Fine-Tuning Guide

Fine-tuning is the key to mastering your organization. Use the **Tuning Lab** in the Visualizer to experiment.

- **v0.2.2**: "Smart Deployment Update" — Added Literal Root organization, Flat Layout toggle, and UI Structure Preview icons. Fixed root-file flattening discrepancies.
- **v0.2.1**: "Sensitivity Update" — Connected Tuning Lab sliders to the backend. Added Jaccard similarity and Min-Cluster-Size overrides.

### 🏠 Clustering Strategy
- **Individual Mode**: Every image is judged on its own. Best for messy, mixed-source directories.
- **Folder Mode**: Aggregates the "mean embedding" of entire subfolders. Use this if you want to find whole backup folders that are visually similar to one another.

### ⚖️ Feature Weights (Setting the "Flavor")
These settings control the **Relative Importance** of various factors in the distance matrix:

- **Visual Similarity (AI - 1.0 default)**: The semantic weight. Increase this to group by "Subject" (e.g., "all photos of my cat").
- **Color Influence (0.3 default)**: The "Vibe" weight. Increase this to group by environment or lighting (e.g., "all sunset photos" or "all studio shots").
- **Temporal Weight (0.3 default)**: The "Time" weight. Critical for event-based sorting. Increase this to prevent photos from different years being grouped just because they were taken at the same place.
- **Visual Hash Weight (0.0 default)**: The "Burst" weight. Focuses on identical pixel structures. Ideal for grouping rapidly fired burst shots into tight groups.

### 🎯 Group Stringency
- **Similarity Sensitivity**: A 0.0 to 1.0 slider.
    - **0.95+**: Extremely strict. Only identical or near-identical images group together.
    - **0.50**: Reasonable semantic grouping.
    - **0.20-**: Very broad. Will group "any photo taken outdoors" into one massive cluster.
- **Min Cluster Size (3 default)**: How many similar photos it takes to form a "group". Set to 2-3 for personal collections; set higher (8+) for large event coverage to hide stray noise.

### 🧬 Advanced: HDBSCAN Clustering Logic
The underlying clustering engine uses **HDBSCAN** (Hierarchical Density-Based Spatial Clustering of Applications with Noise). Mastering these three variables is the key to perfect groups:

- **Min Samples (0 default)**: 
    - **How it works**: Controls how "conservative" the clustering is. A higher value means an image must have more immediate neighbors to be considered a 'core' member of a group.
    - **Tuning**: Set to **0** for automatic balancing. Set higher (e.g., 5) to radically reduce false positives at the cost of moving more images to `unsorted/`.
- **Epsilon (Cluster Merge Threshold)**:
    - **How it works**: A distance-based "safety net" that merges nearby clusters found by the hierarchical scan.
    - **Interaction**: This is the inverse of the **Similarity Sensitivity** slider. If Sensitivity is 0.95, Epsilon is 0.05. Larger Epsilon values (0.2+) will merge broadly similar groups into one.
- **Cluster Selection Method**:
    - **EOM (Excess of Mass)**: The default. It looks for the most "stable" clusters over the entire hierarchy. It produces fewer, larger, and more global groups.
    - **Leaf**: A more "local" approach. It selects the clusters at the bottom (leaves) of the tree. Use this if you want many tiny, extremely tight groups (e.g., separating two very similar burst shots).

---

## ⚡ High-Performance Feature Cache
Because AI embedding generation (CLIP) and spatial color signature extraction are computationally expensive, the organizer implements a persistent JSON-based caching layer.

- **What is cached?**: CLIP visual embeddings, CIELAB color signatures, and perceptual hashes.
- **Storage Location**: A hidden file named `.organizer_cache.json` is created in your **source (root) directory**.
- **Indexing**: Features are indexed by the file's **MD5 checksum**. If you rename a file, the cache remains valid; if you modify the image's pixels, the MD5 changes and the AI will re-analyze it.
- **Versioning**: The cache includes a `version` field (currently **1.3**). If the underlying AI model or extraction logic changes, the cache will automatically clear itself to prevent stale data from corrupting your results.

> [!TIP]
> **Performance Tip:** Transitioning from "off" to "on" for the Feature Cache can reduce re-run times from minutes to milliseconds, enabling near-instant trial-and-error in the **Tuning Lab**.

---

## 🧬 Setting Interactions (The "Gotchas")

| Setting A | Setting B | Interaction Result |
| :--- | :--- | :--- |
| **Temporal Weight (High)** | **Visual Weight (Low)** | Files will group strictly by day/hour, ignoring what is actually in the picture. |
| **Color Weight (High)** | **Spatial Grid Size (64x64)** | Massive performance hit. The "vibe" calculation becomes too granular and can cause fragmentation of clusters. |
| **Individual Mode** | **Min Cluster Size (High)** | Nearly everything will end up in `unsorted/` unless you have massive burst collections. |
| **Folder Mode** | **Min Cluster Size (High)** | Will only group subfolders that are exceptionally similar to one another. |
| **Min Samples (High)** | **Epsilon (Low/High)** | Forces extremely strict boundaries. Groups will be perfect but many more files will be rejected as noise. |

---

## 🛠️ Usage & Local Setup

### Installation
```bash
pip install -r requirements.txt
# Requires: torch, clip, Pillow, scikit-learn, hdbscan, tqdm
```

### Running the Organizer (CLI)
```bash
python media_organizer.py --root "E:/My/Media" --dry-run
```

### Navigating the Visualizer (Web UI)
1. Run `python visualize_helper.py`.
2. Open `http://localhost:8000/visualizer.html` in your browser.
3. Use the **Tuning Lab** to adjust parameters:
    - **Quick Actions**: The [Simulate][Force Scan] / [Execute Log][Cleanup] grid provides rapid access to the most common tasks.
    - **Tooltips**: Hover over the `(i)` info icon next to the "Source Folder" for context-sensitive help.
4. Click **"Simulate Scan"** to preview groups instantly.
5. Load a simulation log and click **"Execute Log"** to commit without re-scanning.
6. Use **"Cloud Explorer"** to fly through your data clusters in a force-directed graph.

---

## 📅 Versioning & Patch History
See [Patch Notes.md](file:///e:/image%20sorter/Patch%20Notes.md) for a detailed history of changes, including the v0.2.0 UI Overhaul and the v0.1.0 Perceptual Video Deduplication system.


---

> [!TIP]
> **Pro Tip:** Always enable the **Feature Cache**. Once AI embeddings are generated, clustering runs in milliseconds rather than minutes, allowing you to iterate on weights instantly.

> [!WARNING]
> **Legacy Logs:** The UI has been updated to use `Match_Confidence`. Log files generated before March 29th, 2026, may show "N/A" for confidence scores until a fresh organized pass is performed.

> [!NOTE]
> **Commit from Log:** You can now skip the full AI pipeline entirely. Run a dry-run, review in the Visualizer, then click "Execute Log" or use `--commit-log` from the CLI to commit instantly with MD5 verification.
