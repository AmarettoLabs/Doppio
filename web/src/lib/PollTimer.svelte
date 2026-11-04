<script lang="ts">
	import { serverNow } from './api';

	// Countdown to the agent's next git poll: two concentric rings — the outer
	// ring sweeps the whole poll interval (minutes scale), the inner ring
	// sweeps the seconds of the current minute. While the agent is working (a
	// task is running, or the check is due and hasn't been rescheduled yet) the
	// same rings spin as a busy indicator instead.
	let {
		nextPollAt,
		pollSeconds = 0,
		running = true,
		working = false,
		textOnly = false
	}: { nextPollAt?: string; pollSeconds?: number; running?: boolean; working?: boolean; textOnly?: boolean } = $props();

	let now = $state(serverNow());
	$effect(() => {
		const t = setInterval(() => (now = serverNow()), 250);
		return () => clearInterval(t);
	});

	const remainingMs = $derived(
		nextPollAt && running ? Math.max(0, new Date(nextPollAt).getTime() - now) : null
	);
	// A git check often takes about a second, less than the portal's refresh
	// interval: keep spinning a few seconds so it is actually visible.
	const MIN_SPIN_MS = 3000;
	const busyNow = $derived(running && (working || !nextPollAt || remainingMs === 0));
	let spinUntil = $state(0);
	$effect(() => {
		if (busyNow) spinUntil = now + MIN_SPIN_MS;
	});
	const busy = $derived(running && (busyNow || now < spinUntil));
	const totalMs = $derived(Math.max(1, (pollSeconds || 1) * 1000));
	// outer ring: fraction of the whole interval left; inner ring: seconds within the current minute
	const outerFrac = $derived(remainingMs === null ? 0 : remainingMs / totalMs);
	const innerFrac = $derived(remainingMs === null ? 0 : (remainingMs % 60000) / 60000);

	const R_OUT = 17;
	const R_IN = 11.5;
	const C_OUT = 2 * Math.PI * R_OUT;
	const C_IN = 2 * Math.PI * R_IN;

	// minutes left, shown in the center of the wheel
	const minutesLeft = $derived(remainingMs === null ? null : Math.ceil(remainingMs / 60000));

	function fmt(ms: number): string {
		const s = Math.ceil(ms / 1000);
		const m = Math.floor(s / 60);
		const sec = s % 60;
		return m > 0 ? `${m}m ${String(sec).padStart(2, '0')}s` : `${sec}s`;
	}
</script>

{#if busy && textOnly}
	<span class="poll-label" title="agent is working">working…</span>
{:else if busy}
	<span class="poll-timer" title="agent is working" role="status" aria-label="agent is working">
		<svg viewBox="0 0 40 40" width="88" height="88">
			<circle cx="20" cy="20" r={R_OUT} fill="none" stroke="#2a2f3a" stroke-width="3.5" />
			<circle
				class="spin spin-out"
				cx="20" cy="20" r={R_OUT} fill="none" stroke="var(--accent)" stroke-width="3.5"
				stroke-linecap="round" stroke-dasharray="{C_OUT * 0.3} {C_OUT * 0.7}"
			/>
			<circle cx="20" cy="20" r={R_IN} fill="none" stroke="#2a2f3a" stroke-width="3" />
			<circle
				class="spin spin-in"
				cx="20" cy="20" r={R_IN} fill="none" stroke="var(--warn)" stroke-width="3"
				stroke-linecap="round" stroke-dasharray="{C_IN * 0.3} {C_IN * 0.7}"
			/>
		</svg>
	</span>
{:else if remainingMs === null}
	<span class="muted">—</span>
{:else if textOnly}
	<span class="poll-label" title="next git check">{fmt(remainingMs)} left</span>
{:else}
	<span class="poll-timer" title="next git check">
		<svg viewBox="0 0 40 40" width="88" height="88">
			<circle cx="20" cy="20" r={R_OUT} fill="none" stroke="#2a2f3a" stroke-width="3.5" />
			<circle
				cx="20" cy="20" r={R_OUT} fill="none" stroke="var(--accent)" stroke-width="3.5"
				stroke-linecap="round" stroke-dasharray={C_OUT}
				stroke-dashoffset={C_OUT * (1 - outerFrac)}
				transform="rotate(-90 20 20)"
			/>
			<circle cx="20" cy="20" r={R_IN} fill="none" stroke="#2a2f3a" stroke-width="3" />
			<circle
				cx="20" cy="20" r={R_IN} fill="none" stroke="var(--warn)" stroke-width="3"
				stroke-linecap="round" stroke-dasharray={C_IN}
				stroke-dashoffset={C_IN * (1 - innerFrac)}
				transform="rotate(-90 20 20)"
			/>
			<text x="20" y="21" text-anchor="middle" dominant-baseline="middle" class="wheel-minutes">
				{minutesLeft ?? ''}
			</text>
		</svg>
	</span>
{/if}

<style>
	.poll-timer {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
	}
	.poll-label {
		font-variant-numeric: tabular-nums;
		color: var(--text);
		font-size: 0.8rem;
		min-width: 4.2em;
	}
	.spin {
		transform-box: view-box;
		transform-origin: 20px 20px;
		animation: poll-spin 1s linear infinite;
	}
	.spin-in {
		animation-duration: 0.6s;
	}
	@keyframes poll-spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spin {
			animation-duration: 4s;
		}
		.spin-in {
			animation-duration: 3s;
		}
	}
	.wheel-minutes {
		fill: var(--muted);
		font-size: 9.75px; /* 25% smaller than before */
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
</style>
