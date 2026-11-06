export interface AgentView {
	id: string;
	name: string;
	persona: string;
	task: string;
	provider: string;
	model: string;
	apiBase?: string;
	hasApiToken: boolean;
	emails: string[];
	gitProvider?: string;
	gitBaseURL?: string;
	hasGitToken: boolean;
	gitLogin?: string;
	gitRepos: string[];
	ownerLogin?: string;
	peerLogins: string[];
	triggers: string[];
	pollSeconds: number;
	maxTurns: number;
	status: string;
	containerId?: string;
	error?: string;
	lastPollAt?: string;
	nextPollAt?: string;
	/** a task is running or about to be picked up */
	working: boolean;
	restartPending: boolean;
	engine: string;
	taskMinutes: number;
	maxReplyTokens: number;
	defaultMaxReplyTokens: number;
	createdAt: string;
	updatedAt: string;
}

export interface AgentPayload {
	name: string;
	persona: string;
	task: string;
	provider: string;
	model: string;
	apiBase: string;
	apiToken: string;
	emails: string[];
	gitProvider: string;
	gitBaseURL: string;
	gitToken: string;
	gitLogin: string;
	gitRepos: string[];
	ownerLogin: string;
	peerLogins: string[];
	triggers: string[];
	pollSeconds: number;
	maxTurns: number;
	engine: string;
	taskMinutes: number;
	/** 0 = the default for the agent's server */
	maxReplyTokens: number;
}

/** Reply caps when an agent sets none (keep in sync with the Go model). */
export const HOSTED_MAX_REPLY_TOKENS = 32768;
export const LOCAL_MAX_REPLY_TOKENS = 8192;

/** An engine that runs an agent's tasks; unavailable ones are listed but can't be picked yet. */
export interface EngineInfo {
	id: string;
	label: string;
	available: boolean;
}

/** Git activity an agent can respond to, in display order. */
export const TRIGGERS: { id: string; label: string; hint: string }[] = [
	{ id: 'review_requests', label: 'Review requests', hint: 'review the PR and comment with a verdict' },
	{ id: 'mentions', label: 'Mentions', hint: 'answer in a comment, no code changes' },
	{ id: 'assigned_issues', label: 'Assigned issues', hint: 'implement the issue and open a PR' },
	{ id: 'assigned_prs', label: 'Assigned PRs', hint: 'address the review feedback and push' }
];

export interface Task {
	id: string;
	agentId: string;
	kind: string;
	title: string;
	input: string;
	source?: string;
	status: string;
	output?: string;
	error?: string;
	createdAt: string;
	startedAt?: string;
	finishedAt?: string;
}

export interface AgentEvent {
	id: number;
	agentId: string;
	taskId?: string;
	ts: string;
	type: string;
	content: string;
}

export interface ProviderInfo {
	id: string;
	name: string;
	apiBase: string;
	models: string[];
}

export interface GitProviderInfo {
	id: string;
	name: string;
}

/**
 * Estimated server clock minus browser clock, in ms, from the Date header of
 * API responses. Countdowns to server timestamps add this to Date.now() so a
 * skewed browser clock doesn't make them end early or late.
 */
export const clock = { offsetMs: 0 };

export function serverNow(): number {
	return Date.now() + clock.offsetMs;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
	const sent = Date.now();
	const r = await fetch(path, init);
	const date = r.headers.get('Date');
	if (date) {
		const server = Date.parse(date);
		// Date has 1s resolution (truncated): +500ms is the expected value.
		if (!Number.isNaN(server)) clock.offsetMs = server + 500 - (sent + Date.now()) / 2;
	}
	if (!r.ok) {
		let msg = `${r.status} ${r.statusText}`;
		try {
			const j = await r.json();
			if (j?.error) msg = j.error;
		} catch {
			/* ignore */
		}
		throw new Error(msg);
	}
	return r.json() as Promise<T>;
}

const json = (method: string) => (body: unknown) => ({
	method,
	headers: { 'Content-Type': 'application/json' },
	body: JSON.stringify(body)
});

export const api = {
	providers: () => req<{ llm: ProviderInfo[]; git: GitProviderInfo[]; engines: EngineInfo[] }>('/api/providers'),
	agents: () => req<AgentView[]>('/api/agents'),
	agent: (id: string) => req<AgentView>(`/api/agents/${id}`),
	createAgent: (a: AgentPayload) => req<AgentView>('/api/agents', json('POST')(a)),
	updateAgent: (id: string, a: AgentPayload) => req<AgentView>(`/api/agents/${id}`, json('PATCH')(a)),
	deleteAgent: (id: string) => req<{ deleted: string }>(`/api/agents/${id}`, { method: 'DELETE' }),
	action: (id: string, op: 'start' | 'stop' | 'restart') =>
		req<AgentView>(`/api/agents/${id}/${op}`, { method: 'POST' }),
	tasks: (id: string) => req<Task[]>(`/api/agents/${id}/tasks`),
	createTask: (id: string, input: string, title?: string) =>
		req<Task>(`/api/agents/${id}/tasks`, json('POST')({ input, title })),
	events: (id: string, since = 0) => req<AgentEvent[]>(`/api/agents/${id}/events?since=${since}`)
};

export function splitList(s: string): string[] {
	return s
		.split(',')
		.map((x) => x.trim())
		.filter((x) => x.length > 0);
}
