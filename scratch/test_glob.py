import time
from pathlib import Path

start = time.time()
base_path = Path("G:/Pictures").resolve()
count = 0
for p in base_path.glob("**/*"):
    if p.is_file():
        count += 1
end = time.time()
print(f"Glob took {end - start:.2f} seconds to find {count} files.")
