#!/usr/bin/env bash
# Check for accidental commits of secrets in source code.
# Exits 0 if clean, 1 if suspicious patterns found.
# Used by GitLab CI (sanity/security stage).

set -e

ROOT="${1:-.}"
cd "$ROOT"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "Not a git repo, skipping secret check."
    exit 0
fi

# git grep exits 0 if match found, 1 if no match
if git grep -n -E -i \
    'api_key\s*=\s*["\x27][^"\x27\s]{12,}["\x27]|'\
    'secret_key\s*=\s*["\x27][^"\x27\s]{12,}["\x27]|'\
    'access_token\s*=\s*["\x27][^"\x27\s]{12,}["\x27]|'\
    'AKIA[0-9A-Z]{16}' \
    -- '*.py' '*.yml' '*.yaml' '*.json' 2>/dev/null; then
    echo ""
    echo "SECRET CHECK FAILED: Suspicious pattern found. Review matches above."
    exit 1
fi

echo "No suspicious secrets detected."
exit 0
