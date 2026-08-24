#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${REPO_ROOT}/.venv"

command -v git >/dev/null 2>&1 || { echo "setup-local: Git is required" >&2; exit 1; }
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || { echo "setup-local: Python 3.12+ is required" >&2; exit 1; }

python_version="$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${python_version}" != 3.1[2-9] && "${python_version}" != 3.[2-9][0-9] ]]; then
	echo "setup-local: Python 3.12+ is required; found ${python_version}" >&2
	exit 1
fi

cd "${REPO_ROOT}"
if [[ ! -x "${VENV_DIR}/bin/python" || ! -x "${VENV_DIR}/bin/pip" ]]; then
	if ! "${PYTHON_BIN}" -m venv "${VENV_DIR}"; then
		echo "setup-local: Python venv support is required (install python3-venv)." >&2
		exit 1
	fi
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install --requirement requirements.txt
"${VENV_DIR}/bin/python" -m pip install --editable . --no-deps

"${PYTHON_BIN}" "${SCRIPT_DIR}/sync_env_file.py" \
	--env-file "${REPO_ROOT}/.env" \
	--example-file "${REPO_ROOT}/.env.example"
echo "Update database and CBBD credentials; read-only Spaces credentials are required when pending ticketed releases must be synchronized."

git config core.hooksPath .githooks

# On a fresh local V34 database this downloads and restores the newest verified
# development snapshot. Existing databases and incomplete prerequisites are
# reported and left untouched, so rerunning setup is safe. Schema-dependent
# releases are migrated from their pinned Fastify revisions by the sync hook.
set +e
"${VENV_DIR}/bin/python" -m scripts.bootstrap_development_database
bootstrap_status=$?
set -e
if [[ "${bootstrap_status}" -ne 0 && "${bootstrap_status}" -ne 10 ]]; then
	exit "${bootstrap_status}"
fi

# Apply any descriptor-backed schema migrations and deltas that were committed
# after the newest full snapshot. The same guarded command is used by all Git
# update hooks.
if [[ "${bootstrap_status}" -eq 0 ]]; then
	"${REPO_ROOT}/.githooks/run-pending-data-releases"
fi

echo "Local setup complete. Use ${VENV_DIR}/bin/python for pipeline commands."
