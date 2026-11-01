# AI usage disclosure

Tool used: **Claude Code** (Anthropic, Claude Opus model, inside VS Code).
The full prompt history is available on request, and I can walk through it during the interview.

---

## What AI did

- **Wrote the code**: all pipeline modules in `src/` (ingest, validation, features, loading, reconciliation).
- **Wrote the tests**: the 40 automated tests in `tests/`.
- **Wrote the explanations**: explained the problem statement in simple language, explained how
  each part works whenever I asked, and wrote the first drafts of the README, data dictionary and design notes.
- **Proposed technical designs**: for example, using two timestamps (`txn_ts` and `ingested_at`) to
  block late data, file hashing for safe reruns, and "first seen wins" for duplicates.
- **Fixed bugs** that were found while I ran and checked the pipeline.

## What I did

- **Planned the work**
  - Decided to first understand the problem fully before building anything.
  - Decided the build order: a 5,000-row version first, fully tested, then scale to 100,000 rows.
  - Decided the README style and what it should contain, and had it rewritten several times.
- **Approved the tech stack**
  - The AI recommended Python + DuckDB + pandas + pytest. This is already my own data engineering
    stack (I have previously used DuckDB to clean a 77-million-row dataset in another project), so I
    approved it, together with the folder structure, before any code was written.
- **Asked "why this, why not that"**
  - Why the data is split into many files instead of one.
  - Why the pipeline is more complex than I expected, and whether that is what the problem asks for.
  - Whether the README covers everything the problem statement and submission guidelines require.
  - After understanding the answers, I decided to keep the design, because each part maps to a requirement.
- **Checked for bugs and gaps**
  - Asked for an unbiased audit against every point of the problem statement, run from a clean copy of the repository.
  - Questioned results that looked wrong (e.g. why my first run showed `in=0` instead of `in=5000`).
  - Spotted things that would confuse reviewers, such as unmeasured claims in the README, which were removed until measured.
- **Tested everything myself**
  - Ran every command from the README on my machine: data generation, pipeline run, rerun
    (no duplicates), inference run, sample queries and `pytest -v` (40 passed).
  - Ran the pipeline on **100,000 rows** and confirmed it works (4.3 s, all reconciliation checks PASS).
  - Checked which files each run creates or overwrites.
- **Made the creative and design-level decisions**
  - Scope, build order, how the work is presented, and where to simplify.
  - Pushed back on complexity and decided what stays in the solution.
  - Asked for the long, AI-style code comments to be cut down to short, natural ones, so the
    code is easier to read. Only comments were changed, and the tests were re-run to confirm the logic was unchanged.
- **Reviewed the code**
  - Reviewed the code, the outputs, the reports, the README and `.gitignore` (no secrets, no
    virtual environment, no large files pushed).
  - My review was not line by line yet. After submission I will go through every file line by
    line and re-write it myself.

## Bugs found and fixed during review and testing

- The inference run overwrote the training reconciliation report.
- A rerun hid a file that was still quarantined.
- The reconciliation report showed "input rows: 0" after a rerun, which looked like nothing had loaded.
- The 100K command in the README would have overwritten the 5K outputs.
- A query parser broke on its own header comment.
