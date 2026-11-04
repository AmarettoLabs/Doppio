<script lang="ts">
	import { page } from '$app/state';
	import { api, type AgentEvent, type AgentView, type Task } from '#lib/api';
	import PollTimer from '#lib/PollTimer.svelte';

	let agent = $state<AgentView | null>(null);
	let tasks = $state<Task[]>([]);
	let events = $state<AgentEvent[]>([]);
	let error = $state('');
	let taskInput = $state('');
	let filter = $state<'all' | 'discussion' | 'logs'>('all');
	let streamEl = $state<HTMLElement | null>(null);
	let busy = $state(''); // action in flight: start/stop/restart/delete/run
	let notice = $state('');

	const id = $derived(page.params.id ?? '');

	async function load() {
		try {
			agent = await api.agent(id);
			tasks = await api.tasks(id);
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

	// live event stream (SSE); the server replays the backlog on (re)connect,
	// so dedupe by event id before appending
	const seenIds = new Set<number>();
	$effect(() => {
		events = [];
		seenIds.clear();
		const es = new EventSource(`/api/agents/${id}/stream`);
		es.onmessage = (m) => {
			const ev = JSON.parse(m.data) as AgentEvent;
			if (seenIds.has(ev.id)) return;
			seenIds.add(ev.id);
			events = [...events, ev];
		};
		return () => es.close();
	});

	// auto-scroll the stream
	$effect(() => {
		events.length; // track
		if (streamEl) streamEl.scrollTop = streamEl.scrollHeight;
	});

	const filtered = $derived(
		events.filter((e) => {
			if (filter === 'discussion') return e.type === 'user' || e.type === 'assistant';
			if (filter === 'logs') return e.type !== 'user' && e.type !== 'assistant';
			return true;
		})
	);

	async function act(op: 'start' | 'stop' | 'restart') {
		if (busy) return;
		busy = op;
		notice = `${op} in progress…`;
		try {
			await api.action(id, op);
			error = '';
			notice = `${op} done`;
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = '';
		}
		load();
		setTimeout(() => (notice = ''), 4000);
	}

	async function runTask() {
		if (!taskInput.trim() || busy) return;
		busy = 'run';
		notice = 'Task queued…';
		try {
			await api.createTask(id, taskInput.trim());
			taskInput = '';
			error = '';
			load();
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = '';
		}
		setTimeout(() => (notice = ''), 4000);
	}

	async function del() {
		if (busy) return;
		if (!confirm('Delete this agent, its tasks and its event history?')) return;
		busy = 'delete';
		notice = 'Deleting…';
		try {
			await api.deleteAgent(id);
			location.href = '/';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
			busy = '';
			notice = '';
		}
	}
</script>

{#if agent}
	<div class="row" style="justify-content: space-between">
		<h1>{agent.name} <span class="badge {agent.status}">{agent.status}</span></h1>
		<div class="row">
			{#if busy === 'start' || busy === 'stop' || busy === 'restart' || busy === 'delete'}
				<button disabled><span class="spinner"></span>{busy}…</button>
			{:else}
				{#if agent.status === 'running'}
					<button class="ghost" onclick={() => act('stop')}>Stop</button>
				{:else}
					<button onclick={() => act('start')}>Start</button>
				{/if}
				<button class="ghost" onclick={() => act('restart')}>Restart</button>
				<a class="btn ghost" href="/agents/{agent.id}/edit">Edit</a>
				<button class="danger" onclick={del}>Delete</button>
			{/if}
		</div>
	</div>
	{#if notice}<p class="muted">{notice}</p>{/if}
	{#if error}<p class="err">{error}</p>{/if}
	{#if agent.error}<p class="err">{agent.error}</p>{/if}

	<h2>Configuration</h2>
	<div class="panel">
		<dl class="summary">
			<dt>Model</dt><dd>{agent.provider}/{agent.model}{agent.apiBase ? ` (${agent.apiBase})` : ''} · token {agent.hasApiToken ? 'set' : 'missing'}</dd>
			<dt>Emails</dt><dd>{agent.emails?.join(', ') || '—'}</dd>
			<dt>Git</dt><dd>{agent.gitProvider && agent.gitProvider !== 'none' ? `${agent.gitProvider} @${agent.gitLogin ?? ''} · token ${agent.hasGitToken ? 'set' : 'missing'} · repos ${(agent.gitRepos ?? []).join(', ') || '—'}${agent.ownerLogin ? ` · owner @${agent.ownerLogin}` : ''}` : '—'}</dd>
			<dt>Poll</dt><dd>every {agent.pollSeconds}s</dd>
			<dt>Next git check</dt><dd>
				{#if agent.gitProvider && agent.gitProvider !== 'none'}
					<PollTimer nextPollAt={agent.nextPollAt} pollSeconds={agent.pollSeconds} running={agent.status === 'running'} textOnly />
				{:else}
					<span class="muted">git not configured</span>
				{/if}
			</dd>
			<dt>Max turns</dt><dd>{agent.maxTurns === -1 ? 'unlimited (context limit)' : agent.maxTurns}</dd>
			<dt>Persona</dt><dd class="muted">{agent.persona.slice(0, 300)}{agent.persona.length > 300 ? '…' : ''}</dd>
			<dt>Task</dt><dd class="muted">{agent.task.slice(0, 300)}{agent.task.length > 300 ? '…' : ''}</dd>
		</dl>
	</div>

	<h2>Run a task</h2>
	<div class="panel">
		<textarea bind:value={taskInput} placeholder="Ask this agent to do something… (queued; the running container picks it up within a few seconds)"></textarea>
		<div class="row" style="margin-top:0.5rem">
			<button onclick={runTask} disabled={!taskInput.trim() || agent.status !== 'running' || busy === 'run'}>
				{#if busy === 'run'}<span class="spinner"></span>Queuing…{:else}Run{/if}
			</button>
			{#if agent.status !== 'running'}<span class="muted">start the agent first</span>{/if}
		</div>
	</div>

	<h2>Tasks</h2>
	{#if tasks.length === 0}
		<p class="muted">No tasks yet.</p>
	{:else}
		<table>
			<thead><tr><th>Status</th><th>Kind</th><th>Title</th><th>Created</th><th>Output / error</th></tr></thead>
			<tbody>
				{#each tasks as t (t.id)}
					<tr>
						<td><span class="badge {t.status}">{t.status}</span></td>
						<td>{t.kind}{t.source ? ` · ${t.source}` : ''}</td>
						<td>{t.title}</td>
						<td class="muted">{new Date(t.createdAt).toLocaleString()}</td>
						<td class="muted">{t.error ? `⚠ ${t.error}` : (t.output ?? '').slice(0, 200)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<h2>Live stream</h2>
	<div class="filters">
		<button class={filter === 'all' ? 'active' : ''} onclick={() => (filter = 'all')}>All</button>
		<button class={filter === 'discussion' ? 'active' : ''} onclick={() => (filter = 'discussion')}>Discussion</button>
		<button class={filter === 'logs' ? 'active' : ''} onclick={() => (filter = 'logs')}>Logs & actions</button>
	</div>
	<div class="stream" bind:this={streamEl}>
		{#each filtered as ev (ev.id)}
			<div class="ev {ev.type}">
				<span class="t">{new Date(ev.ts).toLocaleTimeString()} #{ev.id}</span>
				<span class="c">{ev.content}</span>
			</div>
		{:else}
			<div class="ev log"><span class="c muted">No events yet. Start the agent to see live logs and discussions.</span></div>
		{/each}
	</div>
{:else}
	<p class="muted">Loading…</p>
{/if}
