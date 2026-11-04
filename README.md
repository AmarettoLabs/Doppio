<p align="center">
  <img src="web/static/assets/doppio-logo.png" alt="Doppio logo" width="120">
</p>

<h1 align="center">Doppio</h1>

<p align="center">
  <a href="https://github.com/AmarettoLabs/Doppio/actions/workflows/ci.yml"><img src="https://github.com/AmarettoLabs/Doppio/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache 2.0"></a>
</p>

![Doppio main view](assets/doppio-home.png)

AmarettoLabs runs a lot of custom coding agents manually, each one a container
with a persona, a model token, a git account and a poll loop. **Doppio** was
born out of the need to automate that work for the simplest of our agents:
today it handles around 20% of them.

Doppio is a web portal to create, deploy, track and manage those agents. Each
agent gets a name, a persona, a standing task, a model (OpenAI / Anthropic /
Mistral or any custom `provider/model` + token), email addresses, and its own
GitHub/GitLab account (login + token + repos to watch). One click deploys it as
a Docker container; the worker polls GitHub/GitLab for mentions, assignments
and review requests, answers through its model (with a tool loop: shell,
file edits, opening PRs), and streams everything live to the portal.

> **Security:** the manager drives the host Docker daemon and agents run a
> shell steered by whoever can comment in their repos. Read
> [SECURITY.md](SECURITY.md) before exposing the portal or pointing an agent at
> a repo with untrusted contributors.

## Requirements

- Docker with Compose v2 (to run it)
- Go 1.26+ and Node 22+ (only for development)

## Run it

```sh
./deploy/up.sh     # builds images, ensures network, starts compose (idempotent)
```

Portal at **http://localhost:8080**. It listens on loopback only by default;
to reach it from other machines, set a password and a bind address:

```sh
PORTAL_PASSWORD='a-long-random-secret' BIND_ADDR=0.0.0.0 ./deploy/up.sh
```

Stop with `./deploy/down.sh` (agent containers keep running until stopped
from the portal).

## Configuration

Manager environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADDR` | `:8080` | Listen address |
| `DATA_DIR` | `./data` (`/opt/doppio/data` in compose) | State: store, tokens, agent workspaces |
| `STATIC_DIR` | `./web/build` | Built SPA to serve |
| `PORTAL_PASSWORD` | _(empty = no login)_ | Portal login password |
| `AGENT_IMAGE` | `doppio-agent:latest` | Image for agent containers |
| `AGENT_NETWORK` | `doppio_agents` | Shared Docker network |
| `MANAGER_URL` | `http://manager:8080` | URL agents use to reach the manager |
| `MAX_EVENTS_PER_AGENT` | `5000` | Persisted events per agent (`0` = unlimited) |
| `DOCKER_BIN` | `docker` | Docker CLI binary |

Compose only: `BIND_ADDR` (default `127.0.0.1`) is the host interface the
portal port is published on.

## Development

```sh
go run ./cmd/server            # API on :8080, state in ./data
cd web && npm ci && npm run dev  # UI on :5173, /api proxied to :8080
go test ./...                  # backend tests
cd web && npm run check        # type-check the UI
```

Deploying agents in dev mode needs the agent image
(`docker build -f deploy/Dockerfile.agent -t doppio-agent:latest .`)
and `MANAGER_URL` set to an address the containers can reach, e.g.
`http://host.docker.internal:8080`.

## Layout

```
cmd/server/     Go API (stdlib) — also serves the built SPA
internal/       api (routes/SSE/auth), store (JSON), runtime (docker), model
web/            SvelteKit SPA
deploy/         Dockerfiles, docker-compose.yml, up.sh / down.sh
```

Agent containers run the generic worker `deploy/agent/agent_worker.py`.

## Contributing

Issues and pull requests are welcome. Please run `go test ./...` and
`npm run check` (in `web/`) before opening a PR. Report security issues
privately as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 AmarettoLabs.
