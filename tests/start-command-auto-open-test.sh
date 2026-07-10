#!/bin/bash

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/bin"

cat > "$TMP_DIR/bin/curl" <<'EOF'
#!/bin/bash
exit 0
EOF

cat > "$TMP_DIR/bin/open" <<'EOF'
#!/bin/bash
echo "$*" >> "$TEST_OPEN_LOG"
EOF

cat > "$TMP_DIR/fake-dev.sh" <<'EOF'
#!/bin/bash
sleep 2
EOF

chmod +x "$TMP_DIR/bin/curl" "$TMP_DIR/bin/open" "$TMP_DIR/fake-dev.sh"

TEST_OPEN_LOG="$TMP_DIR/open.log" \
PATH="$TMP_DIR/bin:/usr/bin:/bin" \
DEV_SCRIPT="$TMP_DIR/fake-dev.sh" \
AUTO_OPEN_TIMEOUT=3 \
AUTO_OPEN_INTERVAL=1 \
OPEN_CMD="$TMP_DIR/bin/open" \
CURL_CMD="$TMP_DIR/bin/curl" \
bash "$ROOT/start.command"

grep -q "http://127.0.0.1:3011/" "$TMP_DIR/open.log"
