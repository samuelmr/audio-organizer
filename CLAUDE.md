# Guidelines for Claude

audio-organizer (`aorg`) maintains a music collection: catalog, metadata with
provenance, reviewable changesets, playlists and S3 sync. README.md describes usage and
the module layout.

## Branches and worktrees

Several chats may work on this repository at the same time. To keep them apart:

- **Before changing any tracked file, enter a new worktree** with the EnterWorktree tool,
  named after the work (e.g. `playlists`, `fix-sync-nfc`). It is created in
  `.claude/worktrees/<name>` on a new branch from the local `main`
  (`worktree.baseRef: head` in `.claude/settings.json`). Questions, analysis and reviews
  that change nothing need no worktree.
- **Never switch branches or commit in the main checkout.** It stays on `main` and is
  shared by every chat. The only thing done there is merging, when the user asks.
- Commit on the branch at coherent points, with tests and lint passing.
- **Merging into `main` only when the user asks**: in the worktree, `git rebase main` and
  rerun the tests; then in the main checkout `git merge --ff-only <branch>`. Other chats
  may have merged in the meantime; resolve conflicts on the branch, and ask the user
  when a resolution isn't obvious. Afterwards remove the worktree and delete the branch
  (ExitWorktree with `remove`, or `git worktree remove` and `git branch -d`).
- Never push unless the user asks.
- Each worktree has its own `.venv`; `uv run` creates it on first use.

## Personal instructions

Contributors can keep their own instructions (their music library, plans, credentials
to stay away from) in `CLAUDE.local.md` in the repository root. It is gitignored, and
`.worktreeinclude` copies it into new worktrees. It may import a file kept elsewhere,
e.g. `@~/notes/audio-organizer.md`.

## Real music and services

- Never change a real music library while developing: try changes on copies in a
  scratch folder, and point `AORG_DB` at a scratch catalog instead of the default one
  (`~/.local/share/audio-organizer/catalog.db`).
- Never sync to a real bucket or other external service without asking. For end-to-end
  checks run a local fake S3: `uvx --from 'moto[server]' moto_server -p 5055`.

## Code

- Python 3.13 with uv: `uv sync`, `uv run pytest`, `uv run ruff format`,
  `uv run ruff check`. Run tests and lint before every commit. If `VIRTUAL_ENV` is set
  from another project, `unset VIRTUAL_ENV` first.
- Keep logic in the `audio_organizer` core; `cli.py` stays a thin layer, as a web UI and
  a hosted service will share the core.
- Principles: tags are evidence, not truth (values stored per source in `FieldValue`);
  no file is changed without a reviewable, undoable changeset; scans and syncs are
  incremental and batched for libraries of hundreds of thousands of tracks; save
  bandwidth (server-side copies, no needless uploads).
- Match the surrounding style: type hints, docstrings that explain why, line length 100.
- Tests generate audio with ffmpeg (`copy_audio` in `tests/conftest.py`) and fake S3
  with moto. Add tests with new behaviour; path parser cases come from real file names.
- New tables are created by `create_all`; before changing existing tables, set up
  Alembic migrations.
- README.md is for users first (installation, configuration, usage), contributors
  second. Update it when commands change.
- Commit messages: a short imperative summary, then a body explaining what and why.
