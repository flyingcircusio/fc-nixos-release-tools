# Claude Context

## Pre-commit Hooks

This project uses pre-commit hooks that include formatters like `ruff` which may modify files during commit.

When committing changes:
1. If a pre-commit hook fails due to formatting changes (like ruff reformatting), the commit will be rejected
2. The hook will automatically fix the formatting issues
3. You must re-add the modified files and retry the commit
4. This is normal behavior - just retry the commit after the auto-formatting
5. Ensure that the same semantical set of changes is added after the re-format

Example workflow:
```bash
git add file.py
git commit -m "message"  # May fail with formatting changes
git add file.py          # Re-add the auto-formatted file
git commit -m "message"  # Should succeed now
```
