"""Filling in a part's parameters from something other than a human typing them.

Nothing in here writes `parameter_value` directly — enrichment writes
`parameter_value_candidate` and a promotion step decides. `mpn_decoders/` is the
one source in this package that needs no network, no API key and no model, which
is why it is built first.
"""
