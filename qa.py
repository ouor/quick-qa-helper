"""Run quick-qa straight from a clone without installing: python qa.py "<task>" --app PKG"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quickqa.__main__ import main  # noqa: E402

sys.exit(main())
