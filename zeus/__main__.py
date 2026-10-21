"""python3 -m zeus ... (the zipapp built by scripts/build.sh has the same entry point at its root)."""
import sys

from zeus.cli import main

sys.exit(main())
