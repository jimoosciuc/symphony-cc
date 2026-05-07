"""Allow ``python -m symphony`` to invoke the CLI."""

from symphony.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
