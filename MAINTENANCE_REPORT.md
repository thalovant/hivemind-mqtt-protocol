# Maintenance report

## 2026-11-15 - Removed the per-file author/date/motive headers

Rationale: the workspace owner withdrew the per-file author/date/motive header convention (hard rule: no such header anywhere). Removed the header line (and the blank line after it) from every file that carried one, 7 files in this change. No code, behaviour or documentation content changed; only the header lines.

Verification: a case-insensitive `git grep` for the header keyword (the two words "last" and "edit" joined by a space) finds nothing after the change; `git diff --stat` shows deletions only.

Verification (added 2026-11-15): the red e2e checks are not caused by this change. `pytest tests -q` on `dev` and on this branch both give 83 passed, 10 errors; details and root cause in [AUDIT.md](AUDIT.md). The AUDIT.md entry was written by Claude Sonnet 5.5 (Claude Code) from the local reproduction; no human has reviewed it.

### Transparency report

- **AI model**: Claude Sonnet 5.5 (Claude Code).
- **Actions taken**: ran a script that deletes the header line (plain, in `<!-- -->` or `/* */` comments, or marked as a previous header) and the blank line that followed a leading header; edited this report and AUDIT.md.
- **Oversight**: requested by the owner; the diff was reviewed as deletions only; CI runs on the pull request.

## 2026-09-30: license and language metadata

Added a root Apache-2.0 license, documented GitHub's detected languages (Python), and updated repository reference, audit, and navigation files. License basis: Existing [pyproject.toml](pyproject.toml) declaration.

Verification: `python -m pytest tests -q --maxfail=1 --ignore=tests/e2e --ignore=tests/test_integration_live_stack.py` — 80 passed, 1 warning in 1.17s The broader baseline run failed in the installed hivescope integration harness; the unit-only rerun passed.

### Transparency Report

- AI Model: GPT-6 (Codex).
- Actions Taken: inspected GitHub metadata, package declarations, source symbols and tests; added license text and documentation; checked the final diff.
- Oversight: the user requested the update and explicitly selected proprietary terms for repositories with no existing declaration. The assistant performed automated validation; no human line-by-line review is claimed.

Metadata validation: 116 checks passed across the 29 repositories (canonical license text, new documentation links and headers, source citations, and metadata-only change scope).
