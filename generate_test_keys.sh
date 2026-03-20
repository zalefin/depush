#!/usr/bin/env bash
# Generate the test SSH keypair used by the ssh-server Docker image and
# the rsync integration tests.
#
# Run this once before the first `docker compose build`, and again any time
# you want to rotate the keys.
#
# Usage:
#   ./generate_test_keys.sh

set -euo pipefail

KEY_DIR="$(cd "$(dirname "$0")" && pwd)/test-keys"
KEY_FILE="$KEY_DIR/depush_test_rsa"

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [[ -f "$KEY_FILE" ]]; then
    echo "Keys already exist at $KEY_DIR — delete them first to regenerate."
    exit 0
fi

ssh-keygen -t rsa -b 2048 -f "$KEY_FILE" -N '' -C 'depush-test'
chmod 600 "$KEY_FILE"
chmod 644 "${KEY_FILE}.pub"

echo ""
echo "Keys written:"
echo "  private: $KEY_FILE"
echo "  public:  ${KEY_FILE}.pub"
echo ""
echo "Next steps:"
echo "  docker compose build ssh-server"
echo "  docker compose up -d"
