---
name: commit
description: Use when committing changes with git. Enforces small per-file commits with an ALL-CAPS verb prefix (ADD, UPDATE, DELETE, FIX, REFACTOR, etc.) and a message of 60 characters max. Trigger on requests to commit, save changes, or create git snapshots.
---

# Commit skill

This skill governs how changes are committed in this repository. Every commit
must be **small**, **per file**, and follow the **VERB + message** format
described below.

## Commit format

```
<VERB> <message>
```

- `<VERB>` is a single imperative verb in **ALL CAPS**. Pick the one that best
  describes the change.
- `<message>` is a short description of what changed in that file.
- The full message **after** the verb (excluding the verb and the single
  separating space) must be **60 characters or fewer**.
- One verb per commit. No body, no footers, no scope/issue tags unless the user
  explicitly asks for them.

### Allowed verbs (non-exhaustive)

| Verb     | Use when                                                |
| -------- | ------------------------------------------------------- |
| ADD      | A new file is created.                                  |
| UPDATE   | An existing file is modified (features, logic, text).  |
| DELETE   | A file is removed.                                      |
| FIX      | A bug is corrected.                                     |
| REFACTOR | Internal restructuring with no behavior change.        |
| RENAME   | A file is renamed or moved.                             |
| FORMAT   | Only whitespace/formatting changes.                     |
| DOCS     | Documentation-only change.                              |
| TEST     | Tests added or modified.                                |
| CHORE    | Build, config, deps, tooling, gitignore, etc.           |
| REVERT   | Reverting a previous change.                            |

If none of the above fit, choose another imperative verb in ALL CAPS. Never use
lowercase or mixed case for the verb.

### Examples

- `ADD user authentication middleware`
- `UPDATE chunking strategy for parent-child docs`
- `DELETE unused telemetry helper module`
- `FIX off-by-one in retrieval top_k slicing`
- `REFACTOR server tool registration into dict`
- `RENAME ingestion.py to ingest.py`
- `DOCS update README quickstart section`
- `CHORE pin qdrant-client to 1.9.0`

## Per-file rule

- **Each commit touches exactly one file.** Stage only one file with
  `git add <path>` before committing. Never use `git add .`, `git add -A`,
  `git add -u`, or stage multiple paths in one commit.
- If multiple files need committing, produce **one commit per file**, in
  sequence. Ask the user whether to proceed with the next file after each
  commit, or commit them all back-to-back if the user requested a batch — but
  never collapse two files into one commit.
- Renames (`RENAME`) count as a single file even though git records a
  delete+add; stage the rename with `git add <old> <new>` (or `git add -A` on
  just those two paths) so git detects it as a rename. This is the only
  exception to the "stage exactly one path" rule.

## Workflow

Before committing, always inspect the working tree:

1. Run `git status` and `git diff` to see what changed. For staged changes use
   `git diff --cached`. For new untracked files, read the file to understand
   the change before describing it.
2. Determine the single file to commit and the verb that matches the change.
3. Compose the message. Count the characters **after** the verb and space;
   trim wording until it is <= 60. Prefer concrete nouns over vague phrases
   ("chunking strategy" not "some changes to the chunking related stuff").
4. Stage exactly that one file: `git add <path>`.
5. Commit: `git commit -m "<VERB> <message>"`. Use the `-m` flag with the full
   message on one line. Do not open an editor.
6. Verify with `git log --oneline -1` and `git show --stat HEAD` that the
   commit contains exactly one file and the message is well-formed.
7. If more files remain, repeat from step 1 for the next file.

## Message length check

Before running `git commit`, verify the message portion (everything after the
verb and the single space) is <= 60 characters. In PowerShell:

```powershell
$msg = "UPDATE chunking strategy for parent-child docs"
$verb, $rest = $msg -split ' ', 2
$rest.Length   # must be <= 60
```

If it exceeds 60, shorten it before committing. Keep the verb as-is; trim only
the message.

## Constraints

- Never commit secrets, `.env`, API keys, or anything in `.gitignore`. If a
  staged file looks like it contains a secret, stop and warn the user.
- Never amend, force-push, or create empty commits unless the user explicitly
  asks.
- Never run `git config` changes or skip hooks.
- Never use `--no-verify`.
- Do not push unless the user explicitly asks to push.
- If a pre-commit hook modifies the file, re-stage the single file and commit
  again with the same message.
