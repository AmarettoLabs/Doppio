<script lang="ts">
	import { api, type AgentView } from '#lib/api';
	import PollTimer from '#lib/PollTimer.svelte';

	let agents = $state<AgentView[]>([]);
	let error = $state('');
	// agentId -> action currently in flight ("start"/"stop"/"restart"/"delete")
	let busy = $state<Record<string, string>>({});

	async function load() {
		try {
			agents = await api.agents();
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		}
	}

	$effect(() => {
		load();
		const t = setInterval(load, 5000);
		return () => clearInterval(t);
	});

	async function act(id: string, op: 'start' | 'stop' | 'restart') {
		if (busy[id]) return; // an action is already in flight for this agent
		busy = { ...busy, [id]: op };
		try {
			await api.action(id, op);
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			const b = { ...busy };
			delete b[id];
			busy = b;
		}
		load();
	}

	async function del(id: string) {
		if (busy[id]) return;
		if (!confirm('Delete this agent, its tasks and its event history?')) return;
		busy = { ...busy, [id]: 'delete' };
		try {
			await api.deleteAgent(id);
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			const b = { ...busy };
			delete b[id];
			busy = b;
		}
		load();
	}
</script>

<h1>Agents</h1>
{#if error}<p class="err">{error}</p>{/if}

{#if agents.length === 0}
	<div class="panel">
		<p class="muted">No agents yet.</p>
		<p><a class="btn" href="/agents/new">Create your first agent</a></p>
	</div>
{:else}
	{#each agents as a (a.id)}
		<div class="agent-row">
			<div class="timer-block" title="next git check">
				{#if a.gitProvider && a.gitProvider !== 'none'}
					<PollTimer nextPollAt={a.nextPollAt} pollSeconds={a.pollSeconds} running={a.status === 'running'} working={a.working} />
				{:else}
					<span class="muted">—</span>
				{/if}
			</div>
			<div class="agent-card panel">
				<div class="agent-info">
					<div class="row" style="gap:0.5rem">
						<a href="/agents/{a.id}" class="agent-name">{a.name}</a>
						<span class="badge {a.status}">{a.status}</span>
					</div>
					<div class="agent-sub muted">{a.provider}/{a.model}</div>
					{#if (a.gitRepos ?? []).length}
						<div class="agent-sub muted">{(a.gitRepos ?? []).join(', ')}</div>
					{/if}
				</div>
				<div class="row actions">
					{#if busy[a.id]}
						<button disabled><span class="spinner"></span>{busy[a.id]}…</button>
					{:else}
						{#if a.status === 'running'}
							<button class="ghost" onclick={() => act(a.id, 'stop')}>Stop</button>
						{:else}
							<button onclick={() => act(a.id, 'start')}>Start</button>
						{/if}
						<button class="ghost" onclick={() => act(a.id, 'restart')}>Restart</button>
						<button class="danger" onclick={() => del(a.id)}>Delete</button>
					{/if}
				</div>
			</div>
		</div>
	{/each}
{/if}
