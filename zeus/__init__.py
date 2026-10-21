"""Zeus is the throne of an Elaia/Argus home setup: he deploys a commit only after the owner approved it, and he can
put the previous approved commit back. He never fetches from anywhere but the allowlisted repositories, never deploys
a branch, never runs a command from a message, a page or a config, and never pushes anywhere.
Python 3 standard library only. See docs/design.md."""
from importlib import resources
from pathlib import Path


def version():
    """The version string: zeus/VERSION inside the built zipapp (scripts/build.sh puts it there), else the repo's VERSION."""
    try:
        return resources.files(__name__).joinpath("VERSION").read_text().strip()
    except (FileNotFoundError, OSError):
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
