# Befundlink. From scratch: `make bootstrap` (= up + setup + sync + validator + smoke).
SHELL := /bin/bash
COMPOSE := timeout 180 docker compose -f infra/docker-compose.yml
JAVA ?= /opt/homebrew/opt/openjdk@21/bin/java
VALIDATOR := infra/tools/validator_cli.jar
# Pinned: HL7 FHIR Validation tool 7.0.2 (Git# de3463393298), 204412235 bytes.
VALIDATOR_VERSION := 7.0.2
VALIDATOR_SHA256 := e3e50f06b806437821b7aef8189a1b790415cb96c5036e0812ed62981452b377
VALIDATOR_URL := https://github.com/hapifhir/org.hl7.fhir.core/releases/download/$(VALIDATOR_VERSION)/validator_cli.jar
# Optional: copy from a local jar instead of downloading, e.g. `make validator VALIDATOR_SRC=~/jars/validator_cli.jar`
VALIDATOR_SRC ?=
# Bind address of the service (port 8000). Loopback by default. `make run HOST=0.0.0.0` for a phone test
# deliberately exposes the service, including the outside-person /r/* links, to everyone on the LAN.
HOST ?= 127.0.0.1
# Image pull policy for `make up`: missing (default) | never (offline / metered) | always
PULL ?= missing

.PHONY: bootstrap up down stop setup status logs clean validator validate-patient sync smoke hapi-interceptor demo-reset

bootstrap: up setup sync validator smoke

up:
	$(COMPOSE) up -d --pull $(PULL) --wait --wait-timeout 120 synapse mailpit element
	$(COMPOSE) up -d --pull $(PULL) hapi

down:
	$(COMPOSE) down

stop:
	$(COMPOSE) stop

setup:
	infra/setup.sh

status:
	@$(COMPOSE) ps -a --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
	@printf 'synapse  '; timeout 3 curl -fsS http://localhost:8008/health || printf 'DOWN'; echo
	@printf 'element  '; timeout 3 curl -fsS -o /dev/null -w '%{http_code}' http://localhost:8080/ || printf 'DOWN'; echo
	@printf 'hapi     '; timeout 5 curl -fsS -o /dev/null -w '%{http_code}' http://localhost:8090/fhir/metadata || printf 'DOWN'; echo
	@printf 'mailpit  '; timeout 3 curl -fsS -o /dev/null -w '%{http_code}' http://localhost:8025/livez || printf 'DOWN'; echo
	@test -f infra/state.json && echo "state    infra/state.json present" || echo "state    infra/state.json MISSING (make setup)"

logs:
	$(COMPOSE) logs -f --tail 100

## Wipes Matrix users/room, HAPI data, mailpit, state.json
clean:
	$(COMPOSE) down -v --remove-orphans
	rm -f infra/state.json

sync:
	uv sync

validator: $(VALIDATOR)

$(VALIDATOR):
	mkdir -p infra/tools
	if [ -n "$(VALIDATOR_SRC)" ]; then cp "$(VALIDATOR_SRC)" $@.part; \
	else curl -fL --retry 3 --max-time 600 -o $@.part "$(VALIDATOR_URL)"; fi
	echo "$(VALIDATOR_SHA256)  $@.part" | shasum -a 256 -c - || { rm -f $@.part; echo "validator_cli.jar is not $(VALIDATOR_VERSION)"; exit 1; }
	mv $@.part $@

## Validate the scenario patient (also warms ~/.fhir/packages for offline use)
validate-patient: $(VALIDATOR)
	$(JAVA) -jar $(VALIDATOR) infra/patient.json -version 4.0 -tx n/a

smoke:
	python3 -I infra/smoke.py

## Rebuild infra/hapi/classes (pgk.AppendOnlyInterceptor) against the running HAPI image's own jars (no download)
hapi-interceptor:
	@d=$$(mktemp -d) && trap 'rm -rf "$$d"' EXIT && \
	timeout 120 docker compose -f infra/docker-compose.yml cp hapi:/app/main.war "$$d/main.war" && \
	(cd "$$d" && unzip -q -o main.war 'WEB-INF/lib/hapi-fhir-base-*.jar' 'WEB-INF/lib/hapi-fhir-server-[0-9]*.jar') && \
	rm -rf infra/hapi/classes && \
	$(dir $(JAVA))javac --release 21 -cp "$$(ls $$d/WEB-INF/lib/*.jar | paste -sd: -)" -d infra/hapi/classes infra/hapi/src/pgk/AppendOnlyInterceptor.java && \
	echo "rebuilt infra/hapi/classes; apply with: $(COMPOSE) up -d --no-deps --force-recreate hapi"

## Fresh demo run without pulling anything: wipe app state (service SQLite, bot state) + all volumes, then bootstrap.
## Restart `make run` (service + bot) afterwards: the room id and tokens change.
demo-reset:
	@if timeout 2 curl -s -o /dev/null http://localhost:8000/ || timeout 2 curl -s -o /dev/null http://localhost:8001/; then \
	  echo "WARNING: service/bot still running; they keep the old SQLite handle and room id. Restart them after this."; fi
	rm -f service/data/pgk.sqlite*
	rm -f bot/.data/bot_state.json
	$(MAKE) clean
	$(MAKE) bootstrap PULL=never

## --- app processes -------------------------------------------------------
## Secrets: generated once into .env (gitignored). PGK_SECRET signs eID assertions; PGK_BPK_SALT salts bPK hashes.
## PGK_NOTIFY_KEY authenticates the service to the bot's POST /notify (header X-PGK-Notify-Key).
.env:
	@printf 'PGK_SECRET=%s\nPGK_BPK_SALT=%s\nPGK_API_KEY=%s\nPGK_NOTIFY_KEY=%s\n' "$$(python3 -c 'import secrets;print(secrets.token_hex(32))')" "$$(python3 -c 'import secrets;print(secrets.token_hex(16))')" "$$(python3 -c 'import secrets;print(secrets.token_hex(24))')" "$$(python3 -c 'import secrets;print(secrets.token_hex(24))')" > .env
	@echo "wrote .env"

## Append keys added after an existing .env was generated (never overwrites existing values).
env-keys: .env
	@grep -q '^PGK_NOTIFY_KEY=.' .env || { printf 'PGK_NOTIFY_KEY=%s\n' "$$(python3 -c 'import secrets;print(secrets.token_hex(24))')" >> .env; echo "added PGK_NOTIFY_KEY to .env"; }

.PHONY: service bot run demo test audit-verify env-keys

service: env-keys
	set -a; . ./.env; set +a; uv run uvicorn service.app:app --host $(HOST) --port 8000

bot: env-keys
	set -a; . ./.env; set +a; uv run python -m bot

## service + bot together (Ctrl-C stops both)
run: env-keys
	set -a; . ./.env; set +a; trap 'kill 0' INT TERM EXIT; \
	uv run uvicorn service.app:app --host $(HOST) --port 8000 & \
	uv run python -m bot & wait

## demo mode (PGK_DEMO=1): a request to a person with no simulated ID Austria enrols one and shows Dr. Hofer
## the name + PIN to pass on (stands in for that person's own ID Austria app); SMS inbox from localhost
demo:
	PGK_DEMO=1 $(MAKE) run

test:
	uv run pytest -q

audit-verify: env-keys
	set -a; . ./.env; set +a; uv run python -m service.audit_verify --fhir

## Re-verify every filed Provenance signature against the FHIR read-back (content-bound)
.PHONY: verify-provenance
verify-provenance: env-keys
	set -a; . ./.env; set +a; uv run python -m service.verify_provenance
