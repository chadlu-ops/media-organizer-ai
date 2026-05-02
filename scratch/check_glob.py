import os
from pathlib import Path

# Test how glob casing works
base_path = Path("G:/Pictures/EVE").resolve()
files = list(base_path.glob("*"))
if files:
    print(f"Sample from glob: {str(files[0])}")
    print(f"p_str would be: {str(files[0]).replace('\\\\', '/')}")
