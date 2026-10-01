# Git hooks

Install once per clone: `sh bin/hooks/install.sh` (sets `core.hooksPath`).

- `pre-commit`: when a commit stages `_bibliography/paperpile.bib`, `_data/pubinfo.yml`,
  `_data/oa_links.yml`, `_data/citations.yml` or a file in `assets/img/publication_preview/`,
  runs `uv run --frozen python bin/update_references.py` and stages the regenerated
  `_bibliography/papers.bib`. It is offline; link lookups are done by the
  "Update publications" workflow (or `bin/resolve_links.py`). Skip with `git commit --no-verify`.

The hook is a convenience: the "Update publications" workflow and the "Check publications"
PR gate enforce the same result on GitHub.
