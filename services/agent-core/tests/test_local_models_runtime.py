import asyncio
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from agent.storage import CoreDataError
from test_local_models_catalog import finish, service_at
from test_local_models_gguf import gguf_file


_SERVER = r'''
import argparse, json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
p = argparse.ArgumentParser()
p.add_argument('--port', type=int)
p.add_argument('--alias')
p.add_argument('--api-key-file')
p.add_argument('--log-verbosity', type=int, default=3)
args, unknown = p.parse_known_args()
token = open(args.api_key_file).read().strip()
mode = MODE
if mode == 'crash':
    print('invalid GGUF model', file=sys.stderr, flush=True)
    sys.exit(1)
if mode == 'driver':
    print('CUDA driver initialization failed', file=sys.stderr, flush=True)
    sys.exit(1)
if mode == 'slow':
    time.sleep(30)
if mode == 'gpu' and args.log_verbosity >= 4:
    print('using device CUDA0: test GPU', file=sys.stderr, flush=True)
    print('offloaded 2/2 layers to GPU', file=sys.stderr, flush=True)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer ' + token:
            self.send_response(401); self.end_headers(); return
        if self.path == '/health': value = {'status':'ok'}
        elif self.path == '/v1/models': value = {'data':[{'id': 'wrong' if mode == 'identity' else args.alias}]}
        elif self.path == '/props': value = {'total_slots':1}
        else: self.send_response(404); self.end_headers(); return
        data = json.dumps(value).encode()
        self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + token:
            self.send_response(401); self.end_headers(); return
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        data = json.dumps({'choices':[{'message':{'role':'assistant','content':'ready'},'finish_reason':'stop'}]}).encode()
        self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
HTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
'''


def fake_runtime(root, *, modes=None):
    root.mkdir()
    modes = modes or {"cpu": "normal"}
    engines = {}
    for backend, mode in modes.items():
        directory = root / backend
        directory.mkdir()
        script = _SERVER.replace("MODE", repr(mode))
        executable = directory / ("llama-server.exe" if os.name == "nt" else "llama-server")
        if os.name == "nt":
            import pip._vendor.distlib as distlib
            launcher = Path(distlib.__file__).parent / "t64.exe"
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("__main__.py", script)
            executable.write_bytes(launcher.read_bytes() + ('#!"' + sys.executable + '"\n').encode() + archive.getvalue())
        else:
            executable.write_text("#!" + sys.executable + "\n" + script)
            executable.chmod(0o700)
        license_file = directory / "LICENSE"
        license_file.write_text("fixture license")
        files = {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (executable, license_file)}
        engines[backend] = {"executable": str(executable.relative_to(root)).replace("\\", "/"), "files": files,
            "version": "b12110", "source_commit": "a" * 40, "build": {"platform": "test"}}
    manifest = {"schema_version": 1, "protocol_version": 1, "engines": engines,
                "licenses": [f"{backend}/LICENSE" for backend in engines]}
    (root / "runtime.json").write_text(json.dumps(manifest))
    return root


async def imported(service, path):
    assert (await finish(service, await service.handle("model.import", {"path": str(gguf_file(path))})))["status"] == "completed"
    return (await service.handle("model.list", {}))["models"][-1]


def test_manifest_verifies_entire_runtime_and_source_consistency(tmp_path):
    from agent.local_models.manifest import RuntimeManifest
    root = fake_runtime(tmp_path / "runtime", modes={"cpu": "normal", "vulkan": "normal"})
    manifest = RuntimeManifest(root)
    assert manifest.engine("cpu").executable == root / "cpu" / "llama-server.exe"
    (root / "vulkan" / "LICENSE").write_text("modified")
    with pytest.raises(CoreDataError) as failure:
        RuntimeManifest(root).engine("cpu")
    assert failure.value.code == "runtime_integrity"
    (root / "vulkan" / "LICENSE").write_text("fixture license")
    data = json.loads((root / "runtime.json").read_text())
    data["engines"]["vulkan"]["source_commit"] = "b" * 40
    (root / "runtime.json").write_text(json.dumps(data))
    with pytest.raises(CoreDataError):
        RuntimeManifest(root).engine("cpu")


def test_manifest_rejects_unlisted_executable_and_extra_dll(tmp_path):
    from agent.local_models.manifest import RuntimeManifest
    root = fake_runtime(tmp_path / "runtime")
    (root / "cpu" / "injected.dll").write_bytes(b"bad")
    with pytest.raises(CoreDataError) as failure:
        RuntimeManifest(root).engine("cpu")
    assert failure.value.code == "runtime_integrity"
    (root / "cpu" / "injected.dll").unlink()
    data = json.loads((root / "runtime.json").read_text())
    data["engines"]["cpu"]["files"].pop("cpu/llama-server.exe")
    (root / "runtime.json").write_text(json.dumps(data))
    with pytest.raises(CoreDataError):
        RuntimeManifest(root).engine("cpu")


def test_runtime_inventory_does_not_hash_or_probe_unverified_components(tmp_path, monkeypatch):
    from agent.local_models.manifest import RuntimeManifest
    root = fake_runtime(tmp_path / "runtime", modes={"cpu": "normal", "cuda": "gpu"})
    def forbid_hash(*args, **kwargs):
        raise AssertionError("Inventory must not trigger a full integrity scan")
    monkeypatch.setattr("agent.local_models.manifest.file_hash", forbid_hash)
    assert set(RuntimeManifest(root).inventory()) == {"cpu", "cuda"}


def test_authenticated_subprocess_load_profile_and_unload(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime"))
        model = await imported(service, tmp_path / "model.gguf")
        job = await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert job["status"] == "completed", job
        status = await service.handle("runtime.status", {})
        assert status["state"] == "loaded" and status["backend"] == "cpu" and status["session_id"]
        assert "api_key" not in json.dumps(status) and "http://" not in json.dumps(status)
        profile = service.get_profile(model["provider_profile_id"])
        assert profile.allowed_model_ids == (model["id"],)
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(profile.base_url + "/models")).status_code == 401
            response = await client.post(profile.base_url + "/chat/completions", headers={"Authorization": "Bearer " + profile.api_key},
                json={"model": model["id"], "messages": [{"role": "user", "content": "hello"}]})
            assert response.json()["choices"][0]["message"]["content"] == "ready"
        assert len(service.provider_profiles()) == 1
        process = service._runtime.process
        await service.handle("runtime.unload", {})
        assert process.poll() is not None and service.provider_profiles() == {}
        assert (await service.handle("runtime.status", {}))["state"] == "unloaded"
        await service.close()
    asyncio.run(exercise())


def test_serial_invocation_permits_busy_remove_and_switch_wait(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime"))
        model = await imported(service, tmp_path / "model.gguf")
        await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        second_entered = asyncio.Event()
        async def second():
            async with service.invocation(model["provider_profile_id"]):
                second_entered.set()
        async with service.invocation(model["provider_profile_id"]):
            waiting = asyncio.create_task(second())
            await asyncio.sleep(0.05)
            assert not second_entered.is_set()
            with pytest.raises(CoreDataError) as error:
                await service.handle("model.remove", {"model_id": model["id"]})
            assert error.value.code == "model_busy"
            with pytest.raises(CoreDataError):
                await service.handle("runtime.unload", {})
        await waiting
        async with service.invocation(model["provider_profile_id"]):
            loading = await service.handle("runtime.load", {"model_id": model["id"]})
            await asyncio.sleep(0.05)
            assert (await service.handle("job.list", {}))["jobs"][0]["status"] == "running"
        assert (await finish(service, loading))["status"] == "completed"
        await service.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("mode,code", [("identity", "runtime_identity"), ("crash", "malformed_model"), ("driver", "driver_error")])
def test_failed_readiness_never_publishes_profile(tmp_path, mode, code):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime", modes={"cpu": mode}), load_timeout=5)
        model = await imported(service, tmp_path / "model.gguf")
        job = await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert job["status"] == "failed", job
        assert job["error"]["code"] == code
        assert service.provider_profiles() == {}
        await service.close()
    asyncio.run(exercise())


def test_cancel_loading_kills_child_and_removes_credential_file(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime", modes={"cpu": "slow"}))
        model = await imported(service, tmp_path / "model.gguf")
        queued = await service.handle("runtime.load", {"model_id": model["id"]})
        for _ in range(300):
            if service._runtime and service._runtime.process:
                break
            await asyncio.sleep(0.01)
        process = service._runtime.process
        await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
        assert (await finish(service, queued))["status"] == "cancelled"
        assert process.poll() is not None and service.provider_profiles() == {}
        assert not list((tmp_path / "data").rglob("*.key"))
        await service.close()
    asyncio.run(exercise())


def test_repeated_runtime_cancel_and_close_wait_for_child_cleanup(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime", modes={"cpu": "slow"}))
        model = await imported(service, tmp_path / "model.gguf")
        queued = await service.handle("runtime.load", {"model_id": model["id"]})
        for _ in range(300):
            if service._runtime and service._runtime.process:
                break
            await asyncio.sleep(0.01)
        process = service._runtime.process
        original = service._runtime._stop_child
        stopping, release = asyncio.Event(), asyncio.Event()

        async def paused():
            stopping.set()
            await release.wait()
            await original()

        service._runtime._stop_child = paused
        await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
        await asyncio.wait_for(stopping.wait(), 5)
        await service.handle("job.cancel", {"job_id": queued["job"]["id"]})
        closing = asyncio.create_task(service.close())
        try:
            await asyncio.sleep(0.05)
            assert service._operation == "runtime.load"
            assert not closing.done()
        finally:
            release.set()
            await closing
        job = next(job for job in service.catalog.jobs() if job["id"] == queued["job"]["id"])
        assert job["status"] == "cancelled"
        assert process.poll() is not None
    asyncio.run(exercise())


def test_auto_falls_back_but_explicit_backend_failure_remains_visible(tmp_path):
    async def exercise():
        runtime = fake_runtime(tmp_path / "runtime", modes={"cpu": "normal", "cuda": "driver", "vulkan": "normal"})
        service = service_at(tmp_path, runtime_root=runtime, load_timeout=30)
        model = await imported(service, tmp_path / "model.gguf")
        await service.handle("model.configure", {"model_id": model["id"], "device": "cuda"})
        job = await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert job["status"] == "failed" and job["error"]["code"] == "driver_error"
        await service.handle("model.configure", {"model_id": model["id"], "device": "auto"})
        job = await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert job["status"] == "completed", job
        status = await service.handle("runtime.status", {})
        assert status["backend"] == "cpu"
        assert {item["backend"] for item in status["fallbacks"]} == {"cuda", "vulkan"}
        await service.close()
    asyncio.run(exercise())


def test_gpu_backend_requires_observed_layer_offload(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime", modes={"cuda": "gpu"}))
        model = await imported(service, tmp_path / "model.gguf")
        await service.handle("model.configure", {"model_id": model["id"], "device": "cuda"})
        job = await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert job["status"] == "completed", job
        status = await service.handle("runtime.status", {})
        assert status["backend"] == "cuda" and status["gpu_layers_used"] == 2
        await service.close()
    asyncio.run(exercise())


def test_runtime_retains_structural_startup_facts_after_log_ring_rollover():
    from agent.local_models.runtime import _Output
    reader, writer = os.pipe()
    output = _Output(os.fdopen(reader, "rb"))
    try:
        os.write(writer, b"using device CUDA0\nload_tensors: offloaded 12/29 layers to GPU\nllama_context: n_ctx = 8192\n")
        for _ in range(40):
            os.write(writer, b"x" * 4095 + b"\n")
    finally:
        os.close(writer)
    output.thread.join(5)
    assert "offloaded" not in output.text()
    assert output.facts() == {"gpu_layers": 12, "backends": ["cuda"], "context_length": 8192}


def test_idle_release_protects_active_calls_and_crash_invalidates_profile(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime"), idle_timeout=0.1)
        model = await imported(service, tmp_path / "model.gguf")
        await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        async with service.invocation(model["provider_profile_id"]):
            await asyncio.sleep(0.2)
            assert service.provider_profiles()
        await asyncio.sleep(0.3)
        assert service.provider_profiles() == {}
        await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        process = service._runtime.process
        process.kill()
        await asyncio.sleep(0.2)
        assert service.provider_profiles() == {}
        assert (await service.handle("runtime.status", {}))["state"] == "failed"
        await service.close()
    asyncio.run(exercise())


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_job_close_kills_child_process_tree(tmp_path):
    from agent.infrastructure.processes import WindowsJob, launch_contained
    script = "import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(child.pid,flush=True);time.sleep(30)"
    process, job = launch_contained([sys.executable, "-c", script], cwd=tmp_path, stdout=subprocess.PIPE)
    descendant = int(process.stdout.readline())
    assert process.poll() is None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    child_handle = kernel.OpenProcess(0x00100000, False, descendant)
    assert child_handle
    job.close()
    process.wait(timeout=5)
    try:
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        kernel.CloseHandle(child_handle)
        process.stdout.close()


@pytest.mark.skipif(os.name != "nt" or sys.maxsize <= 2**32, reason="Windows x64 Job Object ABI")
def test_windows_job_memory_limit_applies_to_process_and_aggregate_job(tmp_path):
    import ctypes
    from ctypes import wintypes
    from agent.infrastructure.processes import launch_contained
    limit = 128 * 1024 * 1024
    process, job = launch_contained([sys.executable, "-c", "import time;time.sleep(30)"], cwd=tmp_path, memory_limit=limit)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    information = ctypes.create_string_buffer(144)
    try:
        assert kernel.QueryInformationJobObject(job._handle, 9, information, len(information), None)
        flags = int.from_bytes(information.raw[16:20], "little")
        assert flags & 0x300 == 0x300
        assert int.from_bytes(information.raw[112:120], "little") == limit
        assert int.from_bytes(information.raw[120:128], "little") == limit
    finally:
        job.close()
        process.wait(timeout=5)


def test_port_conflict_retries_with_new_authenticated_endpoint(tmp_path, monkeypatch):
    import agent.local_models.runtime as runtime
    original = runtime.allocate_port
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    blocked_port = occupied.getsockname()[1]
    first = True

    def allocate():
        nonlocal first
        if first:
            first = False
            return blocked_port
        return original()

    monkeypatch.setattr(runtime, "allocate_port", allocate)

    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime"), load_timeout=10)
        model = await imported(service, tmp_path / "model.gguf")
        job = await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert job["status"] == "completed", job
        profile = service.get_profile(model["provider_profile_id"])
        assert f":{blocked_port}/" not in profile.base_url
        await service.close()
    try:
        asyncio.run(exercise())
    finally:
        occupied.close()


def test_verified_tool_mode_and_default_survive_reload(tmp_path):
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / "runtime"))
        model = await imported(service, tmp_path / "model.gguf")
        await service.handle("model.configure", {"model_id": model["id"], "is_default": True})
        assert service.default_provider_id() is None
        await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert service.default_provider_id() == model["provider_profile_id"]
        profile = service.get_profile(model["provider_profile_id"])
        assert profile.model_tool_modes[model["id"]] == "text_only"
        assert profile.model_capabilities[model["id"]]["input_cost_per_million"] == "0"
        async with service.invocation(model["provider_profile_id"]):
            await service.handle("model.configure", {"model_id": model["id"], "is_default": False})
            assert service.default_provider_id() is None
            assert service.get_profile(model["provider_profile_id"]) == profile
        service.set_tool_mode(model["provider_profile_id"], "structured")
        await service.handle("runtime.unload", {})
        await finish(service, await service.handle("runtime.load", {"model_id": model["id"]}))
        assert service.get_profile(model["provider_profile_id"]).model_tool_modes[model["id"]] == "structured"
        await service.close()
    asyncio.run(exercise())

@pytest.mark.parametrize(('target', 'stage'), [('restrict_file', 'credential preparation'), ('launch_contained', 'process creation')])
def test_native_preparation_failure_reports_its_stage_and_os_code(tmp_path, monkeypatch, target, stage):
    import agent.local_models.runtime as runtime
    async def exercise():
        service = service_at(tmp_path, runtime_root=fake_runtime(tmp_path / 'runtime'))
        model = await imported(service, tmp_path / 'model.gguf')
        def fail(*args, **kwargs):
            raise PermissionError(13, 'synthetic denied operation')
        monkeypatch.setattr(runtime, target, fail)
        try:
            job = await finish(service, await service.handle('runtime.load', {'model_id': model['id']}))
            assert job['status'] == 'failed'
            assert job['error']['code'] == 'runtime_dependency'
            assert stage in job['error']['message']
            assert 'OS error 13' in job['error']['message']
            assert 'test_local_models_runtime.py:' in job['error']['message']
            assert str(tmp_path) not in job['error']['message']
        finally:
            await service.close()
    asyncio.run(exercise())
