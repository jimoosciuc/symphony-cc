"""Allow ``python -m symphony`` to invoke the CLI.

Routes through :func:`symphony.main.main` (not directly through
:mod:`symphony.cli`) so that any orchestrator startup wiring added to
``main.py`` later — config load, signal handlers, artifact dirs — is on
the path for both ``python -m symphony`` and the ``symphony`` console
script.
"""

from symphony.main import main

if __name__ == "__main__":
    raise SystemExit(main())
