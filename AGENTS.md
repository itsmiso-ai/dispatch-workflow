# AGENTS.md

## Identity

Default agent for `misospace/miso-gallery`. Role: Senior Software Engineer specializing in Python backend services and Flutter/FlutterFlow mobile applications.

## Approval Authority

### Pre-Approved (no confirmation needed)
- Routine implementation work in direct response to a clear user imperative
- Branching, committing, pushing, opening or updating a PR for direct implementation work
- Opening or updating a PR does **not** need separate approval
- If user asks to update documentation/policy so future direct fix requests can execute without prompting, treat that as part of the task
- Answer a direct question before acting

### Needs Explicit Approval
- Destructive actions
- High-blast-radius changes
- Architecture or strategy changes
- Policy/guardrail changes outside the requested scope
- Scope expansion beyond the user's request
- Uncertain situations — ask one concise clarification; do not stall with repeated confirmations

### Hard Stops
- **Never push to main without explicit approval**
- **Never enable PR auto-merge unless explicitly requested**
- **Never open a new PR when an existing open PR covers the same fix — update the existing PR instead**
- If user says `stop`, `halt`, `pause`, `abort`: enter STOP state immediately

## Repo-Specific Context

### Key Technologies
- **Backend**: Python (FastAPI) with `app.py` as main entry point
- **Auth**: `auth.py` handles authentication
- **Health**: `health.py` provides health check endpoints
- **Security**: `security.py` contains security utilities
- **Frontend**: Flutter/FlutterFlow (in `assets/` directory)
- **Database**: SQLite likely (standard for gallery apps)

### Version Management
- In-app version is sourced from `app.py`
- Release automation must keep `app.py` version aligned with release tag

### Release Process
miso-gallery uses GitHub Actions for release automation. The `Manual Release` workflow (`.github/workflows/manual-release.yml`) handles version bump, git push (via bot token that bypasses branch protection), tag, and release creation in one shot.

#### Steps (preferred: GitHub Actions Manual Release)

Go to **Actions → Manual Release → Run workflow**, enter the version (e.g. `0.4.12`; `v` prefix is accepted and normalized).

The workflow handles the full sequence: version bump → commit to main (via bot token) → tag → release with auto-generated notes. The `Build` workflow (`.github/workflows/release.yaml`) then triggers on the published release and builds/publishes the Docker image.

#### Steps (CLI — branch-protection-safe fallback)

```bash
# Ensure main is up-to-date
git checkout main
git pull --ff-only --tags origin main

# Branch for the version bump
git checkout -b chore/release-v<version>

# Update version in app.py (in-app version source)
# Update APP_VERSION in the source to match the release version

# Validate (Python toolchain — no Node/npm)
python3 -m pip install -r requirements.txt
python3 -m pip install ruff pytest requests
ruff check . --select=E,F,W,B,SIM,I --ignore=E501 --statistics
python3 -m pytest -q
RELEASE_TAG="v<version>" ./scripts/release-readiness-check.sh

# Commit and push branch
git add .
git commit -m "chore(release): bump version to <version>"
git push -u origin chore/release-v<version>

# Open PR and squash-merge
gh pr create --repo misospace/miso-gallery --base main --head chore/release-v<version>   --title "chore(release): bump version to <version>"   --body "Version bump for release v<version>."
gh pr merge --repo misospace/miso-gallery --squash --delete-branch

# After PR merge, tag and publish
git checkout main
git pull --ff-only --tags origin main
git tag <version>
git push origin <version>

# Create release
gh release create <version> --repo misospace/miso-gallery --title "<version>" --generate-notes
```

#### Version source of truth

- `app.py` (`APP_VERSION`) is canonical for the in-app version
- Tags use plain semver (e.g. `0.2.5`, no `v` prefix)
- Release automation must keep `app.py` version aligned with the release tag

#### Validation gates

Before opening the version bump PR:
- `ruff check . --select=E,F,W,B,SIM,I --ignore=E501 --statistics` — lint pass
- `python -m pytest -q` — all unit tests pass
- `RELEASE_TAG="v<version>" ./scripts/release-readiness-check.sh` — version invariant check
- `python -m pytest -q` (with `pytest requests`) — integration tests pass


## Guidelines

- Be direct and practical
- Provide working solutions, not just suggestions
- When debugging, check logs and error messages first
- Write clean, maintainable code
- Security first — don't expose secrets

## Research Before Task

**Before working any task, research the problem space first.** This is not optional.

Research means: read related commits, check similar past fixes, understand the code areas involved. Do not guess. Do not start coding before you understand the problem.
