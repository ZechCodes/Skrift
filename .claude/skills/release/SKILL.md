---
name: release
description: "Bump version, commit, create PR, merge, and publish a GitHub release. Use when the user wants to cut a release."
argument-hint: "[version-bump-type]"
allowed-tools: Bash(git *), Bash(gh *)
---

# Release Workflow

You are performing a release for this project. Follow each phase in order. **Do not skip phases or combine them without user approval.**

## Arguments

`$ARGUMENTS` may contain a version bump hint. Interpret it as follows:

| Argument | Meaning |
|----------|---------|
| *(empty)* | Bump the least significant segment (e.g. `0.1.0a1` -> `0.1.0a2`, `0.1.0` -> `0.1.1`) |
| `patch` | Bump patch (e.g. `0.1.0` -> `0.1.1`) |
| `minor` | Bump minor (e.g. `0.1.0` -> `0.2.0`) |
| `major` | Bump major (e.g. `0.1.0` -> `1.0.0`) |
| `alpha` | Bump alpha pre-release (e.g. `0.1.0a1` -> `0.1.0a2`) |
| `beta` | Promote to or bump beta (e.g. `0.1.0a3` -> `0.1.0b1`, `0.1.0b1` -> `0.1.0b2`) |
| `rc` | Promote to or bump rc (e.g. `0.1.0b2` -> `0.1.0rc1`) |
| `stable` | Drop pre-release suffix (e.g. `0.1.0rc1` -> `0.1.0`) |
| An explicit version like `1.2.3` | Use that version exactly |

If no argument is given, default to bumping the least significant segment.

---

## Phase 0 — Preflight

1. Run `git status` (never use `-uall`) and `git diff` to check for uncommitted changes.
2. If there are **uncommitted changes unrelated to this release**, stop and ask the user what to do — they may want to commit or stash first.
3. Identify the **main/default branch** (usually `main` or `master`). Confirm you are on it or on a branch that will PR into it.
4. Run `gh pr list --state merged --limit 1` and `gh release list --limit 1` to find the last release tag and last merged PR for context.

## Phase 1 — Gather Changes

1. Find the latest release tag from Phase 0.
2. Run `git log <last-tag>..HEAD --oneline` to collect all commits since that tag.
3. Categorise commits into: **New Features**, **Bug Fixes**, **Improvements**, **Breaking Changes**, **Other**. Ignore merge commits and version-bump-only commits.
4. If there are no meaningful changes since the last release, tell the user and stop.

## Phase 2 — Bump Version

1. Read `pyproject.toml` and find the current `version` field.
2. Compute the new version based on the argument (see table above).
3. **Show the user** the current version, the new version, and a summary of categorised changes. Ask for confirmation before proceeding. If they want a different version, use that instead.
4. Edit `pyproject.toml` with the new version.

## Phase 3 — Commit & Push

1. Create a new branch named `release/v<new-version>` from the current branch.
2. Stage **only** the changed files (version bump + any other release-related edits). Do not use `git add -A`.
3. Commit with a message like:

   ```
   Bump version to <new-version>
   ```

4. Push the branch with `-u`.

## Phase 4 — Pull Request

1. Compose a PR title: short, descriptive (e.g. `Release v<new-version> — <one-line summary>`).
2. Compose a PR body using this template:

   ```markdown
   ## Summary
   <High-level description of what's in this release>

   ## Changes

   ### New Features
   - ...

   ### Bug Fixes
   - ...

   ### Improvements
   - ...

   ### Breaking Changes
   - ...

   ## Release version
   `<old-version>` -> `<new-version>`
   ```

   Omit empty categories. Reference relevant issues/PRs where applicable.

3. Create the PR with `gh pr create`.

## Phase 5 — Merge

**Ask the user for confirmation before merging**, unless they already explicitly told you to merge.

Once confirmed, merge with `gh pr merge <number> --merge`.

Then switch back to the main branch and pull.

## Phase 6 — GitHub Release

**Ask the user for confirmation before creating the release**, unless they already explicitly told you to create one.

1. Create a release with `gh release create v<new-version>` using a body that mirrors the PR's change categories but written for end-users (concise, no internal jargon).
2. Print the release URL for the user.

---

## Important Rules

- **Atomicity**: The version bump and any release-related changes must be in a single commit.
- **User control**: Always confirm before merging and before creating a release, unless the user pre-authorized those steps.
- **No surprises**: Show the user exactly what version you'll set and what changes you'll include before doing anything irreversible.
- **Clean state**: After the release, leave the repo on the main branch with a clean working tree.
