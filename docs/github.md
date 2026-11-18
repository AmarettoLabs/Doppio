# Agents on GitHub

How an agent finds work on GitHub, what it does with it, and the settings that control it. GitLab works the same way; the differences are noted at the end.

## Settings

These are in the agent form, under **Git**.

| Setting | What it does |
| --- | --- |
| Provider | `GitHub`, `GitLab` or none. With none, the agent only answers portal messages. |
| Agent's git login | The agent's own account. This is the account it gets assigned to and pushes as. |
| Git base URL | Leave empty for github.com. For GitHub Enterprise, give the server URL (`/api/v3` is added). |
| Access token | The token of the agent's account. Use a fine-grained token limited to the watched repos, with read/write on contents, issues and pull requests (see [SECURITY.md](../SECURITY.md)). |
| Repositories to watch | `owner/repo`, comma-separated. The agent only looks at these. |
| Owner login | The only account whose requests the agent acts on. Required. |
| Peer agent logins | Stored for context. The worker doesn't use them yet. |
| Responds to | Which triggers are on (see below). If none are ticked, all four are on. |
| Poll interval | Seconds between git checks (default 1800, minimum 5). |
| Max tool turns / task minutes | Limits for one task. |

Several agents can share one git account and token, each with different triggers and models.

## How work is picked up

1. **Polling.** Every poll interval, the worker searches each watched repo for open issues and PRs that involve the agent's login. These are the ones assigned to it, the ones where its review is requested and the ones that mention it. It also lists the open PRs directly, because GitHub's search index can lag.
2. **The owner check.** An item becomes work only if the owner asked for it:
   - an **assignment** or **review request** must have been made by the owner, according to the item's event history;
   - a **mention** must have been written by the owner, in the description or in a comment.

   Anything else is ignored and logged in the agent's events. If the owner asks later, the item becomes work at that point.
3. **No repeats.** Each piece of work has a key, and a key that was already handled isn't run again. The key changes, so the item becomes new work, when:

   | Trigger | New work when |
   | --- | --- |
   | Review request | a new commit is pushed to the PR (the key is the head SHA) |
   | Assigned issue or PR | the owner adds a new comment or review |
   | Mention | the owner writes a new mention |

   Re-assigning an item or changing its labels does **not** count as new work. To restart an agent on an item, comment on it.
4. **First start.** On its first check, an agent only records what already exists. Old assignments and mentions are skipped, but pending review requests are still picked up.
5. **Queue.** One task runs at a time, with assigned work first. If a task fails because of something around it (the model API, the network, the workspace), it is retried at the next check, up to 3 attempts.

The agent's state is in `/agent/workspace/worker-state.json`.

## What each trigger does

| Trigger | The agent... |
| --- | --- |
| Assigned issues | Creates a branch `<login>/<number>-<slug>`, implements and tests the change, pushes, opens a PR with `Closes #<number>`, then comments with the link. If the issue is unclear or blocked, it asks in a comment instead. |
| Assigned PRs | Checks out the PR branch, addresses the owner's review comments and pushes. |
| Review requests | Reviews the PR against the repo's guidance and submits a GitHub review (approve, request changes or comment). It never pushes. |
| Mentions | Answers in a comment. It never changes code. |

Before assigned work and reviews, the worker updates the repo's clone and puts the repo's guidance in the prompt: every `AGENTS.md`, `CLAUDE.md` and `CONTRIBUTING.md`, plus the root `README.md`.

## Status labels

On assigned work, the worker keeps the item's status label up to date, but only if the repo has a `ready` label (either `ready` or `status:ready`).

- **When a task starts:** the item is labeled `in-progress`.
- **When it ends:** the item gets the status from the agent's last `STATUS:` line, which is `needs-review`, `question`, `on-hold` or `blocked`. Without a `STATUS:` line, the label is `needs-review` if the agent opened a PR and `question` otherwise. The item's other status labels, `ready` included, are removed.
- **When it fails:** the label is `blocked`. A task that will be retried stays `in-progress`.

The worker uses the label names the repo already has, plain or with the `status:` prefix; plain names win when both exist. It never creates a label.

Labels are **written only**: adding `ready` doesn't start work. Assigning the item does.

## Typical flows

- **Get an issue done:** assign the issue to the agent. It opens a PR and comments with the link. Review the PR; assign the PR to the agent and leave comments to get changes made.
- **Answer the agent's question:** reply in a comment on the item. The agent picks the work up again at the next check.
- **Get a review:** request the agent's review on the PR. Push new commits to get another review.
- **Ask something quick:** mention `@<agent-login>` in a comment.

## GitLab

The same settings apply, with these differences:
- the base URL is the GitLab server;
- PRs are merge requests;
- a review is a note, plus an approval when the verdict is approve;
- mentions are found through project search.
