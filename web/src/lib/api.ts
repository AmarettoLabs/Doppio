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
	pollSeconds: number;
	maxTurns: number;
	status: string;
	containerId?: string;
	error?: string;
	lastPollAt?: string;
	nextPollAt?: string;
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
	pollSeconds: number;
	maxTurns: number;
}

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

async function req<T>(path: string, init?: RequestInit): Promise<T> {
	const r = await fetch(path, init);
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
	providers: () => req<{ llm: ProviderInfo[]; git: GitProviderInfo[] }>('/api/providers'),
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
