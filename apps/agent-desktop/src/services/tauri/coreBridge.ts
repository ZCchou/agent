import { invoke } from "@tauri-apps/api/core";

export interface CoreStatus {
  running: boolean;
  healthy: boolean;
  issue?: string | null;
}

/**
 * 查询长期运行的 Python Agent Core 当前状态。
 *
 * 所有 Tauri invoke 调用集中放在 Adapter 中，
 * 页面组件不直接依赖具体 Rust command 名称。
 */
export async function getAgentCoreStatus(): Promise<CoreStatus> {
  return invoke<CoreStatus>("agent_core_status");
}

export interface CorePing {
  pid: number;
  core_version: string;
  protocol_version: number;
  schema_version: number;
  build_id: string;
}

export async function pingAgentCore(): Promise<CorePing> {
  return invoke<CorePing>("ping_agent_core");
}

export async function startAgentCore(): Promise<CoreStatus> {
  return invoke<CoreStatus>("start_agent_core");
}

export async function shutdownAgentCore(): Promise<CoreStatus> {
  return invoke<CoreStatus>("shutdown_agent_core");
}
