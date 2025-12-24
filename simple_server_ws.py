#!/usr/bin/env python3
"""
WebSocket Proxy Server (Production)
Container → Here → Client (WebSocket) → Real LLM → Client → Here → Container
"""
import asyncio
import uuid
import json
from datetime import datetime
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
import uvicorn

def log(msg):
    """Log with timestamp (local time + UTC)"""
    local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    utc_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    print(f"[{local_time}][UTC {utc_time}] {msg}", flush=True)

class AdaptiveTimeout:
    """自适应超时管理器，基于历史平均的动态超时（类似 TCP RTO 算法）"""
    def __init__(self, initial_timeout=60.0, min_timeout=10.0, max_timeout=300.0):
        self.timeout = initial_timeout
        self.min_timeout = min_timeout
        self.max_timeout = max_timeout

        # 类似 TCP 的 SRTT (Smoothed Round Trip Time)
        self.smoothed_time = initial_timeout
        self.deviation = 0.0
        self.update_count = 0

    def update(self, actual_time: float):
        """根据实际耗时更新超时"""
        # 指数加权移动平均 (EWMA)
        alpha = 0.125  # 平滑因子
        beta = 0.25    # 偏差因子

        # 更新平滑时间
        self.smoothed_time = (1 - alpha) * self.smoothed_time + alpha * actual_time

        # 更新偏差
        self.deviation = (1 - beta) * self.deviation + beta * abs(actual_time - self.smoothed_time)

        # 计算新的超时 = 平均 + 4倍偏差 (类似 TCP RTO 算法)
        self.timeout = self.smoothed_time + 4 * self.deviation

        # 限制在合理范围内
        self.timeout = max(self.min_timeout, min(self.timeout, self.max_timeout))

        self.update_count += 1

    def get_timeout(self) -> float:
        """获取当前超时值"""
        return self.timeout

    def get_stats(self) -> dict:
        """获取统计信息（用于调试）"""
        return {
            "current_timeout": round(self.timeout, 2),
            "smoothed_time": round(self.smoothed_time, 2),
            "deviation": round(self.deviation, 2),
            "update_count": self.update_count
        }

app = FastAPI()

# Global variables
connected_client: Optional[WebSocket] = None  # Current connected Client
connected_client_addr: Optional[str] = None  # Address of current connected client
connected_client_job_id: Optional[str] = None  # Job ID of current connected client
pending_requests: Dict[str, dict] = {}  # request_id -> request_data
responses: Dict[str, dict] = {}  # request_id -> response_data
cancelled_requests: set = set()  # Cancelled request ID blacklist
rejected_connections: Dict[str, int] = {}  # IP -> count of rejections (for statistics)
last_stats_time: float = 0  # Last time we printed statistics
push_timeout_manager = AdaptiveTimeout(initial_timeout=60.0, min_timeout=10.0, max_timeout=300.0)  # 推送超时管理器

# ===== WebSocket Management =====

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, job_id: str = None):
    """WebSocket connection endpoint (only one Client allowed, requires valid job_id)"""
    global connected_client, connected_client_addr, connected_client_job_id, rejected_connections

    await websocket.accept()

    # Safe get client address (prevent websocket.client from being None)
    try:
        if websocket.client is not None:
            client_addr = f"{websocket.client.host}:{websocket.client.port}"
            client_ip = websocket.client.host
        else:
            client_addr = "Unknown"
            client_ip = "Unknown"
    except Exception:
        client_addr = "Unknown"
        client_ip = "Unknown"

    log(f"[Server] New client attempting connection: {client_addr} (job_id: {job_id or 'None'})")

    # Validate job_id
    if not job_id:
        log(f"[Server] ⛔ REJECT connection from {client_addr} - no job_id provided")
        try:
            await websocket.send_json({"type": "error", "message": "job_id parameter is required"})
            await websocket.close()
        except Exception:
            pass
        return

    # Verify job_id with eval_server (assumes eval_server is on localhost:8080)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "http://localhost:8080/internal/validate_job",
                params={"job_id": job_id}
            )
            if resp.status_code == 200:
                result = resp.json()
                if not result.get("valid"):
                    log(f"[Server] ⛔ REJECT connection from {client_addr} - invalid job_id: {job_id}")
                    try:
                        await websocket.send_json({"type": "error", "message": f"Invalid or expired job_id: {job_id}"})
                        await websocket.close()
                    except Exception:
                        pass
                    return
                # Job ID is valid, log job info
                log(f"[Server] ✓ Job validation passed: {job_id} (mode: {result.get('mode')})")
            else:
                log(f"[Server] ⛔ REJECT connection from {client_addr} - validation failed (HTTP {resp.status_code})")
                try:
                    await websocket.send_json({"type": "error", "message": "Job validation failed"})
                    await websocket.close()
                except Exception:
                    pass
                return
    except Exception as e:
        log(f"[Server] ⚠️  Warning: Could not validate job_id (eval_server unreachable): {e}")
        log(f"[Server] Allowing connection anyway (fallback mode)")
        # Allow connection if validation service is down (backward compatibility)

    # Check if there is already a Client connected
    if connected_client is not None:
        # Track rejected connection for statistics
        if client_ip != "Unknown":
            rejected_connections[client_ip] = rejected_connections.get(client_ip, 0) + 1

        log(f"[Server] ⛔ REJECT connection from {client_addr} - slot occupied by {connected_client_addr or 'Unknown'}")
        try:
            await websocket.send_json({"type": "error", "message": "Another client is already connected"})
            await websocket.close()
        except Exception:
            pass
        return

    connected_client = websocket
    connected_client_addr = client_addr
    connected_client_job_id = job_id  # Save job_id for logging
    log(f"[Server] ✓ ACCEPT connection from {client_addr} (job_id: {job_id}) - now serving this client")

    # Create two tasks
    task_handle_messages = None
    task_push_requests = None
    disconnect_reason = "unknown"

    try:
        # Use create_task instead of gather,这样可以更好地控制取消
        task_handle_messages = asyncio.create_task(handle_client_messages(websocket))
        task_push_requests = asyncio.create_task(push_requests_to_client(websocket))

        # Wait for any task to complete (usually because of connection closed)
        done, pending = await asyncio.wait(
            [task_handle_messages, task_push_requests],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel incomplete tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log(f"[Server] Error canceling task: {e}")

        # Check exceptions of completed tasks
        for task in done:
            try:
                task.result()
                disconnect_reason = "normal"
            except WebSocketDisconnect:
                disconnect_reason = "client_disconnect"
                log(f"[Server] 🔌 Client DISCONNECTED: {client_addr} (reason: client initiated)")
            except asyncio.TimeoutError:
                disconnect_reason = "timeout"
                log(f"[Server] 🔌 Client DISCONNECTED: {client_addr} (reason: timeout - no heartbeat)")
            except Exception as e:
                disconnect_reason = "error"
                log(f"[Server] 🔌 Client DISCONNECTED: {client_addr} (reason: error - {e})")

    except Exception as e:
        disconnect_reason = "exception"
        log(f"[Server] WebSocket error: {e}")
        import traceback
        log(f"[Server] Stack: {traceback.format_exc()}")
    finally:
        # Ensure cleanup (this will always execute)
        connected_client = None
        connected_client_addr = None
        connected_client_job_id = None  # Clear job_id on disconnect

        # Cancel all possible running tasks
        for task in [task_handle_messages, task_push_requests]:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except:
                    pass

        log(f"[Server] Cleanup completed for {client_addr}, slot now available (reason: {disconnect_reason})")

async def handle_client_messages(websocket: WebSocket):
    """Handle messages from Client (responses, heartbeats, etc.)"""
    import time
    while True:
        try:
            # Add timeout: must receive message within 90 seconds (heartbeat interval is 30 seconds, allow 2 missed)
            message = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=90.0
            )
            msg_type = message.get("type")

            if msg_type == "response":
                # Client returned response
                request_id = message.get("request_id")
                response_data = message.get("data")

                # Check if in cancelled list (blacklist)
                if request_id in cancelled_requests:
                    log(f"[Server] ⚠️  Ignored response for cancelled request: {request_id}")
                    cancelled_requests.discard(request_id)  # Remove from blacklist
                    continue  # Discard response, not process

                # Calculate client processing time
                req_info = pending_requests.get(request_id, {})
                queued_at = req_info.get("_queued_at")
                if queued_at:
                    client_processing_time = time.time() - queued_at
                else:
                    client_processing_time = None

                responses[request_id] = response_data

                if client_processing_time:
                    log(f"[Server] 📥 RESPONSE received from {connected_client_addr}: {request_id} (job: {connected_client_job_id}, status: {response_data.get('status_code')}, client time: {client_processing_time:.2f}s)")
                else:
                    log(f"[Server] 📥 RESPONSE received from {connected_client_addr}: {request_id} (job: {connected_client_job_id}, status: {response_data.get('status_code')})")

            elif msg_type == "heartbeat":
                # Heartbeat response, add send timeout protection (30 seconds to tolerate network congestion)
                try:
                    await asyncio.wait_for(
                        websocket.send_json({"type": "heartbeat_ack"}),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    log(f"[Server] Send heartbeat response timeout")
                    raise  # Throw exception to trigger cleanup

            else:
                log(f"[Server] Unknown message type: {msg_type}")

        except asyncio.TimeoutError:
            log(f"[Server] Receive message timeout (90 seconds no message), Client may be disconnected")
            raise  # Trigger connection cleanup
        except WebSocketDisconnect:
            log(f"[Server] handle_client_messages: Client主动断开")
            raise  # Propagate upwards
        except Exception as e:
            log(f"[Server] handle_client_messages error: {e}")
            import traceback
            log(f"[Server] Stack: {traceback.format_exc()}")
            raise  # Trigger cleanup

async def push_requests_to_client(websocket: WebSocket):
    """Continuously check queue, push new requests immediately to Client"""
    import time
    global last_stats_time, rejected_connections

    while True:
        try:
            # Print periodic statistics (every 60 seconds)
            current_time = time.time()
            if current_time - last_stats_time > 60:
                num_rejected_ips = len(rejected_connections)
                total_rejections = sum(rejected_connections.values())

                if total_rejections > 0:
                    log(f"[Server] 📊 STATUS: connected_client={connected_client_addr or 'None'}, job={connected_client_job_id or 'N/A'}, "
                        f"pending={len(pending_requests)}, responses={len(responses)}, "
                        f"rejected_in_60s={total_rejections} from {num_rejected_ips} IPs")

                    # Show top rejected IPs
                    if rejected_connections:
                        sorted_ips = sorted(rejected_connections.items(), key=lambda x: x[1], reverse=True)[:5]
                        top_rejections = ", ".join([f"{ip}({count})" for ip, count in sorted_ips])
                        log(f"[Server] ⚠️  Top rejected IPs: {top_rejections}")

                    # Clear rejection counters after reporting
                    rejected_connections.clear()
                else:
                    log(f"[Server] 📊 STATUS: connected_client={connected_client_addr or 'None'}, job={connected_client_job_id or 'N/A'}, "
                        f"pending={len(pending_requests)}, responses={len(responses)}")

                last_stats_time = current_time

            # Check if there are pending requests to push
            to_push = [rid for rid, req in pending_requests.items() if not req.get("pushed")]

            if to_push:
                # Batch push (maximum 100)
                batch = to_push[:100]
                requests_to_send = []

                for request_id in batch:
                    req_data = pending_requests[request_id]
                    req_data["pushed"] = True

                    # Calculate queue waiting time
                    queued_at = req_data.get("_queued_at")
                    if queued_at:
                        queue_time = time.time() - queued_at
                        req_data["_queue_time"] = queue_time

                    # Add push timestamp for diagnosis
                    req_data["_server_push_time"] = datetime.utcnow().isoformat()
                    requests_to_send.append(req_data)

                # Calculate average queue time for this batch
                avg_queue_time = sum(r.get("_queue_time", 0) for r in requests_to_send) / len(requests_to_send)

                # Get current adaptive timeout
                current_timeout = push_timeout_manager.get_timeout()
                timeout_stats = push_timeout_manager.get_stats()

                log(f"[Server] 📤 PUSH to client {connected_client_addr}: {len(requests_to_send)} request(s) {batch} (job: {connected_client_job_id}, avg queue time: {avg_queue_time:.3f}s, timeout: {current_timeout:.1f}s)")

                # Add send timeout protection with adaptive timeout
                try:
                    send_start = datetime.utcnow()
                    await asyncio.wait_for(
                        websocket.send_json({
                            "type": "new_requests",
                            "requests": requests_to_send
                        }),
                        timeout=current_timeout
                    )
                    send_duration = (datetime.utcnow() - send_start).total_seconds()

                    # Update adaptive timeout based on actual duration
                    push_timeout_manager.update(send_duration)

                    # Log with different levels based on duration
                    if send_duration > 10.0:  # If more than 10 seconds, record warning
                        log(f"[Server] ⚠️  SLOW PUSH: {send_duration:.3f}s for {len(requests_to_send)} requests (TCP flow control likely), next timeout: {push_timeout_manager.get_timeout():.1f}s")
                    elif send_duration > 1.0:  # If more than 1 second
                        log(f"[Server] Push duration: {send_duration:.3f}s (TCP send buffer), next timeout: {push_timeout_manager.get_timeout():.1f}s")
                    elif send_duration > 0.1:  # If more than 100ms
                        log(f"[Server] Push duration: {send_duration:.3f}s, next timeout: {push_timeout_manager.get_timeout():.1f}s")

                    # Periodically log timeout statistics (every 20 successful pushes)
                    if timeout_stats["update_count"] % 20 == 0 and timeout_stats["update_count"] > 0:
                        log(f"[Server] 📊 Adaptive timeout stats: {timeout_stats}")

                except asyncio.TimeoutError:
                    log(f"[Server] ❌ Push request TIMEOUT after {current_timeout:.1f}s")
                    # Update timeout manager with increased tolerance (1.5x current timeout)
                    push_timeout_manager.update(current_timeout * 1.5)
                    log(f"[Server] Increased timeout to {push_timeout_manager.get_timeout():.1f}s for next push")
                    raise  # Trigger cleanup

            await asyncio.sleep(0.1)  # Check every 100ms

        except asyncio.TimeoutError:
            log(f"[Server] push_requests_to_client: Send timeout")
            raise  # Trigger cleanup
        except WebSocketDisconnect:
            log(f"[Server] push_requests_to_client: Client disconnected")
            raise
        except Exception as e:
            log(f"[Server] push_requests_to_client error: {e}")
            import traceback
            log(f"[Server] Stack: {traceback.format_exc()}")
            raise

# ===== HTTP API =====

async def _handle_proxy_request(request: Request, request_data: dict, endpoint: str):
    """Common handler for proxy requests (both /chat/completions and /responses)"""
    import time
    request_id = f"req_{uuid.uuid4().hex[:8]}"
    request_start_time = time.time()

    # Extract model name if available
    model_name = request_data.get("model", "unknown")
    caller_ip = request.client.host if request.client else "unknown"

    log(f"[Server] 📨 NEW REQUEST: {request_id} (job: {connected_client_job_id or 'N/A'}, endpoint: {endpoint}, model: {model_name}, from container: {caller_ip}, will push to: {connected_client_addr or 'no client'})")

    # Check if there is a Client connected
    if connected_client is None:
        log(f"[Server] ❌ No available Client for {request_id}")
        return JSONResponse(
            content={
                "error": {
                    "message": "No client connected to proxy server",
                    "type": "service_unavailable",
                    "code": "no_client"
                }
            },
            status_code=503
        )

    # Add to pending queue with endpoint information
    # Only add _endpoint for non-default endpoints to maintain backward compatibility
    req_dict = {
        "request_id": request_id,
        "pushed": False,
        "_queued_at": time.time(),
        **request_data
    }
    # Only add _endpoint metadata if it's not the default /chat/completions
    # This maintains backward compatibility with old clients
    if endpoint != "/chat/completions":
        req_dict["_endpoint"] = endpoint

    pending_requests[request_id] = req_dict

    # Wait for response (maximum 10 minutes)
    for i in range(1200):  # 1200 * 0.5 = 600 秒
        # Check if the caller is disconnected (check every 10 seconds to reduce overhead)
        if i % 20 == 0:  # 20 * 0.5 = 10 秒
            if await request.is_disconnected():
                log(f"[Server] ⚠️  Caller disconnected {request_id}, stop waiting")
                pending_requests.pop(request_id, None)
                responses.pop(request_id, None)
                cancelled_requests.add(request_id)  # Add to cancelled blacklist
                # Do not return anything, connection is disconnected
                return

        # Check if client (websocket) is still connected
        if connected_client is None:
            log(f"[Server] ❌ Client disconnected, cancel {request_id}")
            pending_requests.pop(request_id, None)
            responses.pop(request_id, None)
            return JSONResponse(
                content={
                    "error": {
                        "message": "Client disconnected while processing request",
                        "type": "client_disconnected",
                        "code": "client_disconnect"
                    }
                },
                status_code=503
            )

        # Check if response is received
        if request_id in responses:
            resp_data = responses.pop(request_id)
            pending_requests.pop(request_id, None)  # Clean up

            total_latency = time.time() - request_start_time

            log(f"[Server] ✓ DELIVERED to container {caller_ip}: {request_id} (job: {connected_client_job_id or 'N/A'}, status: {resp_data.get('status_code')}, total latency: {total_latency:.2f}s, from client: {connected_client_addr or 'unknown'})")

            return JSONResponse(
                content=resp_data["body"],
                status_code=resp_data["status_code"]
            )

        await asyncio.sleep(0.5)

    # Timeout
    pending_requests.pop(request_id, None)
    log(f"[Server] ⏱️  Request TIMEOUT {request_id} (waited 600s)")
    return JSONResponse(
        content={
            "error": {
                "message": "Request timed out after 600 seconds (10 minutes)",
                "type": "timeout",
                "code": "timeout"
            }
        },
        status_code=504
    )

@app.post("/v1/chat/completions")
async def proxy_chat(request: Request, request_data: dict):
    """Proxy for OpenAI Chat Completions API"""
    return await _handle_proxy_request(request, request_data, "/chat/completions")

@app.post("/v1/responses")
async def proxy_responses(request: Request, request_data: dict):
    """Proxy for OpenAI Responses API"""
    return await _handle_proxy_request(request, request_data, "/responses")

@app.get("/")
async def root():
    """Health check with detailed status"""
    return {
        "service": "WebSocket Proxy Server",
        "status": "running",
        "client_connected": connected_client is not None,
        "connected_client_address": connected_client_addr,
        "connected_job_id": connected_client_job_id,
        "pending_requests": len(pending_requests),
        "pending_responses": len(responses),
        "cancelled_requests": len(cancelled_requests)
    }

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    print("="*50)
    print("WebSocket Proxy Server (Production)")
    print(f"Port: {port}")
    print(f"WebSocket: ws://0.0.0.0:{port}/ws")
    print("="*50)

    uvicorn.run(app, host="0.0.0.0", port=port)
