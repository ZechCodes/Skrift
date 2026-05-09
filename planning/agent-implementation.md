# Skrift Agents — Design Outline (v8)

A handoff spec for the coding agent. This document describes **what needs to exist** and the **shape** of each component for Skrift's agent subsystem.

This spec assumes the worker subsystem (specified separately) exists. Some primitives required by this spec do not yet exist in the worker subsystem; they are listed in §0 (Phase 0 Decisions). **Phase 0 must land before Phase 1 of this spec begins.**

---

## 0. Phase 0 Decisions

These are locked in. Implementation begins after they are honored.

### 0.1 DLQ finalization mechanism: per-handler `on_dead` callback

The worker subsystem must provide a per-handler terminal callback:

```python
@skrift.handler("agents.run", queue="agents")
async def agents_run_handler(payload, context): ...

@agents_run_handler.on_dead
async def agents_run_dead(entry: DeadJobEntry):
    # Runs as its own worker job (with its own retries/durability).
```

The callback runs as its own worker job after the original job lands in DLQ.

### 0.2 Idempotent `EventLog.append` with conflict detection

`EventLog.append(stream, event)` must be idempotent on `event["event_id"]`:

- If no entry with that `event_id` exists in the stream → append, return new position.
- If an entry with that `event_id` exists and its payload is structurally equal to the new event → return the existing entry's position; do not duplicate.
- If an entry with that `event_id` exists and its payload differs → raise `EventIdConflict`.

Silent acceptance of same-id-different-payload writes would hide event-id collision bugs. Conflict detection makes those bugs loud at write time.

### 0.3 Caller-supplied job ids; idempotent `submit` with conflict detection

`Queue.submit(job, *, job_id=None)` must accept caller-supplied `job_id`:

- If no job with that id exists → submit, return new handle.
- If a job with that id exists in any state and its envelope is structurally equal → return the existing handle.
- If the envelopes differ → raise `JobIdConflict`.

Same reasoning as §0.2.

### 0.4 Atomic `StateStore.update` is mandatory for RunState

The agent subsystem requires `StateStore.update(key, fn)` to be atomic across all backends. Direct `StateStore.set` of RunState is forbidden. Phase 0 includes a contention test confirming atomicity on every supported backend.

### 0.5 Per-stream archive retention overrides

Streams configured at registration with `null` retention are never pruned. Streams without explicit registration fall back to global `archive_event_ttl`.

### 0.6 Logical event envelope preservation

Backends preserve dict structure on `EventLog.append` and `read` faithfully — no key reordering, no field-name normalization, no value-type coercion. Phase 0 includes structural-fidelity tests.

### 0.7 StateStore key listing

`StateStore.keys(prefix=...)` is required for the outbox reconciler (§5.7). The worker contract already specifies this; Phase 0 verifies it on every backend.

---

## 1. Scope and framing

Skrift Agents is a stateful, resumable, durable-by-default execution model for Pydantic AI agents.

Design goals:
- **Transparency** — the user writes Pydantic AI; durability and orchestration are invisible.
- **Auditability** — every run produces a complete, untruncated record (§15).
- **Composability** — multi-turn, mid-run steering, sub-agents, and HITL share the same RunState/event substrate.
- **Crash safety** — every cross-backend operation is recoverable through the outbox protocol (§5.7).

The agent runner is registered as a worker handler under `agents.run`. Calling `agent.run()` submits a job; a worker picks it up; the runner drives Pydantic AI's iteration.

---

## 2. The Agent class

`skrift.Agent` subclasses `pydantic_ai.Agent`. It adds:

- A required `name` for routing — global agent registry maps name → definition. Modules listed in `workers.imports`.
- Tool decorator overrides that capture **policy metadata** (§3) at registration time.
- A required deps factory if `deps_type` is set (§4).
- A `.run(*, dispatch="queued")` method that submits an `agents.run` worker job and returns a `Session` (§6).

The class registers itself in the registry at import time.

---

## 3. Tool policy

The tool decorator accepts policy hints at registration:

- **`approval`** — boolean or callable producing a boolean from args.
- **`idempotent`** — boolean. Determines retry behavior on indeterminate failures (§7.5).
- **`detached`** — boolean. For v1, supported for `@agent.tool_plain` only:
  plain tools run as separate worker jobs (§7.4). `@agent.tool(detached=True)`
  raises `NotImplementedError` at registration because context tools require the
  rehydration contract tracked in §19.13.
- **`approval_on_retry`** — boolean.
- **`policy_description`** — string. Strongly recommended when `approval` is a callable (§15.3.1).

---

## 4. Deps factory and `ResumeContext`

Per-agent factory; receives `ResumeContext` (session id, optional tool-call id, identity metadata, secret references, host metadata), returns `deps` instance.

**Deps are never serialized.** Only metadata in `deps_ref` is durable. Foundation of secrets discipline (§15.10).

Mandatory if `deps_type` is set.

---

## 5. `RunState`

Persisted via worker `StateStore`. Periodically snapshotted to `Archive` via worker state snapshotter.

Fields:

- `session_id`, `agent_name`, `status`
- `version` — incremented every `update`
- `current_run_job_id` — null when no run in flight
- `current_tool_execution` — `ToolExecutionState | None` (§5.4)
- `messages`, `pending_approvals`, `pending_steers`
- `outbox` — list of `OutboxEntry` (§5.7)
- `last_seq` — last event sequence number allocated by RunState. The event log may briefly lag last_seq during drain; idempotent append ensures convergence.
- `cursor`
- `deps_ref`
- `parent_session_id`, `root_session_id`
- `started_at`, `paused_at`, `terminal_at` — timestamps; null until applicable
- `schema_version`, `created_at`, `last_active_at`

**RunState is not the audit record.** It is *resumable working state*. Audit lives in the event log (§15).

### 5.1 RunState vs. Pause continuation state

- **RunState (StateStore):** durable, admin-visible, source of truth.
- **Pause continuation state:** transient correlation tokens for one pause-and-resume cycle. Cleared on consumption.

### 5.2 Atomic mutation contract

All RunState mutations MUST go through `StateStore.update(key, fn)`. Direct `set()` is forbidden. Framework increments `version` on every update.

### 5.3 `current_run_job_id`

When a fresh `agents.run` job is submitted (initial `agent.run()`, multi-turn `send`, DLQ replay, manual resume):

1. Generate `new_job_id`.
2. Atomic-update: set `current_run_job_id = new_job_id`, append outbox entries (§5.7).
3. Drain.

Cleared on terminal exit. Stays set across pauses.

### 5.4 `current_tool_execution`

```python
class ToolExecutionState(BaseModel):
    tool_call_id: str
    tool_name: str
    args: dict
    started_at: datetime
    status: Literal["started", "executing", "completed", "errored"]
    idempotent: bool
    approval_on_retry: bool
    detached_tool_job_id: str | None
    result: Any | None       # set when status == "completed"
    error: dict | None       # set when status == "errored"
```

**Tool lifecycle (revised in v8 to fix duplicate-emission bug):**

`ToolCallStarted` is emitted at tool dispatch. Result/error events (`ToolCallCompleted` / `ToolCallErrored`) are emitted **at result-integration time**, not at tool-finish time. This collapses the two-step sequence into one and avoids the "crash between tool-finish and result-integration" race that v7 left open.

Synchronous tool flow:

1. Atomic-update: set `current_tool_execution(status="executing")`, outbox `ToolCallStarted` event.
2. Drain.
3. Execute tool body.
4. Atomic-update: write result/error into `current_tool_execution`, set status to `completed`/`errored`. **No event outboxed at this step.**
5. (Drain — outbox is empty for this step but we drain anyway as a discipline.)
6. Atomic-update on next runner step or in the same node-boundary update: append result to message history, outbox `ToolCallCompleted`/`ToolCallErrored` event, clear `current_tool_execution`.
7. Drain.

If we crash between steps 4 and 6, recovery sees `current_tool_execution.status` as `completed`/`errored` with the result populated. The runner then performs step 6: outbox the event with a fresh seq, append to history, clear. Because the event is emitted exactly once at step 6, never at step 4, the seq-based event_id is stable across crash recoveries. Idempotent append handles step-7 retries normally.

Detached tool flow (the `agents.tool_call` handler):

1. Drain parent's outbox.
2. Run tool body.
3. Atomic-update on parent's RunState: write result/error into `current_tool_execution`, set status. **No `ToolCallCompleted`/`ToolCallErrored` event yet.** Outbox a wake of `current_run_job_id`.
4. Drain.

The parent runner, on wake-up, sees `current_tool_execution.status == "completed"`, emits the event at integration time per the synchronous-flow steps 6-7 above.

### 5.5 `started_at`

Replaces "no prior `AgentStarted` event" detection.

- Null → fresh run; emit `AgentStarted` (via outbox), set `started_at`. Same atomic update.
- Set → resume; emit `AgentResumed` via outbox. Same atomic update.

### 5.6 RunState mutation patterns

**Pure state mutation:** single `update`, no outbox.

**Cross-backend mutation:** atomic-update writes state change *plus* outbox entries together; drain executes them.

### 5.7 The outbox protocol

The outbox is the durable record of cross-backend intent.

```python
class OutboxEvent(BaseModel):
    kind: Literal["event"] = "event"
    entry_id: str    # unique within session; deterministic where possible
    stream: str      # explicit target stream (supports cross-stream emissions, e.g. parent stream from sub-agent)
    event: dict      # full event payload, including event_id

class OutboxSubmit(BaseModel):
    kind: Literal["submit"] = "submit"
    entry_id: str
    job: dict        # JobEnvelope
    job_id: str      # caller-supplied, matches envelope's id

class OutboxWake(BaseModel):
    kind: Literal["wake"] = "wake"
    entry_id: str
    job_id: str
    resume_at: datetime | None = None

OutboxEntry = OutboxEvent | OutboxSubmit | OutboxWake
```

**Sequence allocation.** Event seq numbers are allocated at outbox-entry-creation time. The atomic update that adds an `OutboxEvent` to the outbox also advances `RunState.last_seq` to the seq used by the event. Multiple events in one atomic update consume sequential seqs. Event id is `f"{session_id}:{seq}:{event_type}"`, deterministic from RunState at creation time.

The event log lags RunState during drain, never the reverse. Crash before drain: same RunState produces same event_ids on retry; idempotent `EventLog.append` deduplicates.

**Drain procedure:**

```python
async def drain_outbox(session_id):
    while True:
        rs = await state_store.get(runstate_key(session_id))
        if not rs.outbox:
            return
        
        entry = rs.outbox[0]  # insertion order
        match entry.kind:
            case "event":
                await event_log.append(entry.stream, entry.event)
            case "submit":
                await queue.submit(entry.job, job_id=entry.job_id)
            case "wake":
                await wake(entry.job_id, resume_at=entry.resume_at)
        
        await state_store.update(
            runstate_key(session_id),
            lambda rs: rs.copy(update={"outbox": [e for e in rs.outbox if e.entry_id != entry.entry_id]}),
        )
```

**Ordering is enforced.** Drain processes entries in insertion order. Convention: when one atomic update creates multiple entries, **event before submit/wake** (so audit shows "user message received → run queued" not the reverse).

**When drain runs:**

- After any atomic-update that adds outbox entries.
- At the start of every runner pickup.
- At the start of every `Session` operation that reads RunState.
- Periodically by a reconciler process (§5.7.1).

**Bounded outbox:** `len(outbox) > outbox_max_entries` (default 100) raises. Indicates repeated drain failures or a programming error.

### 5.7.1 Outbox reconciler

The reconciler is a background process that drains outboxes for sessions whose normal operation didn't trigger a drain (e.g., the process holding the session crashed mid-drain).

**Discoverability mechanism:** uses `StateStore.keys(prefix="runstate:")` (worker primitive, §0.7) to enumerate RunStates. For each, atomically reads `outbox` length; if non-empty, runs `drain_outbox`. Runs at `agents.outbox_drain_reconciler_interval` seconds (default 60).

This is O(sessions) per pass. For deployments with very large session counts where this matters, a future optimization is a `pending_outbox_sessions` index maintained alongside RunState updates (§19.11). Not required for v1.

The reconciler is the safety net, not the primary drain trigger. In normal operation, drain happens immediately after each atomic update that adds outbox entries; the reconciler only catches up sessions whose drain was interrupted by process death.

---

## 6. `Session`

Returned by `Agent.run()` and by `skrift.session(id)`.

Methods: `send`, `steer`, `approve`, `reject`, `cancel`, `pause`, `resume`. All take `actor=` kwarg.

Properties: `status`, `messages`, `id`, `lineage`. Awaitable for typed result; async-iterable for events.

Every Session method that mutates RunState runs drain before returning. Reads also drain first.

The `actor` keyword captures identity. If omitted, framework reads from `contextvars.ContextVar` set by `skrift.set_actor(...)`. If neither is set on a call recording an actor-bearing event, `actor="unknown"` is recorded with a warning.

### 6.1 Cooperative cancellation

`Session.cancel()`:

1. Atomic-update: if `terminal_at` is None, set `status = "cancelled"`, outbox `AgentCancellationRequested` event. (If already terminal, return early.)
2. Drain.
3. If `current_run_job_id` is set, call `JobHandle(current_run_job_id).cancel()`.
   - **Returns True (job was queued, now removed):** atomic-update with terminal_at guard:
     ```
     if rs.terminal_at is None:
         rs.terminal_at = utcnow()
         rs.current_run_job_id = None
         outbox AgentCancelled event
     # else: the runner already finalized; nothing to do
     ```
     Drain.
   - **Returns False (already claimed/in flight):** the runner will see `status="cancelled"` at its next check pass and finalize itself. Session.cancel does nothing further.
4. If the run was paused (HITL or detached tool wait): atomic-update outboxes a wake of `current_run_job_id`. Drain.

**The terminal_at guard is what prevents double-finalization.** Event-id idempotency does not save us here because Session.cancel and the runner allocate seqs in separate atomic updates and would produce different event_ids. Instead, both finalization paths atomic-update with `terminal_at IS NULL` as the guard: whichever runs first wins; the other no-ops.

The runner's check pass (§7.1 step 2) uses the same guard.

---

## 7. The agent runner

Registered as worker handler under `agents.run`.

Per-job-claim:

1. Read `session_id` and `agent_name` from job payload.
2. **Drain the outbox** for this session.
3. Load RunState.
4. **Stale job id check:** if `context.job_id != rs.current_run_job_id`, drain and exit cleanly. (Late claims of superseded jobs do not run; see §7.7.)
5. Check `RunState.status` for terminal states (cancelled/completed/failed) → exit. (DLQ retry transitions failed→queued before re-submitting per §7.6, so retries are not blocked here.)
6. Look up agent definition.
7. Reconcile `current_tool_execution` if non-null (§7.5).
8. **If `started_at` is null:** atomic-update outboxes `AgentStarted` event (full agent definition snapshot per §15.3.1), sets `started_at`. **Else:** atomic-update outboxes `AgentResumed` event. Drain.
9. Build `ResumeContext`; call deps factory.
10. Drive Pydantic AI iteration, node by node.
11. At each node boundary, run the **runner check pass** (§7.1).
12. Translate iteration events into agent events via outbox + drain.
13. Enforce tool policy (§7.4, §7.5).
14. Atomic-update RunState after each node (cursor, last_seq, current_tool_execution as appropriate, outbox entries for events). Drain.
15. Return appropriately (§7.2).

### 7.1 The runner check pass

At every node boundary:

1. **Drain outbox.**
2. **Cancellation:** if `status == "cancelled"`, atomic-update with terminal_at guard:
   ```
   if rs.terminal_at is None:
       rs.terminal_at = utcnow()
       rs.current_run_job_id = None
       outbox AgentCancelled event
   ```
   Drain. Return.
3. **Pending steers** (only at `ModelRequestNode`): atomic-update drains `pending_steers` into message history, outboxes `SteerApplied` per consumed steer, clears queue.
4. **Time-slice limit:** if exceeded, return `Pause(state={"resume": "time_slice"}, resume_at=utcnow())`.

Cancellation > steering > time-slicing.

### 7.2 Returning from the runner

- Successful completion → atomic-update with terminal_at guard: outbox `AgentCompleted`, set status=completed, terminal_at, clear `current_run_job_id`. Drain. Return typed output.
- HITL pause → atomic-update + outbox `ToolCallAwaitingApproval` + status=awaiting_approval + record pending approval. Drain. Return `Pause(state={"awaiting_approval_id": tool_call_id})`.
- Detached tool wait → see §7.4.
- Time-slice yield → `Pause(state={"resume": "time_slice"}, resume_at=utcnow())`. No outbox; worker re-enqueue handles continuation.
- Manual operator pause → `Pause(state={"resume": "manual"})`. (Status was set to `paused` by `Session.pause()`, §12.)
- Permanent failure → raise `PermanentFailure`. Worker dead-letters; `on_dead` finalizes (§7.6).
- Transient errors → raise. Worker retry policy applies.

### 7.3 Cooperative pause for HITL

See §9.

### 7.4 Cooperative pause for detached plain tools

V1 supports this flow for `@agent.tool_plain(detached=True)`. Context tools
registered with `@agent.tool(detached=True)` are rejected at registration until
the RunContext rehydration contract in §19.13 is implemented. This preserves
the invariant that deps and request-local context are never serialized into
durable worker payloads or audit records.

Runner generates `tool_job_id`. Atomic-update:
- Set `current_tool_execution(status="executing", detached_tool_job_id=tool_job_id)`.
- Outbox `ToolCallDispatched` event.
- Outbox submit of `agents.tool_call` job with `job_id=tool_job_id`.

Drain. Return `Pause(state={"detached_tool_job_id": tool_job_id})`.

The `agents.tool_call` handler:

1. **Stale job id check:** if the dead/handler-claimed job id doesn't match the parent's `current_tool_execution.detached_tool_job_id`, drain and exit. (Late completions of superseded tool jobs are ignored.)
2. Drain parent's outbox.
3. Run the plain tool body using the stored tool name and JSON-like args.
4. Atomic-update on parent's RunState: write result/error into `current_tool_execution`, set status. Outbox a wake of parent's `current_run_job_id`. **No `ToolCallCompleted`/`ToolCallErrored` event here** — emitted by parent runner at integration time per §5.4.
5. Drain.

If `agents.tool_call` lands in DLQ, its `on_dead` callback runs the same stale-job-id check, then writes errored result into parent's `current_tool_execution` and outboxes the wake.

Context-tool dispatch requires reconstructing deps and a fresh RunContext in
the tool worker from durable metadata only; see §19.13.

### 7.5 Indeterminate state on resume

If `current_tool_execution` is non-null:

- **Status `completed`/`errored`:** atomic-update appends result to message history (idempotent: message id stable), outboxes `ToolCallCompleted`/`ToolCallErrored` event (this is the single emission point per the revised §5.4 lifecycle), clears `current_tool_execution`. Drain. Continue.
- **Status `started`/`executing` (no result recorded):** indeterminate.
  - `idempotent=True` → re-execute, replacing the execution state. Note: re-execution does not re-emit `ToolCallStarted`; the original is durable in the log.
  - `approval_on_retry=True` → synthesize a HITL pause.
  - Default fail-closed → atomic-update sets status=`errored` with reason `"indeterminate_after_reclaim"`, outboxes `ToolCallErrored` (single emission point). Drain. Continue with error in history.

For detached tools (`detached_tool_job_id` set) with status `executing`: query the queue's job-status API for the tool job. If still in flight, re-pause. If terminal but the parent didn't see the wake, reconcile from the tool job's recorded result/cause.

### 7.6 DLQ finalization

Wired via `@on_dead` (§0.1).

```python
@agents_run_handler.on_dead
async def agents_run_dead(entry: DeadJobEntry):
    session_id = entry.payload["session_id"]
    
    async def finalize(rs: RunState) -> RunState:
        # Stale job id: a late on_dead from a superseded job.
        if entry.job_id != rs.current_run_job_id:
            return rs
        
        # Already terminal (any state): no-op. Retry safety.
        if rs.terminal_at is not None:
            return rs
        
        rs.status = "failed"
        rs.terminal_at = utcnow()
        rs.current_run_job_id = None
        
        if rs.current_tool_execution and rs.current_tool_execution.status in ("started", "executing"):
            rs.current_tool_execution.status = "errored"
            rs.current_tool_execution.error = {
                "exception_type": entry.exception_type,
                "exception_message": entry.exception_message,
                "traceback": entry.traceback,
            }
        
        rs = append_outbox_event(rs, AgentFailed(
            cause=entry.cause,
            exception_type=entry.exception_type,
            exception_message=entry.exception_message,
            traceback=entry.traceback,
            failed_at=utcnow(),
        ))
        return rs
    
    await state_store.update(runstate_key(session_id), finalize)
    await drain_outbox(session_id)
```

Two guards together:
- **Stale job id check** prevents an old DLQ callback from finalizing a session that has already been retried (`current_run_job_id` will have been advanced to the new job).
- **`terminal_at` guard** prevents double-finalization across `on_dead` retries.

**Operator-triggered DLQ retry** of an `agents.run` entry:

1. Atomic-update: `status: "failed" → "queued"`, `terminal_at: <set> → null`, generate new `current_run_job_id`, outbox the new job submission.
2. Drain.

The runner picks up, sees `status = queued` and `started_at` non-null, emits `AgentResumed`, continues from cursor.

`agents.tool_call` `on_dead` is structurally identical: stale-job-id check (against `current_tool_execution.detached_tool_job_id`), terminal_at-equivalent guard (whether the tool's status has already been set to `completed` or `errored`), then writes errored result into parent's `current_tool_execution` and outboxes the wake.

### 7.7 The stale job id check

A general principle that applies in three places (§7 step 4, §7.4 step 1, §7.6):

When a worker handler or `on_dead` callback runs, it may be processing a job that has been *superseded* — the operator triggered a retry, multi-turn `send` ran, or some other path advanced `current_run_job_id` (or `current_tool_execution.detached_tool_job_id`) to a newer job. Late claims of the old job must not run, and late `on_dead` callbacks must not finalize.

The check: compare the handler's claimed `job_id` (or the DLQ entry's `job_id`) to the relevant id in RunState. If they don't match, the operation no-ops.

This invariant is critical once job ids are durable and visible across retries. Without it, the system is correct for the happy path but breaks under retry contention.

---

## 8. Agent events

Emitted via outbox entries that drain to **`EventLog.append`** — the durable, idempotent primitive (§0.2). Stream naming convention: `agents:run:{session_id}` for per-session streams.

`context.emit` is a worker-level convenience for handlers not using the outbox. The agents runner does not use `context.emit` for durable events; it uses the outbox protocol. Live subscribers (e.g., the streaming `Session` iterator) subscribe via the worker's `EventLog.subscribe`/tail mechanism.

Categories: run lifecycle, reasoning, tool calls, messages, steering, sub-agents, state. (Full list and payloads in §15.3.)

Common envelope (logical, see §15.11): `event_id`, `type`, `session_id`, `parent_session_id`, `seq`, `ts`, `schema_version`, `payload`.

### 8.1 Streaming deltas are advisory

Audit consumers MUST NOT reconstruct content from deltas. Use `*Completed` events.

### 8.2 Event id determinism

```
event_id = f"{session_id}:{seq}:{event_type}"
```

`seq` allocated at outbox-entry-creation time within the atomic update. Same RunState produces same `event_id` on retry; idempotent append no-ops the duplicate.

For events on cross-stream paths (e.g., `SubAgentCompleted` written to a parent stream from a child runner): seq is allocated in the **parent's** RunState, not the child's. The event_id namespace matches the stream's owner.

---

## 9. HITL flow

1. Runner detects approval-required tool. Atomic-update: add to `pending_approvals`, set status=`awaiting_approval`, outbox `ToolCallAwaitingApproval` event.
2. Drain. Return `Pause(state={"awaiting_approval_id": tool_call_id})`.
3. `Session.approve(tool_call_id, *, actor, note)`:
   - Atomic-update: validate, remove pending approval, outbox `ToolCallApproved` event AND wake of `current_run_job_id` (event before wake, per §5.7 ordering).
4. Drain.
5. Runner reclaims, drains outbox, reloads RunState, proceeds.

`Session.reject` emits `ToolCallRejected`; runner continues with rejection in history.

No `resume_at` on these pauses; approval is event-driven.

---

## 10. Multi-turn conversations

`Session.send(message, *, actor=None)`:

1. Validate session is resumable.
2. Generate `new_job_id`.
3. Atomic-update:
   - Append message to history.
   - Set `current_run_job_id = new_job_id`.
   - Set status = `queued`.
   - Outbox: `UserMessageReceived` event, then submit of new `agents.run` job (event before submit).
4. Drain.

Crash between 3 and 4: outbox persists; next drain executes. Idempotent submit → exactly one job.

---

## 11. Mid-run steering

`Session.steer(text, *, role="user", actor=None)` injects guidance for the next model call. Distinct from `send`.

### 11.1 Mechanism

1. Validate non-terminal.
2. Atomic-update: append `Steer(...)` to `pending_steers`, outbox `SteerInjected` event.
3. Drain.

Steers do NOT wake the run; applied at the next `ModelRequestNode` per the runner check pass.

### 11.2 Composition

Composes with HITL pauses, detached tool waits, time-slice yields, multi-turn boundaries, and manual pauses. Steers submitted during any pause apply on resume.

### 11.3 Edge cases

- **During cancellation flow:** cancellation check runs first; pending steers in cancelled sessions remain in RunState (forensic visibility) but are never consumed.
- **For terminal session:** rejected with clear error.
- **Multiple steers between model calls:** applied as separate messages in submission order.

### 11.4 Why not `send()`

`send` is "user takes a turn" (chat semantics). `steer` is "user adjusts guidance for the in-flight task" (agent-task semantics).

### 11.5 System prompt awareness

Agent system prompts should acknowledge `[steer]`-tagged messages can arrive mid-task. Framework prepends configurable prefix (default `[steer] `).

---

## 12. Manual pause and resume

`Session.pause(*, actor=None)` and `Session.resume(*, actor=None)` give operators explicit control independent of HITL approvals or detached tool waits. Useful for debugging, throttling, or holding a session while external state stabilizes.

### 12.1 `Session.pause`

Validates `status` is one of: `queued`, `running`, `awaiting_approval`. (Already-paused, terminal, and cancelled states reject.)

1. Atomic-update:
   - Save existing status as `_status_before_pause` (preserved so resume knows what to restore).
   - Set `status = "paused"`, `paused_at = utcnow()`.
   - Outbox `AgentPaused` event with `actor` and `prior_status`.
2. Drain.

Behavior thereafter depends on what the session was doing:

- **Was queued (job not yet claimed):** the job stays in the queue. When eventually claimed, the runner's stale-job-id check passes (job id matches), but the runner sees `status="paused"` and exits cleanly via the cancellation-style return path *for paused* — the worker subsystem re-parks the job on Pause without `resume_at`. (Implementation: the runner's check pass adds a "paused" branch parallel to the cancellation branch in §7.1; on `paused` it returns `Pause(state={"resume": "manual"})` rather than finalizing.)
- **Was running:** the runner sees `status="paused"` at the next check pass and returns `Pause(state={"resume": "manual"})`.
- **Was awaiting_approval:** already in a Pause; the status change is recorded but the existing Pause continues. On approval, `Session.approve`'s wake will resume it; the runner then sees `status="paused"`, returns Pause(manual). The session is effectively double-paused; resume must restore the awaiting_approval status.

### 12.2 `Session.resume`

Validates `status == "paused"`.

1. Atomic-update:
   - Restore status: if `_status_before_pause == "awaiting_approval"`, set `status = "awaiting_approval"`. Otherwise set `status = "queued"` (the runner will set it to `running` on pickup).
   - Clear `paused_at` and `_status_before_pause`.
   - Outbox `AgentResumed` event with `actor`.
   - **If `current_run_job_id` is set** (a parked job exists): outbox a wake of `current_run_job_id`.
   - **Else** (no parked job — pause happened before the runner ran the queued job, or the job somehow terminated during pause): generate a new `current_run_job_id` and outbox a fresh `agents.run` job submission. This recovery path is rare but covers the case where pause happened on a queued job that was reclaimed/reaped during the pause.
2. Drain.

### 12.3 Idempotency

Both `pause` and `resume` are idempotent against retry of the same operation: pausing an already-paused session no-ops; resuming a non-paused session raises. The atomic-update's status-precondition check provides the guard.

### 12.4 Composition with cancellation

`Session.cancel()` on a paused session works as in §6.1: the wake outboxed for the cancel reaches the parked runner, which sees `status="cancelled"` (not `paused` — cancel updated it) and finalizes. Cancel takes precedence over pause.

---

## 13. Lineage

Tracked in RunState, not worker job metadata.

### 13.1 Sub-agent dispatch

When a tool body calls another agent's `.run()`:

1. Runner sets `contextvars.ContextVar` with current session id.
2. Submitting agent reads var, generates `child_session_id` and `child_job_id`. Atomic-update on child's RunState: record `parent_session_id`/`root_session_id`, outbox child's job submission.
3. Drain child's outbox.
4. Atomic-update on parent's RunState: outbox `SubAgentDispatched` event (on parent's stream).
5. Drain parent's outbox.

When child terminates (its runner reaches a terminal state), the child's runner emits `SubAgentCompleted` to the **parent's stream** via an `OutboxEvent` on the parent's RunState (using the explicit `stream` field). The seq is allocated in the parent's `last_seq` so the event id namespace matches the parent's stream.

**Default dispatch is `queued`.** `same_worker` is opt-in.

When `same_worker` is used with a non-idempotent tool, the framework emits a runtime log warning the first time per `(tool_name, parent_session_id)` per process.

---

## 14. Replay

`replay(session_id, until=...)` reconstructs RunState by reading events from Archive. Read-only; distinct from audit export (§15.8).

---

## 15. Audit trail

The agent subsystem produces a complete, untruncated record of every run.

### 15.1 What the audit trail captures

Every:
- **System prompt and agent definition** at run start (`AgentStarted`)
- **User message** (via `send` and `steer`)
- **Assistant reasoning** as exposed by the model/provider via Pydantic AI
- **Tool call** with full args
- **Tool result** with full return value
- **Tool error** with full traceback
- **HITL request** (tool name, args, requesting context)
- **HITL decision** (approval or rejection, with actor identity and full note/reason)
- **Steer** (full text, role, actor) — both injection and application
- **Manual pause/resume** (with actor)
- **Cancellation** (with actor)
- **Sub-agent dispatch and completion** (referenced by session id)
- **Run terminal state** (completed output or full failure context)

If it influenced or recorded a decision in the run, it is in the audit trail with its full value.

**Reasoning caveat:** "Reasoning" means whatever the model/provider exposes via Pydantic AI. Some models do not expose chain-of-thought; for those, `ReasoningCompleted` events will not be emitted. The audit captures what's exposed.

### 15.2 Full-value commitment

Full-value preservation, no truncation. Every event payload preserves the complete value.

Forbidden: truncating tool results, summarizing reasoning, dropping fields from tool args, eliding rejection reasons or approval notes.

Permitted: compressing payloads transparently, content-addressed offload (§15.12) for individual oversized fields, configuring offload thresholds.

### 15.3 Required event payloads

Minimum contract. Backends and event versions may add fields; they may not omit these. Any field may be replaced by a `BlobRef` (§15.12) if oversized.

| Event | Required fields |
|-------|-----------------|
| `AgentStarted` | `agent_name`, `agent_definition` (§15.3.1), `input_message` (full), `actor`, `parent_session_id`, `root_session_id`, `dispatch_kind` |
| `AgentResumed` | `resumed_at`, `prior_status`, `actor` (if manual) |
| `AgentPaused` | `paused_at`, `prior_status`, `actor` |
| `UserMessageReceived` | `message` (full), `actor`, `turn_index` |
| `AssistantMessageCompleted` | `message` (full), `model` |
| `ReasoningCompleted` | `text` (full), `model` |
| `ToolCallStarted` | `tool_call_id`, `tool_name`, `args` (full) |
| `ToolCallExecuting` | `tool_call_id` |
| `ToolCallCompleted` | `tool_call_id`, `result` (full), `duration_ms` |
| `ToolCallErrored` | `tool_call_id`, `exception_type`, `exception_message`, `traceback` (full), `duration_ms` |
| `ToolCallDispatched` | `tool_call_id`, `tool_name`, `args` (full), `tool_job_id` |
| `ToolCallAwaitingApproval` | `tool_call_id`, `tool_name`, `args` (full), `requesting_context` |
| `ToolCallApproved` | `tool_call_id`, `actor`, `note` (full or null), `decided_at` |
| `ToolCallRejected` | `tool_call_id`, `actor`, `reason` (full), `decided_at` |
| `SteerInjected` | `steer_id`, `text` (full), `role`, `actor`, `submitted_at` |
| `SteerApplied` | `steer_id`, `applied_at`, `position_in_history` |
| `SubAgentDispatched` | `child_session_id`, `child_agent_name`, `dispatch_kind`, `parent_tool_call_id` |
| `SubAgentCompleted` | `child_session_id`, `terminal_status`, `child_terminal_at` |
| `AgentCancellationRequested` | `actor`, `requested_at` |
| `AgentCancelled` | `cancelled_at`, `reached_from_status` |
| `AgentCompleted` | `output` (full), `completed_at` |
| `AgentFailed` | `cause`, `exception_type`, `exception_message`, `traceback` (full), `failed_at` |

Streaming delta events (`ReasoningDelta`, `AssistantMessageDelta`) are advisory and not part of the audit contract.

### 15.3.1 Agent definition snapshot

`agent_definition` field of `AgentStarted` carries:

- `model_id` — string
- `system_prompt` — full text (subject to offload)
- `output_type_schema` — JSON schema of output type
- `tools` — list of `ToolDefinitionSnapshot`:
  - `name`, `description`, `parameters_schema`
  - `policy` — `ToolPolicySnapshot`:
    - `idempotent`, `detached`, `approval_on_retry` — booleans
    - `policy_description` — operator-supplied description
    - `approval` — one of: `false`, `true`, `{"kind": "callable", "qualified_name": "module:func"}`, `{"kind": "callable", "source": "<inspect.getsource()>"}`, `{"kind": "callable", "unknown": true}`

Framework strongly recommends `policy_description` for callable predicates; emits a startup warning if registered without one.

### 15.4 Identity capture

Operator-actioned events carry the actor responsible. Required for: `UserMessageReceived`, `ToolCallApproved`, `ToolCallRejected`, `SteerInjected`, `AgentCancellationRequested`, `AgentStarted`, `AgentPaused` (manual), `AgentResumed` (manual).

Capture order:
1. `actor=` kwarg on Session method
2. `contextvars.ContextVar` set by `skrift.set_actor(...)`
3. `actor="unknown"` with logged warning

Actor structure: `{"kind": "user" | "service" | "unknown", "id": "..."}`. Hosts may add fields.

### 15.5 Streaming deltas advisory

Audit consumers MUST NOT reconstruct content from deltas. Use `*Completed` events.

### 15.6 Sub-agent activity

Parent's stream references children by session id via `SubAgentDispatched` and `SubAgentCompleted`. Child's events live on `agents:run:{child_session_id}`.

`audit_export(session_id, *, include_lineage=True)` walks lineage. Two output shapes: flat (chronological across all sessions) and nested (parent timeline with children embedded). Default is flat.

### 15.7 Retention

Audit retention is independent of generic event retention. Default: never prune (`null`). Configurable via `agents.audit.retention_seconds`. Depends on per-stream retention overrides (§0.5).

### 15.8 `audit_export` API

```
skrift.audit_export(session_id, *, include_lineage=True, format="flat") -> AuditTrail
```

`AuditTrail` model:
- `session_id`, `agent_name`, `started_at`, `terminal_at`, `terminal_status`
- `lineage` (parent_session_id, root_session_id, included_session_ids)
- `events` — fully-typed event models in canonical order, with `BlobRef`s dereferenced
- `actors` — deduplicated actors that touched the session
- `tools_called` — deduplicated tool names with counts
- `export_metadata` — export timestamp, exporter version, retention status of source streams

Read-only, side-effect-free.

### 15.9 Worker subsystem dependencies

Locked in via §0:

1. Per-handler `on_dead` callback (§0.1)
2. Idempotent `EventLog.append` with conflict detection (§0.2)
3. Caller-supplied job ids; idempotent `submit` with conflict detection (§0.3)
4. Atomic `StateStore.update` (§0.4)
5. Per-stream archive retention overrides (§0.5)
6. Logical event envelope preservation (§0.6)
7. `StateStore.keys(prefix=...)` (§0.7)

### 15.10 What is not in the audit; secrets and PII

Not included:
- The `deps` object's runtime values (only `deps_ref` metadata is durable). **Secrets must travel through deps, never as tool args.**
- Internal worker scheduling details — those live on worker streams.
- Time-slice yields and routine in-process pause/resume cycles.

Tool authors are responsible for keeping secrets out of args, results, and reasoning. A tool that takes an API key as an arg writes that key into the audit. The framework cannot prevent this; the discipline is documentation-level.

PII in tool results, user messages, and assistant outputs is captured in full. Host applications must comply with their own privacy policies. The framework provides hooks for at-rest encryption (BlobStore wrappers, payload encryption per backend); redaction-on-export filters are post-v1.

If a regulatory regime requires evidence of *who could have accessed* a value (key custody for `deps` secrets), that evidence lives in the host application's secret-management system.

### 15.11 Stream entry format

Every agent event becomes one entry on its session's stream. Wire format: **logical envelope + payload**. Backends preserve the dict structure faithfully; physical storage layout is a backend choice.

**Logical envelope** (always present):
- `event_id`, `type`, `session_id`, `parent_session_id`, `seq`, `ts`, `schema_version`

**Payload field:**
- `payload` — type-specific data per §15.3, subject to field-level offload (§15.12).

**Concrete shapes:**
- Redis Streams: envelope as separate stream fields, payload as a JSON-string field. Recommended.
- SQLAlchemy Archive: one row per event with envelope columns and a JSONB `payload` column, OR all-in-one JSONB column. Either satisfies the contract.
- In-memory: Pydantic model instances; envelope and payload as model attributes.

The agent subsystem reads via `EventLog.read(stream, ...)` returning `list[tuple[int, dict]]`. The dict has envelope keys at top level and `payload` as a sub-dict.

### 15.12 Field-level offload

When a single payload field's serialized size exceeds the threshold, the field is replaced by a `BlobRef`. The whole event is not offloaded; only the oversized field.

**`BlobRef` shape:**

```json
{
  "_offload": true,
  "blob_id": "<id>",
  "hash": "sha256:<digest>",
  "size": <bytes>,
  "content_type": "<mime>"
}
```

`_offload: true` discriminates a BlobRef from a regular value. Fields whose typed schema accepts both are typed as a union.

**Threshold:**
- Configured globally via `agents.audit.large_value_threshold_bytes` (default 256KB).
- EventLog backends may declare `max_inline_size`. Effective: `min(operator_configured, backend_capability)`.
- Recommended: in-memory unlimited, SQLAlchemy 1MB, Redis 64KB.

**What gets offloaded:**
- Each top-level payload field checked independently.
- Field's serialized JSON exceeds threshold → replaced by `BlobRef`.
- Lists with large items offloaded as a whole list.
- Nested structures not recursively offloaded.

**Reconstruction:**
- `audit_export`, `replay`, streaming Session iterator transparently dereference BlobRefs.
- Direct stream consumers see BlobRef as-is.
- On dereference, consumer verifies bytes match stored hash; mismatch raises `BlobIntegrityError`.

### 15.13 BlobStore backend interface

```python
class BlobRef(BaseModel):
    blob_id: str
    hash: str
    size: int
    content_type: str = "application/octet-stream"

class BlobStore(Protocol):
    async def put(self, value: bytes, *, content_type: str = "application/octet-stream") -> BlobRef: ...
    async def get(self, blob_ref: BlobRef) -> bytes: ...
    async def exists(self, blob_ref: BlobRef) -> bool: ...
    async def delete(self, blob_ref: BlobRef) -> None: ...
```

`put` is content-addressed: same bytes → same `blob_id`. Repeated put of identical bytes is idempotent.

**Required implementations:** `InMemoryBlobStore`, `ArchiveBlobStore` (rides the worker Archive with a dedicated `agents:blobs:*` stream).

**Optional:** `S3CompatibleBlobStore`.

**Configuration:** `agents.blob_backend` import string; defaults per preset.

**Retention coupling:** blobs must outlive their referencing events. Null audit retention → automatic. Finite retention requires future blob-GC pass (§19.8); v1 ships without GC.

**Hash discipline:** `sha256` everywhere, with algorithm prefix (`"sha256:..."`).

---

## 16. User-facing surface

```
skrift.Agent
skrift.session(id)
skrift.ResumeContext
skrift.replay(id)
skrift.audit_export(id, *, include_lineage=True, format="flat")
skrift.set_actor(actor)
skrift.Steer
skrift.BlobRef
```

`Session` exposes: `send`, `steer`, `approve`, `reject`, `cancel`, `pause`, `resume`; properties `status`, `messages`, `id`, `lineage`; `__aiter__`, `__await__`. All operator-action methods accept `actor=`.

---

## 17. Configuration

Under `agents:` in `app.yaml`:

- `default_queue` — `agents.run` queue name (default `agents`)
- `tool_call_queue` — `agents.tool_call` queue name (default same as `default_queue`)
- `time_slice_max_nodes`, `time_slice_max_seconds` — runner time-slice thresholds
- `state_snapshot_interval` — RunState snapshot frequency
- `default_subagent_dispatch` — `queued` (default for v1) or `same_worker`
- `steer_prefix` — default `[steer] `
- `audit.retention_seconds` — null (never prune) or seconds
- `audit.large_value_threshold_bytes` — default 262144 (256KB)
- `blob_backend` — import string; defaults per preset
- `outbox_drain_reconciler_interval` — seconds between reconciler passes (default 60; 0 to disable)
- `outbox_max_entries` — max outbox size before raising (default 100)

Effective offload threshold: `min(audit.large_value_threshold_bytes, eventlog_backend.max_inline_size)`.

---

## 18. Runtime CLI

- `skrift agents list`
- `skrift agents trace SESSION_ID`
- `skrift agents replay SESSION_ID`
- `skrift agents audit export SESSION_ID [--lineage/--no-lineage] [--format flat|nested] [--out PATH]`
- `skrift agents sessions inspect SESSION_ID`
- `skrift agents sessions cancel SESSION_ID`
- `skrift agents sessions pause SESSION_ID`
- `skrift agents sessions resume SESSION_ID`
- `skrift agents sessions steer SESSION_ID --message "..."`
- `skrift agents drain SESSION_ID` — manual outbox drain (diagnostics)

CLI commands that record actions capture actor as `{"kind": "service", "id": "cli:<user>"}`; `--actor` overrides.

---

## 19. Open design questions

Resolved (traceability):

- ~~Agent discovery~~
- ~~In-process sub-agents~~
- ~~Long-running tools~~
- ~~Cancellation semantics~~
- ~~Audit completeness~~
- ~~Audit offload backend~~
- ~~Stream entry wire format~~
- ~~DLQ finalization~~ (`on_dead`)
- ~~Concurrent RunState writes~~
- ~~`current_run_job_id`~~
- ~~Send/submit atomicity~~
- ~~Event idempotency~~
- ~~Tool execution emitted-state markers~~
- ~~Stable job ids~~
- ~~`same_worker` warning~~
- ~~Runner terminal vs. DLQ retry~~
- ~~Sequence allocation timing~~
- ~~Outbox stream targeting~~
- ~~DLQ finalization idempotency on `failed`~~
- ~~Queued cancellation cleanup~~
- ~~`context.emit` vs. `EventLog.append`~~
- ~~Outbox ordering~~
- ~~Stale job id checks~~ (§7.7)
- ~~Duplicate logical event creation~~ (revised tool lifecycle, §5.4)
- ~~Cancellation race~~ (terminal_at guard, §6.1)
- ~~Same-id different-payload conflicts~~ (§0.2, §0.3)
- ~~Reconciler discoverability~~ (`StateStore.keys`, §5.7.1)
- ~~Manual pause/resume semantics~~ (§12)

Still open:

1. **Session id ownership.** Auto-generated vs. user-supplied. Lean: support both — auto if omitted, user-supplied scoped under tenant/user prefix.
2. **RunState migration.** `schema_version` exists; migration registry behavior and unknown-version policy still need definition.
3. **Tenancy.** First-class `tenant_id` field vs. opaque metadata.
4. **Pydantic AI version pinning.** Supported version range and behavior on upstream API changes.
5. **Steering across lineage.** Lean: only the named session for v1.
6. **Steered message role default.** `user` vs. optional `system`.
7. **Audit export performance for long lineages.** v1 in-memory flat; large-export work tracked separately.
8. **Blob garbage collection.** v1 ships without GC; design accommodates it (event-id reference recording in BlobRef metadata is a candidate).
9. **Redaction-on-export.** Field-level redaction filters during `audit_export`. Post-v1.
10. **Callable approval predicate serialization** long-term path.
11. **Outbox reconciler scaling.** Currently O(sessions) via `keys` scan. A `pending_outbox_sessions` index would scale better. Watch in production; implement if scan latency becomes a problem.
12. **Awaiting-approval session paused-then-resumed.** §12.1 covers it but the double-pause/double-resume semantics are subtle. Watch in implementation.
13. **Detached context-tool rehydration.** `@agent.tool(detached=True)` needs a
    RunContext rehydration contract before it can be implemented safely. The
    design pass should read Pydantic AI's `RunContext` source and classify every
    field into one of four buckets:
    - **Rehydrated:** `deps`, rebuilt by calling the agent deps factory with
      `ResumeContext(session_id, tool_call_id, deps_ref, metadata=...)`.
    - **Frozen at dispatch:** stable metadata such as model id, retry attempt,
      and dispatched timestamp.
    - **Fresh:** worker-local state for the detached subtask, such as usage
      tracking.
    - **Excluded:** mid-conversation or request-local fields that would be stale
      or unsafe by the time the detached tool runs.

    Until that classification exists, context tools fail loudly at registration.

---

## 20. Suggested build order

### Phase 0: Worker-agent integration primitives (LOCKED IN; see §0)

**Worker subsystem changes:**
- `@handler.on_dead` callback (§0.1)
- Idempotent `EventLog.append` with conflict detection (§0.2)
- Caller-supplied `job_id` with idempotent `submit` and conflict detection (§0.3)
- Per-stream archive retention overrides (§0.5)
- Verify atomic `StateStore.update` across all backends (§0.4)
- Verify backend dict-structure fidelity (§0.6)
- Verify `StateStore.keys(prefix=...)` works on all backends (§0.7)

**Agents-side scaffolding:**
- RunState model with all fields including new pause-related fields (`paused_at`, `_status_before_pause`).
- Outbox protocol (§5.7) with deterministic seq allocation, explicit `stream` on event entries, insertion-order drain, reconciler.
- Test harness: simulated concurrent updates, simulated crashes between outbox population and drain, simulated duplicate `append` and `submit` (both same-content and different-content cases — verify the latter raises).

### Phase 1: Agent class + runner skeleton

`skrift.Agent`, registry, runner registered as `agents.run`. Handler that just logs and exits. Stale-job-id check in place from day one.

### Phase 2: RunState + atomic mutations + outbox

Runner does atomic-update + outbox + drain at every node boundary. Verify deliberately-killed worker can be replaced and run continues.

### Phase 3: Event emission with §15.11 envelope and §15.3 payloads

Agent events via outbox with deterministic event ids. Payloads conform to §15.3 from this phase. Crash tests verify no duplicates and no losses.

### Phase 4: Session abstraction

Awaitable + async-iterable. Backfill-then-live event subscription. Session reads drain first.

### Phase 5: Deps factories

### Phase 6: Tool policy and HITL

HITL via outbox per §9. Pending approvals in RunState. Idempotency tracking on retry.

### Phase 7: Cooperative cancellation

`Session.cancel()` per §6.1 — all three branches (queued direct-finalize, in-flight runner-finalize, paused wake-then-finalize) with terminal_at guard.

### Phase 8: DLQ finalization (`on_dead`)

`@on_dead` for `agents.run` per §7.6 — both stale-job-id check and terminal_at guard. Verify operator-triggered DLQ retry path.

### Phase 9: Multi-turn

`Session.send()` per §10 — outbox of UserMessageReceived event then submit, with new `current_run_job_id`.

### Phase 10: Mid-run steering

`Session.steer()` per §11 — pending steers in RunState, application at ModelRequestNode boundaries.

### Phase 11: Detached tools

`@tool_plain(detached=True)`. `agents.tool_call` worker handler with stale-job-id check. `@on_dead` for `agents.tool_call`. Revised lifecycle per §5.4 — tool result written to RunState, parent emits `ToolCallCompleted`/`Errored` at integration time. `@tool(detached=True)` remains gated on the context rehydration contract in §19.13.

### Phase 12: Manual pause/resume

`Session.pause()` and `Session.resume()` per §12. Test all status transitions including the awkward awaiting-approval pause case.

### Phase 13: Lineage and sub-agent events

Default `dispatch="queued"`. `SubAgentCompleted` written to parent's stream via explicit `stream` field on outbox event (§13.1, §8.2).

### Phase 14: Replay and snapshots

RunState periodic snapshot via worker state snapshotter. `skrift.replay` and CLI.

### Phase 15: Audit trail — offload, blob store, retention, export

§15.12 field-level offload. §15.13 BlobStore (`InMemoryBlobStore`, `ArchiveBlobStore`). Per-stream retention registration. `skrift.audit_export` API and CLI. Test suite verifying full-value contract end-to-end including offload-triggering payloads.

### Phase 16: Operability and migration

RunState `schema_version` migration registry. CLI session operations. Per-tenant queue routing if pursued. Audit export performance work if scale requires. Optional `S3CompatibleBlobStore` and blob GC. Redaction-on-export hooks if v1.1 work proceeds.

---

## Out of scope for this spec

- Worker subsystem internals — see Workers reference.
- Backend implementation details beyond the protocols specified.
- Pydantic AI's API surface — implementation detail for the runner.
- UI / SSE adapters.
- Authentication, authorization, secret management — host application concerns.
- Steering propagation across lineage (§19.5).
- Audit export at lineage scales beyond single-machine memory (§19.7).
- Blob garbage collection (§19.8).
- Redaction-on-export (§19.9).
