#!/usr/bin/env bash
# Block commits that would include sensitive files.

if git diff --cached --name-only \
   | grep -qE '\.(env|key|pem)$|secrets\.json|creds\.md'; then
  echo "BLOCKED: attempt to commit sensitive files" >&2
  exit 2
fi
