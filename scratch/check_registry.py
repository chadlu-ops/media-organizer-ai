import json
from pathlib import Path

registry_path = Path("logs/slideshow_registry.json")
if registry_path.exists():
    with open(registry_path, "r", encoding="utf-8") as f:
        registry = json.load(f)
    print(f"Registry has {len(registry)} items.")
    if registry:
        print("Sample path from registry:")
        print(registry[0]["path"])
        
        # Test resolving a path
        sample_path = registry[0]["path"]
        sample_dir = str(Path(sample_path).parent.resolve()).replace('\\', '/')
        print(f"Parent dir resolved: {sample_dir}")
        print(f"Sample path starts with parent dir? {sample_path.startswith(sample_dir)}")
else:
    print("Registry not found.")
