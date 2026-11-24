# Repository guidelines

## Layout and checks

- `backend/` and `frontend/` each hold their own code and tests. Backend tests live in `backend/tests/`; frontend tests sit beside the source as `*.test.*`.
- `make test` runs every check: tests, lint, type checks, and build. `TEST_SUITE=backend|frontend` runs one side; `make help` lists the other options.
- Run the affected suite while working and the full `make test` before each commit.

## Ask first

- Merging into `main`.
- Dropping tables, columns, or stored files.
- Running `make init`, or discarding uncommitted changes.
- Adding, changing, or removing tests, except a bug fix's regression test.

## Testing

Tests exist so code can change with confidence; the suite must not grow into a second codebase. A test earns its place when it names a failure that would hurt and that I would otherwise miss: wrong results, data loss, leaked access, duplicate execution. Breakage I would notice on first use needs no test. When unsure, leave it out.

- Test observable behavior at the cheapest layer that catches the failure. Frontend tests cover interaction and state, not rendering.
- Reuse the existing fixtures and helpers. Tests use temporary data and never reach real user data or external services.
- A bug fix gets one regression test that fails without the fix.
- For anything else, implement first, then list the tests you plan to add, change, or delete, one line each naming the failure it guards, and wait for my go-ahead. A simple feature usually needs 1–3.
- When behavior changes, update or delete the old tests instead of adding new ones beside them.
- For feature work, report `feature +X lines, tests +Y lines`. If Y exceeds 1.5 × X, trim duplicated setup or say why.

## Documentation

- Maintained guides live in `docs/`, one per topic. Extend an existing guide before adding a new one, and update the guide and links to it in the same commit as the behavior change.
- Working files go in the Git-ignored `.local-docs/<YYYY-MM-DD>-<task-name>/`, one folder per task, created when the task starts: plans, notes, logs, test output, screenshots, temporary scripts, PR drafts.
- Keep those files and links to them out of tracked files and commit messages. If one holds a lasting rule, add that rule to the relevant guide.

## Removing code

- Code needs a live caller in the same change: UI, production code, a script or CI job, or a documented operator procedure. Tests do not count as callers.
- When a feature or the last caller of something goes away, delete everything that only served it, including its tests, config, and docs, then search again until nothing new turns up. Delete rather than stub or comment out.
- Never delete existing migrations.

## Commits

- Work on a task branch. Merge into `main` only with a merge commit (`git merge --no-ff`), never fast-forward or squash.
- Commit each completed step on its own, with `make test` passing and no unrelated changes.
- Start each commit subject with `feat:`, `fix:`, `docs:`, `style:`, `refactor:`, `test:`, or `chore:`, chosen by the commit's main purpose, then a short imperative summary. A feature committed with its tests is `feat:`.
- Commit messages have no `Co-Authored-By` lines.
