import os
import time

start = time.time()
count = 0
for root, dirs, files in os.walk("G:/Pictures"):
    for file in files:
        if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.mov', '.webm', '.mkv', '.m4v', '.avi')):
            count += 1
end = time.time()
print(f"os.walk took {end - start:.2f} seconds to find {count} files.")
