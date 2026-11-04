<script lang="ts">
	import { goto } from '$app/navigation';
	import AgentForm from '#lib/AgentForm.svelte';
	import { api, type AgentPayload } from '#lib/api';

	async function create(payload: AgentPayload) {
		const a = await api.createAgent(payload);
		goto(`/agents/${a.id}`);
	}
</script>

<h1>New agent</h1>
<div class="panel">
	<AgentForm onsubmit={create} />
</div>
