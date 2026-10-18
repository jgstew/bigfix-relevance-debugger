# Session relevance REST fixtures

Real `POST /api/query` response bodies from a BigFix root server, copied from
bigfix-remote-session-relevance's `tests/fixtures/rest/captured/` (redacted
there). `plural_tuples.json` is its `via_explorer__plural_tuples.body`: the
root server forwarded the query to BigFix Explorer, which reports a fractional
`evaltime_ms`; without Explorer the server reports whole milliseconds.
