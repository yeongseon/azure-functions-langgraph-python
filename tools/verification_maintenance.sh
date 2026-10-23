#!/bin/sh
environment=$("${HATCH:-hatch}" env find 2>/dev/null) || environment=
for interpreter in "${VIRTUAL_ENV:-/nonexistent}/bin/python" "$environment/bin/python" .venv/bin/python python3.11 python3 python; do
    if "$interpreter" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$interpreter" -m tools.verification_maintenance "$@"
    fi
done
printf '%s\n' 'Python >=3.11 required; development environment not created: run make install' >&2
exit 2
