"""
Make `python py/scripts/<anything>.py` work from any shell.

Two things go wrong otherwise, and both produce a bare traceback that says
nothing about the fix:

  1. `py/` is not on sys.path, so `import oncall_agent` fails.
  2. The shell has no venv active, so `import dotenv` fails even though the
     dependency is installed two directories away.

(2) is the one that actually bites: every command in this repo's docs was
written by someone who already had `.venv` activated, so the instructions
silently assume it. Rather than adding "remember to activate the venv" to
every line of the README, this re-executes the script under the project venv
when the current interpreter cannot satisfy the imports.

Import this FIRST, before any third-party import:

    from _bootstrap import bootstrap; bootstrap()
"""
import os
import sys
from pathlib import Path

_PY_DIR = Path(__file__).resolve().parent.parent          # <repo>/py
_REPO = _PY_DIR.parent
# Set on the re-executed process so a broken venv cannot loop forever.
_GUARD = "ONCALL_AGENT_BOOTSTRAPPED"


def _venv_python() -> Path | None:
    for candidate in (_REPO / ".venv" / "bin" / "python",
                      _REPO / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    return None


def bootstrap() -> None:
    if str(_PY_DIR) not in sys.path:
        sys.path.insert(0, str(_PY_DIR))

    try:
        import dotenv  # noqa: F401
        return                                            # deps are present
    except ImportError:
        pass

    venv = _venv_python()
    already_tried = os.environ.get(_GUARD) == "1"
    # Compare sys.prefix, NOT the interpreter path: a venv's bin/python is a
    # symlink to the system interpreter, so resolve()-ing both makes them
    # compare equal and the re-exec silently never happens.
    inside_venv = venv is not None and Path(sys.prefix).resolve() == venv.parent.parent.resolve()
    if venv and not already_tried and not inside_venv:
        os.execve(str(venv), [str(venv), *sys.argv],
                  {**os.environ, _GUARD: "1"})           # replaces this process

    hint = (f"    source {_REPO / '.venv' / 'bin' / 'activate'}"
            if venv else
            f"    python -m venv {_REPO / '.venv'}\n"
            f"    source {_REPO / '.venv' / 'bin' / 'activate'}\n"
            f"    pip install -r {_REPO / 'py' / 'requirements.txt'}")
    raise SystemExit(
        "oncall-agent: dependencies are not importable under "
        f"{sys.executable}.\n\n{hint}\n"
    )
