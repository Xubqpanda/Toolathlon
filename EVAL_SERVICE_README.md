# Toolathlon Remote Evaluation Service

Run Toolathlon evaluations on a remote server with two operating modes: **public** (using APIs that are accessible on the Internet) and **private** (using your locally-deployed LLMs).

## Quick Overview

**Components:**
- `eval_server.py` - Server that runs evaluations
- `eval_client.py` - Client that submits tasks and retrieves results
- `simple_server_ws.py` - WebSocket proxy for private mode (auto-started by server)
- `simple_client_ws.py` - WebSocket client for private mode (auto-started by client)

**Key Features:**
- Two modes: Public (OpenAI/Anthropic/etc.) and Private (e.g. local vLLM/SGLang)
- Rate limiting: 3 tasks per IP per 24 hours
- Single concurrent task execution
- 240-minute timeout per task
- Real-time log streaming
- Background daemon operation

---

## Quick Start
**TLDR**: we have setup a public Toolathlon evaluation service (with all depencies installed and accounted configurated) on 47.253.6.47, if you want to test **your inhouse locally deployed model**, just simply put `eval_client.py` and `simple_client_ws.py` together under a folder on your own machine, then install the client-side dependencies:

```bash
pip install httpx typer websockets
```

Then run the following command directly under this folder:

```bash
python eval_client.py run \
  --mode private \
  --base-url http://localhost:8001/v1 \ # your local openai-compatible endpoint
  --api-key dummy \ # this argument will be ignored in private mode
  --model-name your-model-name \
  --workers 10 \ # suggested # of parallel workers
  --output-file ./results/eval_stats.json \ # any file path
  --log-file ./results/client.log \ # any file path
  --server-log ./results/server.log \ # any file path
  --traj-log ./results/traj_log_all.jsonl \ # any file path
  --server-host 47.253.6.47 \ # our public host address
  --server-port 8080 \ # our public port
  --ws-proxy-port 8081 # our public port
```
If the server is idle, your task will be submitted and you will find the results later on under the `./results` directory. Otherwise, please wait for a while and check again later via ``python eval_client.py check --server-host 47.253.6.47 --server-port 8080``.

If you have ready-to-use public API endpoind and API key, please use the public mode as follows:

```bash
python eval_client.py run \
  --mode public \
  --base-url your-puclic-endpoint \ # should be an openai-compatible endpoint as well
  --api-key sk-your-key \
  --model-name your-model-name \
  --workers 10 \
  --output-file ./results/eval_stats.json \
  --log-file ./results/client.log \
  --server-log ./results/server.log \
  --traj-log ./results/traj_log_all.jsonl \
  --server-host 47.253.6.47 \
  --server-port 8080
```
It will return the results exactly the same as in provate mode, we won't save your API keys locally.

If you meet any trouble, please feel free to contact us (jlini@cse.ust.hk / junxianh@cse.ust.hk), e.g. we may help you testing your model if provided with your public API endpoint and API key.

# Implemention Details

## Architecture

### Public Mode
```
Client → Server → OpenAI/Anthropic/etc. API
```
Client submits task with API credentials. Server runs evaluation using the public API.

### Private Mode
```
Client + Local LLM ←→ WebSocket ←→ Server
```
Server runs evaluation but forwards LLM requests back to client via WebSocket. Your LLM credentials never leave your machine.

**How Private Mode Works:**
1. Server starts `simple_server_ws.py` (WebSocket proxy on port 8081)
2. Client starts `simple_client_ws.py` (connects to proxy)
3. When server needs LLM inference, request flows: Server → WebSocket → Client → Your LLM
4. Response flows back: Your LLM → Client → WebSocket → Server

---

## Server Setup

### Prerequisites

Server requires full Toolathlon environment:

```bash
# Install dependencies
bash global_preparation/install_env_minimal.sh true

# Deploy local services (Canvas, email, etc.)
bash global_preparation/deploy_containers.sh true

# Install server dependencies
pip install fastapi uvicorn websockets
```

### Start Server

```bash
python eval_server.py <server_port> <ws_proxy_port>
```

**Default ports:**
- Server: 8080
- WebSocket proxy: 8081

**Example:**
```bash
python eval_server.py 8080 8081
```

Server output:
```
============================================================
Toolathlon Remote Evaluation Server
============================================================
Server Port: 8080
WebSocket Proxy Port: 8081 (for private mode)
Max tasks per IP: 3 per 24 hours
Timeout: 240 minutes
Output directory: ./dumps_public_service
============================================================
✓ WebSocket proxy started (PID: 12345)
  Log: ./dumps_public_service/ws_proxy.log
============================================================
```

---

## Client Setup

### Install Dependencies

```bash
pip install httpx typer websockets
```

### Usage

#### 1. Check Server Status

```bash
python eval_client.py check \
  --server-host <host> \
  --server-port 8080
```

**Output (idle):**
```
✓ Server is idle and ready to accept tasks
```

**Output (busy):**
```
⏳ Server is currently busy
   Job ID: job_ab*****56
   Mode: public
   Started: 2025-11-28T10:30:45.123456

Please try again later.
```

#### 2. Submit Public Mode Task

Use when you have an OpenAI-compatible API key:

```bash
python eval_client.py run \
  --mode public \
  --base-url https://api.openai.com/v1 \
  --api-key sk-your-key \
  --model-name gpt-5.1 \
  --workers 10 \
  --output-file ./results/eval_stats.json \
  --log-file ./results/client.log \
  --server-log ./results/server.log \
  --traj-log ./results/traj_log_all.jsonl \
  --server-host <host> \
  --server-port <server-port>, default 8080
```

**Parameters:**
- `--mode`: `public` or `private`
- `--base-url`: API endpoint URL
- `--api-key`: Your API key (optional for some providers)
- `--model-name`: Model identifier
- `--workers`: Number of parallel task workers (default: 10)
- `--output-file`: Where to save eval_stats.json
- `--log-file`: Client-side log file
- `--server-log`: Server-side log file (synced in real-time)
- `--traj-log`: Trajectory log file (optional, saves traj_log_all.jsonl)
- `--job-id`: Custom job ID (optional, for resuming tasks)

#### 3. Submit Private Mode Task

Use when you want to use your local LLM:

```bash
python eval_client.py run \
  --mode private \
  --base-url http://localhost:8001/v1 \
  --api-key dummy \ # this argument will be ignored in private mode
  --model-name your-model-name \
  --workers 10 \
  --output-file ./results/eval_stats.json \
  --log-file ./results/client.log \
  --server-log ./results/server.log \
  --traj-log ./results/traj_log_all.jsonl \
  --server-host <host> \
  --server-port <server-port>, default 8080 \
  --ws-proxy-port <websocket-proxy-port>, default 8081
```

*Note: private mode is designed for locallly deployed LLMs without API key, but if you set environment variables like OPENAI_API_KEY=xxx in your client machine, you can also use private mode to test a public model.

**What happens:**
1. Client submits task to server
2. Server accepts and starts evaluation
3. Client automatically starts background WebSocket client
4. WebSocket client connects to server's proxy
5. Server forwards LLM requests via WebSocket
6. Client processes requests using your local LLM
7. Results flow back to server

#### 4. Monitor Progress

```bash
# Watch client log
tail -f ./results/client.log

# Watch server execution log (synced in real-time)
tail -f ./results/server.log
```

#### 5. Check Task Status

```bash
python eval_client.py status \
  --job-id <job_id> \
  --server-host <host> \
  --server-port 8080
```

#### 6. Cancel Running Task

```bash
python eval_client.py cancel <job_id> \
  --server-host <host> \
  --server-port 8080
```

This will:
- Kill the evaluation process
- Stop and remove all Docker containers
- Clean up server resources

---

## Output Files

When a task completes, you'll have:

1. **eval_stats.json** (`--output-file`)
   - Evaluation statistics and results
   - Contains pass/fail status for all tasks

2. **traj_log_all.jsonl** (`--traj-log`, optional)
   - Complete trajectory logs for all tasks
   - One JSON object per line, each representing one task

3. **client.log** (`--log-file`)
   - Client-side execution log with timestamps
   - Shows task submission, status polling, completion

4. **server.log** (`--server-log`)
   - Server-side execution log (synced in real-time)
   - Shows container deployment, parallel test execution

5. **ws_client.log** (private mode only)
   - WebSocket client log (in same directory as client.log)
   - Shows WebSocket connection status and request handling

---

## Server File Structure

```
./dumps_public_service/
├── ws_proxy.log                    # WebSocket proxy log
├── job_abc123def456/               # Job directory
│   ├── server_stdout.log           # Server execution log
│   ├── eval_stats.json             # Results (JSON)
│   ├── traj_log_all.jsonl          # Trajectory logs (JSONL)
│   └── finalpool/                  # Individual task outputs
│       ├── task-1/
│       ├── task-2/
│       └── ...
└── job_xyz789/
    └── ...
```

---

## Rate Limiting & Constraints

### Rate Limiting
- **Limit:** 3 tasks per IP per 24 hours
- **Enforcement:** Server-side, based on client IP
- **Error:** HTTP 429 with retry timestamp

**Example error:**
```json
{
  "detail": "Rate limit exceeded: 3 tasks per 24 hours. Retry after 2025-11-29T10:30:00"
}
```

### Single Task Execution
- Server processes **one task at a time**
- No queueing system
- If busy, submission returns HTTP 503

### Timeout
- **Limit:** 240 minutes (4 hours)
- **Behavior:** Server kills process, cleans up containers
- **Client:** Receives timeout status

---

## Privacy & Security

### Public Mode
- ⚠️ API keys are sent to server, but we do not store them
- 💡 Use HTTPS in production to protect credentials
- Server uses your API key to call LLM directly

### Private Mode
- ✓ LLM URL and API key stay on client
- ✓ Server never sees your credentials
- ✓ Only inference requests/responses transmitted
- Server's `TOOLATHLON_OPENAI_BASE_URL` points to local WebSocket proxy

### Job ID Anonymization
When checking server status, job IDs are anonymized:
- `job_abc123def456` → `job_ab*****56`
- First 6 and last 2 characters shown
- Protects running task privacy

---

## Troubleshooting

### Cannot connect to server
```
❌ Cannot connect to server at http://host:8080
```
**Solutions:**
- Verify server is running: `python eval_server.py 8080 8081`
- Check firewall allows port 8080
- Verify host/port are correct

### Server busy
```
❌ Task submission failed:
   Server is currently processing another task. Please try again later.
```
**Solutions:**
- Wait for current task to complete
- Check status: `python eval_client.py check`
- If stuck, ask server admin to investigate

### Rate limit exceeded
```
❌ Rate limit exceeded: 3 tasks per 24 hours. Retry after 2025-11-29T10:30:00
```
**Solutions:**
- Wait until retry time
- Contact server admin if urgent
- Use different IP/network if appropriate

### WebSocket connection failed (private mode)
```
[Client] Fail to connect: ...
```
**Solutions:**
- Verify WebSocket proxy port (default 8081) is accessible
- Check `ws_proxy.log` on server for errors
- Ensure `simple_server_ws.py` is running

### Task timeout
```
ERROR: Task exceeded 240 minutes timeout
```
**Solutions:**
- Reduce `--workers` to avoid API rate limits
- Use faster model
- Contact server admin to increase timeout if needed

---

## Advanced Usage

### Custom Job ID (Resume Tasks)

```bash
python eval_client.py run \
  --mode public/private \
  --job-id my-custom-job-id \
  ...
```

**Use cases:**
- Resume incomplete tasks
- Organize related runs

**Warning:** If job ID already exists, you'll see:
```
⚠️  WARNING: Job ID 'my-custom-job-id' already exists in the system.
   Only use the same job ID if you want to resume an incomplete task.
```

### Custom Ports

**Server:**
```bash
python eval_server.py 9000 9001
```

**Client:**
```bash
python eval_client.py run \
  --server-port 9000 \
  --ws-proxy-port 9001 \
  ...
```