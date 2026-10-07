#!/bin/bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [ "$(id -u)" = 0 ]; then
  exec /usr/bin/python3 "$project_dir/packaging/install.py" "$@"
fi
exec sudo /usr/bin/python3 "$project_dir/packaging/install.py" "$@"
