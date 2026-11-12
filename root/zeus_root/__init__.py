"""zeus-root: the root-side runner and command line for root actions (docs/design.md §16, docs/contract.md §8).

Not part of the `zeus` package and never imports it. On the server it runs as `/usr/bin/python3 -I` from root-owned
files only; it imports the standard library, `cryptography` and itself, nothing else.
"""
__version__ = "0.1.0"
