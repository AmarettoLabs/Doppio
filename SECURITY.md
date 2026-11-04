# Security

Doppio is a self-hosted engineering tool that runs LLM-driven agents with a
shell. Read this before exposing it to anyone but yourself.

## Reporting a vulnerability

Please report security issues privately via GitHub's
[private vulnerability reporting](../../security/advisories/new) rather than in
a public issue. We aim to acknowledge reports within a few working days.

## Threat model

### The manager is host-root

The manager container mounts `/var/run/docker.sock` to create agent
containers. Anyone who can use the portal or its API can start arbitrary
containers and therefore control the host. Consequently:

- `deploy/docker-compose.yml` binds the portal to `127.0.0.1` by default.
- Set `PORTAL_PASSWORD` before binding it elsewhere (`BIND_ADDR=0.0.0.0`),
  and put it behind a TLS-terminating reverse proxy. The session cookie is
  marked `Secure` when the proxy sends `X-Forwarded-Proto: https`.
- There is a single shared password and no user accounts or roles.

### Agents run untrusted input through a shell

An agent acts on mentions, assignments and review requests in the repos it
watches. **Anyone who can comment on, open or assign issues/PRs in those repos
can steer the model**, and the model has a `shell` tool inside its container.
Assume that such a person can:

- read everything in the agent container, including its config
  (`/agent/state/config.json`: model API token, git token, internal token)
  and its workspace;
- push, comment and open PRs with the agent's git account;
- spend the agent's model API budget;
- reach anything on the network the container can reach (the shared
  `doppio_agents` network and, through `host.docker.internal`, the host).

Mitigations you should apply:

- Watch **private repos** where only trusted people can write, or accept the
  risk above for public ones.
- Give every agent a **dedicated git account** with a **fine-grained token**
  scoped to just the repos it needs, without admin or org permissions.
- Use a **dedicated model API key** per agent with a spending limit.
- Do not put secrets in the agent's workspace, persona or task.

### Isolation between agents

Each container receives only its own internal token (an HMAC of the manager's
master token and the agent ID). With it, an agent can only post events,
create/claim tasks and finish tasks for itself; it cannot act for other
agents, and it cannot reach the portal API when `PORTAL_PASSWORD` is set.
Agent containers do share one Docker network and are not hardened beyond
Docker's defaults (they run as root inside the container, with no resource
limits).

### Data at rest

All state, including agent tokens, is stored unencrypted in `DATA_DIR`
(`store.json`, `manager-token` and per-agent `config.json`, written with mode `0600`). Protect
that directory and its backups accordingly. Never commit it: it is listed in
`.gitignore`.
