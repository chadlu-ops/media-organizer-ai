import os
from pathlib import Path

# Test with a lowercase drive letter and different casing
path1 = Path("g:/pictures/eve").resolve()
path2 = Path("G:/Pictures/EVE").resolve()

print(f"Path1 resolved: {str(path1)}")
print(f"Path2 resolved: {str(path2)}")
print(f"Are they exactly equal strings? {str(path1) == str(path2)}")
