# Setting up the reference tools

## Recommended: uv

From the repo root (uses `pyproject.toml` and `uv.lock`):

    uv sync
    uv run python bin/update_references.py --check

## Alternative: conda/mamba

    mamba env create -f reftools/environment.yml
    mamba activate reftools

`bibtexparser` must stay on v1 (`<2`); the scripts use the v1 API. `rendercv` is not needed here (CV rendering uses the root `requirements.txt`).
