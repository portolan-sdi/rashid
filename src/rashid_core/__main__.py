"""Run the validator without installing the rashid console script."""

from rashid_core.cli import main

if __name__ == "__main__":
    main(prog_name="python -m rashid_core")
