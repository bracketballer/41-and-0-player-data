#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"

if [[ -z "${PYTHON_BIN:-}" ]]; then
	if command -v python3 >/dev/null 2>&1; then
		PYTHON_BIN="python3"
	else
		# Native Windows Python installs typically only provide "python", not "python3".
		PYTHON_BIN="python"
	fi
fi

command -v git >/dev/null 2>&1 || { echo "setup-local: Git is required" >&2; exit 1; }
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || { echo "setup-local: Python 3.12+ is required" >&2; exit 1; }

python_version="$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${python_version}" != 3.1[2-9] && "${python_version}" != 3.[2-9][0-9] ]]; then
	echo "setup-local: Python 3.12+ is required; found ${python_version}" >&2
	exit 1
fi

cd "${REPO_ROOT}"
if [[ ! -x "${VENV_DIR}/bin/python" && ! -x "${VENV_DIR}/Scripts/python.exe" ]]; then
	if ! "${PYTHON_BIN}" -m venv "${VENV_DIR}"; then
		echo "setup-local: Python venv support is required (install python3-venv)." >&2
		exit 1
	fi
fi

# POSIX venvs use bin/; native Windows venvs use Scripts/.
if [[ -x "${VENV_DIR}/bin/python" ]]; then
	VENV_PYTHON="${VENV_DIR}/bin/python"
else
	VENV_PYTHON="${VENV_DIR}/Scripts/python.exe"
fi

"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install --requirement requirements.txt
"${VENV_PYTHON}" -m pip install --editable . --no-deps

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
"${VENV_PYTHON}" -m scripts.bootstrap_development_database
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

echo "Local setup complete. Use ${VENV_PYTHON} for pipeline commands."
