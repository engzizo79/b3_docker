#!/bin/bash
# B3 Hive secrets scan — blocks commits containing secrets or wallet data.
# Usage: scripts/scan_secrets.sh [--cached]
#   --cached  scan only git staged files (used by the pre-commit hook)
# Exits 1 (blocking) if a secret-like pattern is found.

set -uo pipefail

MODE="${1:-all}"
FAILED=0

# Files that must NEVER be committed (wallet data, env, node conf)
NEVER_COMMIT_RE='(^|/)(\.env|b3coin\.conf|wallet\.dat|.*\.wallet)$'

# Secret-value patterns in any file (key=value with a real-looking value)
SECRET_VALUE_RE='(RPC_PASSWORD|SESSION_SECRET|TOTP_ENCRYPTION_KEY|UI_PASSWORD|WEBHOOK_URL|rpcauth|rpcpassword|rpcuser)[[:space:]]*=[[:space:]]*[^[:space:]]+'

# Allowed placeholders
PLACEHOLDER_RE='(SET_ME|GENERATE_AT_DEPLOY|SET_BY_ENTRYPOINT|^[[:space:]]*$|b3coinrpc|<|your|example|placeholder|REPLACE)'

is_placeholder() {
    # Shell variable references (${VAR}, $VAR) are placeholders, not secrets
    case "$1" in
        *'${'*|*'$'*) return 0 ;;
    esac
    echo "$1" | grep -Eqi "$PLACEHOLDER_RE"
}

scan_file() {
    local f="$1"
    # Hard rule: never-allowed filenames
    if echo "$f" | grep -Eq "$NEVER_COMMIT_RE"; then
        if [[ "$f" == *.example ]]; then return 0; fi
        echo "SECRETS-SCAN: forbidden file staged: $f" >&2
        FAILED=1
        return
    fi
    # Binary files: skip content scan
    if file "$f" 2>/dev/null | grep -q 'binary'; then return 0; fi
    # Secret-value scan
    while IFS= read -r line; do
        if echo "$line" | grep -Eq "$SECRET_VALUE_RE"; then
            val=$(echo "$line" | sed -E "s/.*=[[:space:]]*//")
            if is_placeholder "$val" || [[ ${#val} -lt 8 ]]; then
                continue
            fi
            echo "SECRETS-SCAN: secret-like value in $f: $(echo "$line" | cut -c1-60)..." >&2
            FAILED=1
        fi
    done < "$f"
}

if [[ "$MODE" == "--cached" ]]; then
    # Staged files only
    while IFS= read -r f; do
        [ -f "$f" ] && scan_file "$f"
    done < <(git diff --cached --name-only --diff-filter=ACM 2>/dev/null)
else
    # Tracked files
    while IFS= read -r f; do
        [ -f "$f" ] && scan_file "$f"
    done < <(git ls-files 2>/dev/null)
fi

if [ "$FAILED" -ne 0 ]; then
    echo "" >&2
    echo "SECRETS-SCAN FAILED — remove secrets and retry. If this is a false positive, use: git commit --no-verify" >&2
    exit 1
fi
exit 0
