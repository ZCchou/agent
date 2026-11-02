"""Final allowance crosses real localhost HTTP, compiler, tools and persisted audit."""
import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_tool_continuation_retry import configuration, fixture, list_reply, start


@contextmanager
def coding_endpoint(*, partial_tool=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            if len(requests) == 1:
                delta, finish = list_reply("inspect"), "tool_calls"
            elif len(requests) == 2:
                delta, finish = {"tool_calls": [{"index": 0, "id": "write-once", "type": "function",
                    "function": {"name": "fs_propose_changes", "arguments": json.dumps({
                        "title": "Create synthetic source", "mutations": [{"kind": "create_text",
                            "path": "result.py", "content": "print('done')\n"}]})}}]}, "tool_calls"
            elif partial_tool:
                delta, finish = {"tool_calls": [{"index": 0, "id": "never-execute", "type": "function",
                    "function": {"name": "fs_propose_changes", "arguments": '{"title":"PRIVATE_PARTIAL'}}]}, "length"
            elif payload["max_tokens"] >= 16_384:
                delta, finish = {"content": "Created result.py."}, "stop"
            else:
                delta, finish = {"content": "Synthetic unfinished coding answer"}, "length"
            body = ("data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
                    + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("window,ceiling,allowance,status", [
    (1_048_576, None, 16_384, "completed"),
    (65_536, None, 16_384, "completed"),
    (1_048_576, 2_048, 2_048, "failed"),
    (1_048_576, 128, 128, "failed"),
    (32_768, None, 4_096, "failed"),
    (8_192, None, 2_048, "failed"),
    (None, None, 2_048, "failed"),
])
def test_final_allowance_reaches_http_and_matches_every_snapshot_and_frozen_spec(
        tmp_path, window, ceiling, allowance, status):
    # Reverting final resolution to the legacy compiler default truncates this
    # coding completion after completed tools; no provider replay can repair it.
    with coding_endpoint() as (base_url, requests):
        async def scenario():
            writes = []
            events = []
            async def sink(event):
                events.append(event)
                if event["event_type"] != "run.tool_requested":
                    return
                operation = event["data"]["operation"]
                service.tool_status(operation["id"], "executing")
                if operation["tool_name"] == "fs.propose_changes":
                    mutation = operation["arguments"]["mutations"][0]
                    target = tmp_path / mutation["path"]
                    with target.open("x", encoding="utf-8") as output:
                        output.write(mutation["content"])
                    writes.append(operation["id"])
                    result = '{"changed_files":1,"path":"result.py"}'
                else:
                    result = '{"entries":[]}'
                service.resolve_tool(operation["id"], True, result)
            value = configuration()
            value["base_url"] = base_url
            value["model_capabilities"]["model"].update(context_length=window, max_output_tokens=ceiling)
            repository, _, work, service = await fixture(tmp_path, sink, value)
            run_id = await start(service, work)
            run = repository.get_model_run(run_id)
            assert run["status"] == status, (run["error_code"], [p["max_tokens"] for p in requests])
            rounds = 4 if status == "failed" else 3
            assert len(requests) == rounds, "length termination permits one text-only closing call, never a tool replay"
            if status == "failed": assert not requests[-1].get("tools")
            assert [p["max_tokens"] for p in requests] == [allowance] * rounds
            assert len(writes) == 1 and (tmp_path / "result.py").read_text() == "print('done')\n"
            assert [(op["tool_name"], op["status"]) for op in service.list_tool_operations(run_id)] == [
                ("fs.list", "completed"), ("fs.propose_changes", "completed")]
            assert any(m.get("role") == "tool" and "changed_files" in m["content"]
                       for m in requests[2]["messages"])
            with repository._connect() as connection:
                snapshots = list(connection.execute(
                    "SELECT output_reserve FROM context_snapshots WHERE run_id=? ORDER BY round_no", (run_id,)))
                specs = list(connection.execute(
                    "SELECT generation_json,status FROM invocation_specs WHERE plan_id=? ORDER BY round_no",
                    (run["execution_plan_id"],)))
            assert [row[0] for row in snapshots] == [max(512, allowance)] * rounds
            assert [json.loads(row[0])["max_output_tokens"] for row in specs] == [allowance] * rounds
            assert [row[1] for row in specs] == ["completed", "completed", status] + (["failed"] if status == "failed" else [])
            if status == "failed":
                assert run["error_code"] == "provider_response_incomplete"
                assert not any(e["event_type"] == "run.completed" for e in events)
            else:
                assert run["assistant_message"]["content"] == "Created result.py."
        asyncio.run(scenario())


def test_generous_final_allowance_never_executes_length_terminated_partial_call(tmp_path, caplog):
    with coding_endpoint(partial_tool=True) as (base_url, requests):
        async def scenario():
            async def sink(event):
                if event["event_type"] == "run.tool_requested":
                    operation = event["data"]["operation"]
                    assert operation["provider_call_id"] != "never-execute"
                    service.tool_status(operation["id"], "executing")
                    service.resolve_tool(operation["id"], True, '{"completed":true}')
            value = configuration()
            value["base_url"] = base_url
            value["model_capabilities"]["model"].update(context_length=1_048_576)
            repository, _, work, service = await fixture(tmp_path, sink, value)
            run_id = await start(service, work)
            run = repository.get_model_run(run_id)
            assert run["status"] == "failed" and run["error_code"] == "provider_response_incomplete"
            assert len(requests) == 4 and requests[-1]["max_tokens"] == 16_384
            assert not requests[-1].get("tools")
            assert "系统执行说明" in run["draft"]["content"]
            assert len(service.list_tool_operations(run_id)) == 2
            assert "PRIVATE_PARTIAL" not in caplog.text
        asyncio.run(scenario())
