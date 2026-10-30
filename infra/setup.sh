#!/usr/bin/env bash
# Idempotent: register Matrix users, create the room, PUT the patient, write infra/state.json.
set -euo pipefail
cd "$(dirname "$0")"

SECRET="$(timeout 20 docker compose exec -T synapse cat /data/registration_shared_secret)" || {
  echo "ERROR: cannot read the registration secret; is the stack up? (make up)" >&2; exit 1; }
SYNAPSE_REG_SECRET="$SECRET" exec python3 -I setup.py
