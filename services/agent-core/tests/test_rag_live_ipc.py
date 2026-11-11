import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


def test_core_stdin_accepts_a_redirected_file_without_final_newline(tmp_path):
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps({"v": 23, "id": "file-input", "type": "ping", "params": {}}), encoding="utf-8")
    environment = {**os.environ, "AGENT_DATA_DIR": str(tmp_path / "data"),
                   "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    with input_path.open("rb") as source:
        result = subprocess.run([sys.executable, "-m", "agent.main"], env=environment, stdin=source,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "file-input"


def test_live_core_stdin_does_not_block_first_onnx_import(tmp_path):
    from test_rag_models import model_directory

    model = model_directory(tmp_path / "中文 模型")
    (model / "model.safetensors").unlink()
    (model / "broken.onnx").write_bytes(b"invalid graph")
    (model / "rag-model.json").write_text(json.dumps({
        "schema_version": 1, "backend": "onnx", "profile": "bge-m3", "dimension": 1024,
        "pooling": "cls", "max_length": 512, "graph": "broken.onnx", "output": "last_hidden_state",
    }), encoding="utf-8")
    environment = {**os.environ, "AGENT_DATA_DIR": str(tmp_path / "应用 数据"),
                   "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    responses = queue.Queue()
    with (tmp_path / "stderr.log").open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen([sys.executable, "-m", "agent.main"], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8")
        def receive():
            for line in process.stdout:
                responses.put(json.loads(line))
        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        try:
            request = {"v": 23, "id": "live-import", "type": "rag.request", "params": {
                "action": "model.inspect", "params": {"path": str(model), "profile": "bge-m3"}}}
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            try:
                response = responses.get(timeout=10)
            except queue.Empty:
                raise AssertionError("First ONNX import blocked while the IPC stdin pipe remained open") from None
            assert response["id"] == "live-import"
            assert response["result"]["valid"] is False
            assert "Invalid ONNX" in " ".join(response["result"]["errors"])
            assert process.poll() is None
            process.stdin.write(json.dumps({"v": 23, "id": "still-live", "type": "ping", "params": {}}) + "\r\n")
            process.stdin.flush()
            assert responses.get(timeout=5)["id"] == "still-live"
            process.stdin.write(json.dumps({"v": 23, "id": "final-frame", "type": "ping", "params": {}}))
            process.stdin.flush()
            process.stdin.close()
            assert responses.get(timeout=5)["id"] == "final-frame"
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=5)
            process.stdout.close()
