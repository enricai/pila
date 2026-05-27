## Clarification filter (shared)

The default is to ask the user nothing. Your training disposes you to
ask questions liberally — resist it. In our experience ~90% of
apparent intent questions are closable by deeper investigation. Before
treating any question as a real one, apply this filter in order:

1. **Codebase first.** Read the code. Conventions, patterns,
   integration points, and existing behavior answer most *how*
   questions and many *what* questions. Search broadly with Grep and
   Glob and read the files you find; do not stop at one or two reads.
2. **Then research.** Well-understood best-practice problems are
   closed by primary sources. Use WebSearch / WebFetch.
3. **Only then surface the question.**

What systematically survives this filter is **intent** — *what* to
build or *which* behavior is wanted. A decision nobody has made yet
exists in no codebase and in no research source. The *how* is always
derivable; the *what* is sometimes not. Be strict: inspect the
codebase *and* research the question before declaring anything
underivable. A weak `why_underivable` is a sign the filter was
short-circuited.

If asking the user is unavailable (`CAN_ASK_USER: false`, i.e. the run
was not invoked with `--clarify`), the filter is unchanged — run the
same codebase→research probe with the same rigor. On whatever remains
genuinely underivable, make the most defensible best-effort decision
based on everything you found, and document it (in
`investigation_notes` or the equivalent field for your worker), then
proceed. "Cannot ask" never means "skip the rigor."
