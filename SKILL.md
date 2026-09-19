---
name: locutus
description: "Multi-agent coordination, inter-terminal messaging bus, distributed file/mutex locking, orchestrating multi-stage DAG task pipelines, and worker queues over Redis. Use when coordinating work between multiple AI assistants or terminal sessions, acquiring distributed mutex locks before editing shared files or running migrations/deployments, dispatching tasks or RPC queries to peer agents, producing/consuming from competing-consumer work queues, orchestrating multi-stage pipelines with automatic dependency resolution, reliably claiming tasks with leases and ack/DLQ handling, scattering tasks to a pool for quorum aggregation, sharing scratchpad memory, managing floor control in roundtable brainstorming, setting and checking run cancellation tokens, running blind consensus ballots without anchoring bias, electing resilient mesh leaders with automated lease failover, or discovering active teammates and their status. Triggers: 'coordinate with the other terminal/agent', 'talk to agent', 'send task to', 'ask the other assistant', 'inter-agent chat', 'lock file', 'lock resource', 'mutex lock', 'prevent concurrent edits', 'work queue', 'enqueue task', 'claim task', 'ack task', 'reliable queue', 'dead letter queue', 'dlq', 'blackboard', 'scratchpad', 'shared memory', 'floor control', 'speaker ring', 'moderated roundtable', 'pass floor', 'yield floor', 'cancel task', 'cancel run', 'cancellation token', 'abort run', 'ballot', 'vote', 'consensus', 'blind voting', 'leader election', 'acquire leader', 'failover', 'mesh leader', 'scatter', 'gather', 'quorum', 'workflow', 'dag', 'pipeline', 'task dependencies', 'resolve step', 'workflow next', 'who is online', 'agent status', 'locutus', 'Redis bus'."
---

# Locutus: Redis Inter-Assistant Communication Bus

Locutus is a daemonless, high-performance inter-assistant communication protocol and CLI engine over Redis. It provides cryptographic HMAC-SHA256 authentication, air-gapped prompt-injection defense, and Redis `EVALSHA` caching with sub-millisecond execution.

---

## 1. Quick Reference & Core Invariants

1. **Native Single-Binary Engine**: All coordination is executed via the high-speed `locutus` binary (built in Nim with compile-time embedded Lua and EVALSHA caching).
2. **Namespace & Team Isolation**:
   - Keys use `$LOCUTUS_REDIS_PREFIX` (default: `locutus:`).
   - Agents are tagged with their project (`$LOCUTUS_PROJECT`).
   - Multicasts are **AND filters** across tags (`project,tag`). Global broadcasts use `*` or `@all`.
3. **Queue Architecture (Single Inbox per Agent)**:
   - Every agent listens to `${LOCUTUS_REDIS_PREFIX}inbox:<my_name>`.
   - Offline messages queue in Redis (7-day default TTL) and are delivered upon reconnect.
4. **Air-Gap Prompt-Injection Firewall**:
   - All messages require valid HMAC-SHA256 signatures derived from `~/.config/locutus/secret` (0600 mode).
   - `locutus listen` drops unauthenticated, forged, or tampered payloads at the process boundary before reaching stdout. The assistant never receives malicious prompts into its context window.
   - Optional E2EE: Setting `LOCUTUS_ENCRYPT=1` encrypts task bodies via OpenSSL AES-256-CBC PBKDF2 across Redis.
5. **Continuous Ear Invariant & Listening Strategy Selection**:
   - Once registered (`locutus open`), the assistant MUST maintain an active listener at all times until explicitly closed via `locutus close`.
   - **Zero Token Churn**: Locutus blocks silently in native code and internally renews its Redis heartbeat. It outputs 0 bytes and never wakes up the assistant until an authentic message arrives.
   - **No Shell Wrapping Loops**: NEVER wrap `locutus listen` in `while true`, `for`, or background bash scripts. The native binary handles indefinite waiting and heartbeats internally.
   - **Choose Your Strategy Based On Tool Capabilities**:
     Inspect your environment and available tool definitions to determine which listener pattern fits your runtime:
     - **Strategy A (Dedicated Ear Subagent)**: If your runtime equips you with tools to spawn background subagents/workers AND tools that send asynchronous messages or events back to the parent agent.
       *Action*: Spawn a single, dedicated "Ear" subagent whose sole job is to run `locutus listen <my-name>` in an endless loop and forward each received payload back to the parent.
     - **Strategy B (Atomic Piggybacked Re-Arm via `--listen`)**: If your runtime operates as a single-agent linear process, or if child agents cannot asynchronously message the parent.
       *Action*: Launch the initial listener in the background (`locutus listen`). Whenever concluding a task or forwarding work, couple the re-arm directly to your reply/send command:
        `locutus reply --to <sender> --subject "Re: <subj>" --body "<result>" --reply-to <id> --listen`
        Locutus delivers the reply, logs status to `stderr`, and seamlessly transitions the same running process into blocking on your inbox, delivering pure JSON on `stdout` when the next message arrives.
     - **Singleton Listener Invariant & Anti-Stacking Guard**:
       Locutus natively enforces a strict singleton listener per agent (`listener:<agent>`). If `--listen` is executed while an active listener is already running (e.g. an ongoing Ear subagent or prior background task), Locutus delivers the outbound message, logs to `stderr`, and **automatically skips listening** to prevent stacking duplicate background tasks or splitting inbox messages.
6. **Agent Identity & Host Isolation**:
   - Multiple assistants on the same computer are isolated via process environment (`export LOCUTUS_AGENT_NAME=<name>`) and workspace directory (`.locutus.agent`).
   - `locutus listen` requires an identifiable agent name (explicit argument, `LOCUTUS_AGENT_NAME`, or workspace `.locutus.agent`).
   - Active listeners that attach via `locutus listen <name>` are automatically registered into the live directory.
   - `locutus who` automatically prunes dead/expired agents upon query, returning only truly active agents.
7. **Distributed Concurrency & File/Resource Locking (`locutus lock` / `locutus unlock`)**:
   - When multiple assistants operate in parallel across terminals, workspaces, or machines, acquire a distributed lease (`locutus lock <lock_name> [ttl_sec]`) before modifying shared files, schema definitions, database state, git branches, or deployment targets.
   - Prevents race conditions, overwrite collisions, and merge conflicts. Always release the lock (`locutus unlock <lock_name>`) upon completing the critical section.

---

## 2. CLI Command Reference

Locutus auto-discovers Redis configuration from `LOCUTUS_REDIS_URL`, `AGENTS.md`, `.env`, or local defaults.

| Action | Command |
| :--- | :--- |
| **Register & Announce** | `locutus open [name] [tags]` |
| **Arm Background Listener** | `locutus listen [name] [timeout_sec] [--force/-f]` |
| **Send Direct Task (O2O)** | `locutus send --to <recipient> --subject "<subj>" --body "<body>" [--listen/-l]` |
| **Send Reply** | `locutus reply --to <sender> --subject "Re: <subj>" --body "<body>" [--reply-to <msg_id>] [--listen/-l]` |
| **Broadcast (O2M)** | `locutus broadcast --tags "<tags>" --subject "<subj>" --body "<body>"` |
| **Synchronous RPC** | `locutus request --to <recipient> --subject "<subj>" --body "<body>" [--timeout 30] [--raw]` |
| **Scatter-Gather Quorum** | `locutus scatter --targets <@tag\|agent1,agent2\|\*> --subject "<subj>" --body "<body>" [--quorum N] [--timeout sec] [--raw]` |
| **Produce to Work Queue** | `locutus enqueue <queue_name> --subject "<subj>" --body "<body>"` |
| **Consume from Work Queue** | `locutus work <queue_name> [timeout_sec]` |
| **Reliable Task Claim** | `locutus claim <queue_name> [timeout_sec] [--lease 120] [--raw]` |
| **Acknowledge Task** | `locutus ack <queue_name> <task_id>` |
| **Shared Blackboard / Scratchpad** | `locutus blackboard <set\|get\|append\|snapshot\|delete\|clear> <room> [key] [val]` |
| **Floor Control (Speaker Ring)** | `locutus floor <request\|yield\|pass\|status> <room> [args...]` |
| **Run Cancellation Token** | `locutus cancel <run_id> [--reason <reason>] \| check <run_id> \| clear <run_id>` |
| **Blind Voting & Ballot** | `locutus ballot <open\|cast\|tally\|status> <ballot_id> [args...]` |
| **Leader Election (Lease)** | `locutus leader <acquire\|renew\|resign\|status> <role> [args...]` |
| **Set Status & Activity** | `locutus status <idle\|busy\|error> [activity_text]` |
| **Distributed Mutex Lock** | `locutus lock <lock_name> [ttl_sec]` |
| **Distributed Mutex Unlock** | `locutus unlock <lock_name>` |
| **Ephemeral Pub/Sub Send** | `locutus pub <channel> "<message>"` |
| **Ephemeral Pub/Sub Recv** | `locutus sub <channel> [timeout_sec]` |
| **Discover Peers** | `locutus who [-a\|--all] [--json] [tag]` (e.g. `locutus who`, `locutus who -a`, `locutus who --json`) |
| **Dynamic Tags** | `locutus tag <add\|remove\|set> <tags>` |
| **Drain Backlog** | `locutus drain [count]` |
| **Unregister / Close** | `locutus close` |


---

## 3. Step-by-Step Lifecycle Guide for Assistants

### Step 1: Open Connection & Register
```bash
locutus open
# Or with specific identity:
locutus open my-agent-1 "backend,qa"
```
*Locutus prints the registration banner, isolates the agent in `.locutus.agent`, and drains any pre-existing messages from your inbox.*

> **Tip for Multi-Agent Host Isolation**:
> When running multiple agents across terminal tabs on the same computer, export your agent name in the shell to ensure complete process-level isolation:
> ```bash
> export LOCUTUS_AGENT_NAME="my-agent-1"
> ```

### Step 2: Maintain Continuous Listening (Choose Strategy A or Strategy B)

Never miss a message while busy executing tasks. Inspect your available tools and adopt the strategy matching your runtime capabilities:

#### Strategy A: Dedicated Ear Subagent (Asynchronous Multi-Agent Runtimes)
*Use this strategy if your environment provides tools to spawn subagents AND communicate/send messages back to the parent agent.*

1. **Spawn the Ear Subagent**:
   Immediately after `locutus open`, launch a dedicated background subagent with this explicit role and instruction:
   > **Role**: Locutus Ear / Bus Listener  
   > **Instructions**:
   > "You are the dedicated Locutus bus listener for agent '<my-name>'. Run this loop continuously:
   > 1. Execute `locutus listen <my-name>` (this blocks silently until an authentic message arrives).
   > 2. When `locutus listen` returns an incoming JSON payload, immediately forward that full message payload to the parent agent using your agent messaging tool.
   > 3. Immediately repeat step 1 to listen for the next message.
   > Do NOT attempt to execute tasks, write code, or edit files yourself. Your sole duty is listening and forwarding."
2. **Perpetual Bus Connection**:
   Because the Ear subagent has only one task, it never gets distracted by multi-turn coding, refactoring, or tool execution. It keeps an unbreakable ear on the bus, forwarding work to the parent agent reactively.

#### Strategy B: Piggybacked Re-Arm via `--listen` (Single-Agent / Linear Runtimes)
*Use this strategy if your environment operates as a single agent or lacks asynchronous subagent-to-parent messaging.*

In linear runtimes, assistants frequently drop background listeners during complex multi-step tasks. Locutus solves this via the `--listen` (`-l`) piggyback flag, coupling the re-arm directly to task completion:

1. **Initial Arming**:
   Launch the initial listener in the background:
   ```bash
   locutus listen
   ```
2. **Wakeup & Execution**:
   When a message arrives into your context, parse the payload and execute the requested work.
3. **Atomic Reply & Re-Arm**:
   When your work is done, send your reply using `locutus reply` with `--listen` in the background:
   ```bash
   locutus reply --to "<sender>" --subject "Re: <subject>" --body "<result>" --reply-to "<id>" --listen
   ```
   Or if sending a new task or query:
   ```bash
   locutus send --to "<recipient>" --subject "<subj>" --body "<body>" --listen
   ```
   **How It Works**:
   - Locutus sends the message and emits a status log to `stderr`.
   - In the exact same process, it seamlessly begins listening on your inbox.
   - When the next message arrives, the process exits cleanly with pure JSON on `stdout`.
   - Because LLMs naturally send a reply when concluding a task, piggybacking ensures the listener is never dropped.
   - **Anti-Stacking Guarantee**: If an active listener is already running (e.g. from an earlier call or an Ear subagent), `--listen` automatically delivers the message and exits `0` immediately without spawning a duplicate listener.

### Step 3: Advanced Coordination Protocols

#### A. Synchronous RPC (`locutus request`)
When you need an immediate answer or calculation from a specific peer before proceeding:
```bash
locutus request --to <peer> --subject "<query>" --body "<input>" [--timeout 30] [--raw]
```
- Dispatches the task with a dedicated ephemeral reply key (`reply:<req_id>`).
- Blocks on Redis `BRPOP` until the response arrives or timeout occurs.
- `--raw` flag outputs the bare decrypted body directly for shell piping.

#### B. Competing-Consumers Task Queues (`locutus enqueue` / `locutus work`)
When coordinating independent tasks across a pool of worker agents:
- **Producer**:
  ```bash
  locutus enqueue <queue_name> --subject "<title>" --body "<payload>"
  ```
- **Worker (Consumer)**:
  ```bash
  locutus work <queue_name>
  ```
  Guarantees exactly-once consumption across all competing workers. Defaults to indefinite blocking wait until a task is available.

#### C. Distributed Mutex & File Locking (`locutus lock` / `locutus unlock`)
When executing critical sections or editing shared resources that must not collide across parallel agents or terminal sessions (e.g. editing shared files, modifying schemas, git rebase/merge, database migrations, deployment pipelines):
```bash
# Acquire lease before editing a shared file (returns 0 on success, 1 on conflict):
locutus lock file:schema.prisma 60

# Or acquire lease for a deployment / critical operation:
locutus lock deploy:staging 120

# Perform safe edits or migration...

# Release lease immediately after completing the work (guarantees only owner can unlock):
locutus unlock file:schema.prisma
```

#### D. Agent Operational State & Activity Tracking (`locutus status`)
Keep teammates and coordinators informed of your current focus:
```bash
locutus status busy "Running full regression suite"
# When ready for new work:
locutus status idle "Awaiting next task"
```
Inspect peer states and activities cluster-wide via `locutus who "*"`.

#### E. Ephemeral Pub/Sub Streaming (`locutus pub` / `locutus sub`)
Broadcast transient announcements where persistence/queue backlog is unnecessary:
```bash
# Subscriber:
locutus sub alerts 10

# Publisher:
locutus pub alerts "Build completed"
```

#### F. Orchestrator Scatter-Gather & Quorum (`locutus scatter`)
Fan out an objective across a tag cluster or list of agents and gather replies until quorum is reached:
```bash
locutus scatter --targets @reviewers --subject "Review PR #42" --body "Diff ready" --quorum 2 --timeout 15
```

#### G. Reliable Task Leases & Dead-Letter Queue (`locutus claim` / `locutus ack`)
Non-destructively lease tasks from a queue with automatic retries and DLQ escalation on failure:
```bash
task=$(locutus claim batch_pipeline --lease 60)
# Process task...
locutus ack batch_pipeline <task_id>
```

#### H. Shared Blackboard & Room Scratchpad (`locutus blackboard`)
Shared persistent key-value and append-log memory for agent rooms:
```bash
locutus blackboard set design_room arch_spec '{"runtime": "nim"}'
locutus blackboard append design_room notes "Checked DB migrations"
snapshot=$(locutus blackboard snapshot design_room)
```

#### I. Floor Control & Speaker Ring (`locutus floor`)
Coordinate turn-taking and speaker turns in roundtable discussions:
```bash
# Request speaker lease (blocks if occupied):
locutus floor request design_room 30
# Yield when finished:
locutus floor yield design_room
# Or pass directly:
locutus floor pass design_room specialist_agent
```

#### J. Global Run Cancellation Tokens (`locutus cancel`)
Instantly abort runaway workflows and stop background workers from spending tokens:
```bash
# Cancel an entire workflow run:
locutus cancel run_42 --reason "Operator requested abort"

# In worker loops before expensive LLM calls or tool actions:
if locutus cancel check run_42 --exit-code; then
  echo "Run was cancelled! Aborting cleanly..."
  exit 0
fi
```

#### K. Blind Voting & Ballot Consensus (`locutus ballot`)
Prevent LLM sycophancy and anchoring bias in architectural decisions:
```bash
# 1. Open ballot with options:
locutus ballot open db_choice --options "postgres,sqlite,redis" --voters "arch,db_spec,sec_spec"

# 2. Voters cast blind votes (hidden until tally):
locutus ballot cast db_choice --vote "sqlite"

# 3. Tally votes and reveal winner:
locutus ballot tally db_choice --close
```

#### L. Leader Election via Lease Preemption (`locutus leader`)
Eliminate single points of failure with auto-failover coordinator leases:
```bash
# Attempt to acquire leader role (with 30s lease):
locutus leader acquire orchestrator 30

# Leader periodically renews lease in background:
locutus leader renew orchestrator 30

# Leader gracefully resigns when work is complete:
locutus leader resign orchestrator
```

#### M. Directed Acyclic Graph (DAG) Workflows (`locutus workflow`)
Coordinate complex multi-stage pipelines with automatic dependency resolution:
```bash
# 1. Define DAG pipeline:
locutus workflow define release_flow --steps "lint,test,build,deploy" --deps "test:lint;build:lint;deploy:test,build"

# 2. Query ready unblocked steps:
ready=$(locutus workflow next release_flow --raw)

# 3. Complete a step and automatically unlock downstream stages:
locutus workflow resolve release_flow lint --output "passed"
```

### Step 4: Graceful Exit
When the session ends or user asks to disconnect:
```bash
locutus close
```

---

## 4. Playbooks & Coordination Recipes

Minimal, production-ready recipes for common multi-agent workflows:

### Playbook 1: Safe Concurrent File Editing
*Goal: Prevent concurrent overwrites when multiple agents or terminals work in the same repo.*
```bash
# 1. Acquire 60s lease on target file (returns 0 on success, 1 on conflict):
locutus lock file:src/router.ts 60

# 2. Inspect, modify, test, or format the file safely...

# 3. Release lease immediately upon completion:
locutus unlock file:src/router.ts
```

### Playbook 2: Distributing Batch Tasks Across a Worker Pool
*Goal: Farm out independent sub-tasks across a pool of interchangeable worker assistants.*
```bash
# Producer (Orchestrator): Push tasks onto shared queue
locutus enqueue test_suite --subject "Run Unit Tests" --body "tests/auth_test.go"
locutus enqueue test_suite --subject "Run Integration Tests" --body "tests/api_test.go"

# Workers (Run concurrently across terminal tabs or subagents):
# Blocks silently until a task is available; guarantees exactly-once delivery:
task=$(locutus work test_suite)
```

### Playbook 3: Synchronous RPC Delegation (Ask a Specialist)
*Goal: Delegate a specialized calculation, schema review, or query and block for the clean result.*
```bash
# Caller: Dispatches task and blocks up to 30s; --raw outputs clean response body for piping
res=$(locutus request --to db-expert --subject "Query Plan" --body "SELECT * FROM users" --timeout 30 --raw)

# Specialist (Responder): Answers directly with locutus reply
locutus reply --to orchestrator --subject "Re: Query Plan" --body "Add composite index on (created_at, user_id)" --reply-to <req_id> --listen
```

### Playbook 4: Team Discovery & Live Focus Broadcasting
*Goal: Check active teammates before dispatching work, and broadcast current focus.*
```bash
# 1. Discover who is online across the project or cluster:
locutus who -a --json

# 2. Broadcast what you are actively working on:
locutus status busy "Refactoring auth middleware"

# 3. Signal completion when ready for new tasks:
locutus status idle "Awaiting next task"
```

### Playbook 5: Real-Time Event Fan-Out (Pub/Sub Telemetry)
*Goal: Broadcast transient events without saving backlog in Redis queues.*
```bash
# Subscriber: Wait up to 60s for event stream
locutus sub build_events 60

# Publisher: Broadcast event to all currently attached subscribers
locutus pub build_events '{"commit": "348d001", "status": "passed"}'
```

### Playbook 6: Unbreakable Background Ear Execution
*Goal: Keep an active ear on the bus without getting dropped during multi-turn coding.*
- **Strategy A (Subagent Runtimes)**: Launch a dedicated background ear subagent running `locutus listen <agent>` in a continuous loop, forwarding incoming message payloads to the parent agent.
- **Strategy B (Single-Agent Runtimes)**: Always append `--listen` (`-l`) to replies or sends:
  ```bash
  locutus reply --to orchestrator --subject "Done" --body "Merged PR" --listen
  ```
  Locutus automatically skips duplicate listeners if one is already active.

### Playbook 7: Orchestrator Scatter-Gather & Quorum Consensus
*Goal: Fan out an objective across a pool of specialists and aggregate responses until quorum is met.*
```bash
# Fan out to all agents with tag 'reviewers', waiting for at least 2 approvals:
replies=$(locutus scatter --targets @reviewers --subject "Review PR #42" --body "Please review diff in staging" --quorum 2 --timeout 15)

# Or fan out to explicit agents and pipe bare response bodies:
locutus scatter --targets "analyzer1,analyzer2" --subject "Benchmark" --body "run" --raw
```

### Playbook 8: Fault-Tolerant Worker Mesh with Leases & Dead-Letter Queue
*Goal: Ensure zero task loss even if a worker crashes or encounters an unhandled exception.*
```bash
# 1. Non-destructively claim task with a 120s lease:
task=$(locutus claim batch_pipeline --lease 120)

# 2. Extract task ID and payload:
task_id=$(echo "$task" | jq -r '.id')
payload=$(echo "$task" | jq -r '.body')

# 3. Process the task safely...

# 4. Confirm completion and clear lease:
locutus ack batch_pipeline "$task_id"

# Note: If the worker crashes mid-task, the lease expires after 120s and is automatically returned to the queue (or moved to dlq:batch_pipeline after 3 failed attempts).
```

### Playbook 9: Shared Blackboard & Roundtable Scratchpad
*Goal: Share persistent design specs and append idea logs without re-transmitting large contexts over chat.*
```bash
# Set shared architecture specification in room 'brainstorm':
locutus blackboard set brainstorm arch_spec '{"runtime": "nim", "crypto": "openssl_evp"}'

# Append ideas or action items to a shared list:
locutus blackboard append brainstorm ideas "Idea 1: Add monotonic fencing tokens to mutex locks"
locutus blackboard append brainstorm ideas "Idea 2: DAG-based workflow pipeline engine"

# Dump entire room scratchpad as clean structured JSON:
snapshot=$(locutus blackboard snapshot brainstorm)
```

### Playbook 10: Moderated Roundtable Discussion with Floor Control
*Goal: Coordinate turn-taking across multiple agents in a shared room without race conditions or talking over each other.*
```bash
# 1. Request the floor (with a 30s speaker lease). Blocks if someone else is speaking until granted or timeout:
locutus floor request design_review 30

# 2. Speak / write findings to the blackboard or broadcast to the room:
locutus blackboard append design_review notes "Speaker proposal: Split monolithic config into modular schemas"

# 3. Yield the floor to the next waiting speaker, or explicitly pass to a designated agent:
locutus floor yield design_review
# Or: locutus floor pass design_review architect_bob
```

### Playbook 11: Coordinated Run Cancellation Across Workers
*Goal: Instantly stop background tasks and prevent token burn when an objective is superseded or aborted.*
```bash
# Lead / Orchestrator: Publish cancellation token for active run
locutus cancel run_101 --reason "Requirements updated: pivoting to Redis streams"

# Workers: Check cancellation token before executing expensive tool steps
if locutus cancel check run_101 --exit-code; then
  echo "Run cancelled: $(locutus cancel check run_101 --raw). Halting execution."
  exit 0
fi

# Reset token when starting a clean re-run:
locutus cancel clear run_101
```

### Playbook 12: Blind Consensus Voting to Eliminate Anchoring Bias
*Goal: Collect independent votes from peer assistants without letting early votes anchor subsequent models.*
```bash
# 1. Lead opens ballot:
locutus ballot open arch_debate --options "monolith,microservices,modular_monolith" --voters "claude,gpt,gemini"

# 2. Each assistant independently casts their ballot (votes remain sealed):
locutus ballot cast arch_debate --vote "modular_monolith" --voter "claude"
locutus ballot cast arch_debate --vote "modular_monolith" --voter "gpt"
locutus ballot cast arch_debate --vote "monolith" --voter "gemini"

# 3. Lead tallies the votes and locks the ballot:
tally=$(locutus ballot tally arch_debate --close)
winner=$(echo "$tally" | jq -r '.winner')
echo "Consensus winner: $winner"
```

### Playbook 13: Self-Healing Leader Election & Automated Failover
*Goal: Maintain high availability for orchestrator roles without single points of failure.*
```bash
# 1. Primary candidate acquires leadership lease (30s TTL):
locutus leader acquire cluster_lead 30

# 2. While primary is healthy, periodically renew lease:
locutus leader renew cluster_lead 30

# 3. Standby candidates monitor leadership:
leader_status=$(locutus leader status cluster_lead)
# If primary crashes or drops lease, standby's acquire call automatically succeeds:
locutus leader acquire cluster_lead 30

# 4. Graceful handoff: primary resigns, instantly waking standbys:
locutus leader resign cluster_lead
```

### Playbook 14: DAG-Based Multi-Stage Workflow Pipeline
*Goal: Coordinate multi-stage task pipelines where dependent tasks automatically unlock as parent steps complete.*
```bash
# 1. Define pipeline graph: lint -> test, build; test & build -> deploy:
locutus workflow define release_pipeline \
  --steps "lint,test,build,deploy" \
  --deps "test:lint;build:lint;deploy:test,build"

# 2. Query ready steps (returns bare step names with --raw):
ready_steps=$(locutus workflow next release_pipeline --raw)
# => "lint"

# 3. Worker executes 'lint', then resolves it:
locutus workflow resolve release_pipeline lint --output "lint clean"
# Automatically unlocks dependent stages 'test' and 'build'

# 4. Check ready steps again:
ready_steps=$(locutus workflow next release_pipeline --raw)
# => "test"
#    "build"

# 5. Workers run 'test' and 'build' concurrently and resolve them:
locutus workflow resolve release_pipeline test --output "all tests green"
locutus workflow resolve release_pipeline build --output "artifacts packaged"
# 'deploy' is now unlocked because both dependencies ('test' and 'build') are resolved!

# 6. Worker runs deploy and resolves it:
locutus workflow resolve release_pipeline deploy --output "deployed to prod"
# Workflow status transitions to 'completed'
```

---

## 5. Fallback Modes

If the compiled `locutus` binary is not in PATH:
1. **Run via Nim directly**: `nim r src/locutus.nim [args...]`
2. **Build binary**: `nim c -d:release -o:~/.local/bin/locutus src/locutus.nim`
3. **Raw Lua Scripts**: Execute `redis-cli -u "$LOCUTUS_REDIS_URL" EVAL "$(cat "scripts/<script>.lua")" ...`
