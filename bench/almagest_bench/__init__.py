"""Measures which local model to use, and what it costs to use it.

Its own distribution and its own venv, for the reason `idcodec/` and `mcpserver/`
are: matplotlib, numpy and shelling out to `kubectl` have no business in the API
image. It depends on `almagest-backend` at runtime rather than only in tests,
because scoring must use the real `candidates.compare_raw` and the real promotion
rules -- a benchmark whose notion of "correct" differs from the shipped one is
measuring a system nobody runs.

**It adds no backend routes**, so `mcpserver/coverage.py` is untouched and stays
that way. Benchmark results are files on a laptop, not inventory.

Status: the spine is here (`record`, `cluster`, `corpus`); the runner, the scoring
pass and the charts are not. There is also no corpus yet, which is the blocking
input rather than a missing module -- see `corpus.py` for what a case has to
carry and why `absent` and `truth_source` are the two fields that decide whether
any of this is evidence.
"""
