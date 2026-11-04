<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import AgentForm from '#lib/AgentForm.svelte';
	import { api, type AgentPayload, type AgentView } from '#lib/api';

	let agent = $state<AgentView | null>(null);
	const id = $derived(page.params.id ?? '');
	let error = $state('');

	$effect(() => {
		api.agent(id)
			.then((a) => (agent = a))
			.catch((e) => (error = String(e instanceof Error ? e.message : e)));
	});

	async function save(payload: AgentPayload) {
		await api.updateAgent(id, payload);
		goto(`/agents/${id}`);
	}
</script>

<h1>Edit agent</h1>
{#if error}<p class="err">{error}</p>{/if}
{#if agent}
	<div class="panel">
		<AgentForm initial={agent} onsubmit={save} />
	</div>
{/if}
