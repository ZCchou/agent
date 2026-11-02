"""
agent 日志事件名称定义。

业务模块应该优先使用这里定义的事件常量，
不要在各处随意手写字符串。

这样可以避免出现：
    core_start
    core_started
    agent_core_started
    python_started

这种同一个事件多个名字的情况。
"""


# =========================================================
# Agent Core 生命周期
# =========================================================

CORE_STARTED = "core_started"
CORE_STOPPED = "core_stopped"
CORE_UNHANDLED_EXCEPTION = "core_unhandled_exception"


# =========================================================
# IPC
# =========================================================

IPC_REQUEST_RECEIVED = "ipc_request_received"
IPC_REQUEST_COMPLETED = "ipc_request_completed"
IPC_REQUEST_FAILED = "ipc_request_failed"
IPC_INVALID_JSON = "ipc_invalid_json"
IPC_REQUEST_TIMEOUT = "ipc_request_timeout"


# =========================================================
# Model
# =========================================================

MODEL_REQUEST_STARTED = "model_request_started"
MODEL_REQUEST_COMPLETED = "model_request_completed"
MODEL_REQUEST_FAILED = "model_request_failed"
MODEL_RUN_PERSISTENCE_FAILED = "model_run_persistence_failed"
MODEL_RESPONSE_DIAGNOSTIC = "model_response_diagnostic"


# =========================================================
# Skill
# =========================================================

SKILL_STARTED = "skill_started"
SKILL_COMPLETED = "skill_completed"
SKILL_FAILED = "skill_failed"


# =========================================================
# Memory
# =========================================================

MEMORY_READ = "memory_read"
MEMORY_WRITE = "memory_write"
MEMORY_WRITE_REJECTED = "memory_write_rejected"


# =========================================================
# System
# =========================================================

LOGGING_STARTED = "logging_started"
LOGGING_STOPPED = "logging_stopped"
LOGGING_DEGRADED = "logging_degraded"
LOG_RETENTION_COMPLETED = "log_retention_completed"
