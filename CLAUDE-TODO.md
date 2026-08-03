# CLAUDE-TODO.md

Claude's working list. `TODO.md` is the user's; do not write to it.

What is here: paths that are built and shipping but have not been *watched running*, and quality gaps that are known and not re-measured. The point is to stop me asserting that something works when all I have is that the code exists and the tests pass.

Move an item out when it has been observed live, and say in the commit what was observed.

## Not yet verified live

Watch the next pipeline run rather than assuming these.

- **The QA tool harness and its conditional screenshot** (0026, 0028). A rewrite of a path that *was* working, 134 calls with 81 of 82 posts scored, so the regression risk is real.
- **5 unexplained `400 Bad Request`** responses from llama-server across those 134 QA calls (0026). Cause unknown. Not reproduced since the rewrite, which is not the same as fixed.
- **A writer run producing a multi-question quiz** (0025).
- **Chart, diagram, timeline, glossary and video sections** emitted on a suitable story. The write path was verified 2026-07-19, but not every section type has been seen in output.
- **A pipeline stage running after an agent-driven backend start** (0023).

## Known content-quality gaps

From the 2026-07-17 run. The machinery was fine; these are the model's output. Targeted by the thin-gate, the writer prompts and the qa stage, but **not re-measured since**.

1. Triage approves `write` for stories with too little source text.
2. The writer restates `summary` sentences verbatim as a prose section.
3. The writer sometimes emits funding/DOI boilerplate as a prose section.

Re-measuring means a capped write run (`?limit=2`) and reading the output, not reading the prompts and concluding they look better.
