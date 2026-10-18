#!/usr/bin/env bash
# Moved to secrets.nix (ADR-0009): tools/hooks/secrets-scan.sh. This wrapper
# keeps the old path working; it prints a one-line notice on stderr and execs
# the moved hook with the same arguments (tools/secrets_nix_tool.py says where
# it is found).
exec python3 "$(dirname "${BASH_SOURCE[0]}")/../secrets_nix_tool.py" hooks/secrets-scan.sh "$@"
