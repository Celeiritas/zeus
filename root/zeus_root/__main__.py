"""`python3 -I -m zeus_root <command>` (and the installed zipapp, Z5): see cli.py."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
