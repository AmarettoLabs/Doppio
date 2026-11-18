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

An agent only starts work when its owner asks (assigns it, requests its
review or mentions it), but the model then reads the whole discussion, the
item's description and the repository's files, which other people can write.
**Anyone who can comment on, open or edit issues/PRs, or push to those repos,
can steer the model through that content**, and the model has a `shell` tool
inside its container. Assume that such a person can:

- read and change everything in the agent's own workspace (checkouts,
  caches, scratch files);
- push, comment and open PRs **on tasks it was assigned** (that engine holds
  the agent's git token, because pushing is the job);
- spend the agent's model API budget;
- reach anything on the network the container can reach (the shared
  `doppio_agents` network and, through `host.docker.internal`, the host).

What they can no longer reach: the engine (the Doppio loop's shell tool, the
CLI engines) runs as an unprivileged user, which cannot read `/agent/state`
— the agent's config with its model API token, git token and internal token —
nor the worker's own files (its git token included: the worker fetches
through root-owned, engine-readable mirrors of the watched repos, so the
engine's git in a checkout never sees it). Its HOME is rebuilt per task and
removed at the end, so no credential outlives a task. Review and mention
tasks give the engine no git token at all (the worker posts the answer
itself, and the Doppio loop refuses its write tools there); portal messages
too. One secret is necessarily inside the engine's reach: a CLI engine's
model key (Mistral Vibe's `MISTRAL_API_KEY`, a Claude Code or Codex login)
is passed into that engine's environment, so a steered CLI engine can read
and spend it — only the Doppio loop keeps the model key away from the
engine. Agent containers also drop every capability, cannot gain new
privileges (`no-new-privileges`) and run under memory, CPU and process
limits.

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
Agent containers do share one Docker network. They are hardened (no
capabilities, no-new-privileges, resource limits), and inside them only the
worker runs privileged; the engines, which execute what the model asks, run
as an unprivileged user that cannot read the agent's or any account's root
state — a CLI engine's own key excepted, which its process needs (see
above).

### Data at rest

All state, including agent tokens, is stored unencrypted in `DATA_DIR`
(`store.json`, `manager-token` and per-agent `config.json`, written with mode `0600`). Protect
that directory and its backups accordingly. Never commit it: it is listed in
`.gitignore`.
