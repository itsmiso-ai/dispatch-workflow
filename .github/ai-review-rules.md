# AI PR Review: miso-gallery

## Security review conventions

miso-gallery serves user-uploaded media. Security-sensitive areas:

- **File serving routes** (`app.py`: `send_from_directory`): sanitize requested paths, block hidden/excluded directories, reject non-media files on public-media-only routes
- **Path traversal**: verify symlink and resolved-path behavior; resolved file targets must not escape the configured data root
- **Uploads** (`app.py`): validate content type, file size, and filename before storage; reject executable content
- **Auth** (`auth.py`): auth bypass routes must be intentionally public and narrowly scoped; OAuth/OIDC callback validation
- **Rate limiting** (`security.py`): endpoint-specific rate limits, Redis-backed state
- **Subprocess execution** (`app.py`: `subprocess` usage): validate any user-influenced arguments, avoid shell injection
- **PIL/Image processing**: validate image dimensions and format before processing; handle `UnidentifiedImageError`

For PRs that touch these areas, call out:
- Is input sanitized before use in filesystem, subprocess, or network operations?
- Are auth bypass routes documented as intentionally public?
- Does the change address all threat cases from the linked issue, or are edge cases documented as out of scope?
- Missing test coverage for file-path or auth edge cases.

## Review tone

- Be direct and practical.
- Flag only real defects, regressions, or meaningful risks as blocking.
- Do not nitpick formatting, naming, or style unless it affects readability or correctness.
- Prefer `approve` or non-blocking comments for PRs that look reasonable overall.
