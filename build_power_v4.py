#!/usr/bin/env python3
"""Power v4 build note.

The supplied Power v4 HTML is a compiled self-contained artifact. The original modular
source fragments (_src_gold.html, etc.) were not supplied, so rebuilding with the older
build.py can overwrite v4-only Command Center and Gold changes.
"""
from pathlib import Path
import shutil, sys
src=Path(__file__).with_name('OCC_Toolkit_Power_v4.html')
out=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).with_name('index.html')
shutil.copyfile(src,out)
print(f'copied {src.name} -> {out}')
