# Skrift Agent Demo

Realtime demo for Skrift's durable agent runtime using:

- Postgres for Skrift's primary database, worker archive, and DLQ records
- Redis for worker state, event log, queue, and notification fanout
- Gemini 3.1 Flash Lite through Pydantic AI
- Skrift time-series notifications for live browser updates

## Run

The repository root `.env` must contain `GEMINI_API_KEY`.

```bash
cd demo/agent-demo
docker-compose up --build
```

Open <http://localhost:8083>.

The Compose stack starts Postgres, Redis, migrations, the web process, an
out-of-process worker, and the worker persister.

## Chat states

The demo keeps a single durable agent session active in the browser.

- Sending the first message creates a session and queues the first turn.
- Sending while the agent is already queued or running records the message as a pending turn. The UI shows the pending count and the runtime activates the next message after the active turn finishes.
- Sending while a tool approval is pending cancels that approval, wakes the run, and queues the new message as the next turn.
- Sending after a failed or cancelled run revives the same session and continues with the committed conversation context.

Use the Audit Trail link after a session starts to inspect `UserMessageReceived`,
`UserMessageActivated`, tool, and terminal run events.
