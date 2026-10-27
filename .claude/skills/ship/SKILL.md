---
name: ship
description: Commit and push the changes. Use when the user wants to commit and/or push to the remote GitHub repo.
---

# Ship

Verify, commit, and push changes for $ARGUMENTS.

1. Run typecheck.
2. Stage the changes and write a commit message from the actual diff — short imperative subject, body only if the diff needs explaining. Make $ARGUMENTS the scope of the subject; if I didn't give one, read it from the diff.
3. Confirm with the commit hash.
4. Do not write attribution notes to anyone.
