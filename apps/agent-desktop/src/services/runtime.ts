import {
  TauriSettingsRepository,
} from "./tauri/TauriSettingsRepository";

import {
  TauriSystemLocaleProvider,
} from "./tauri/TauriSystemLocaleProvider";
import { TauriProjectRepository } from "./tauri/TauriProjectRepository";
import { TauriChatGroupRepository } from "./tauri/TauriChatGroupRepository";
import { TauriConversationRepository } from "./tauri/TauriConversationRepository";
import { TauriWorkToolRepository } from "./tauri/TauriWorkToolRepository";
import { TauriTrashRepository } from "./tauri/TauriTrashRepository";
import { TauriModelRunRepository } from "./tauri/TauriModelRunRepository";
import { TauriProviderRepository } from "./tauri/TauriProviderRepository";
import { TauriModelCatalogRepository } from "./tauri/TauriModelCatalogRepository";
import { TauriAgentExecutionRepository } from "./tauri/TauriAgentExecutionRepository";
import { TauriChangeSetRepository } from "./tauri/TauriChangeSetRepository";
import { TauriWorkPermissionRepository } from "./tauri/TauriWorkPermissionRepository";
import { TauriAttachmentRepository } from "./tauri/TauriAttachmentRepository";
import { TauriProjectFilePreviewRepository } from "./tauri/TauriProjectFilePreviewRepository";
import { TauriProjectDirectoryRepository } from "./tauri/TauriProjectDirectoryRepository";
import { TauriContextRepository } from "./tauri/TauriContextRepository";
import { TauriWebSearchRepository } from "./tauri/TauriWebSearchRepository";
import { TauriMemoryRepository } from "./tauri/TauriMemoryRepository";
import { TauriSkillRepository } from "./tauri/TauriSkillRepository";


/**
 * agent 前端基础设施的统一组装位置。
 *
 * 页面不要自行 new Repository，
 * 这样以后替换实现只需要修改这里。
 */
export const settingsRepository =
  new TauriSettingsRepository();


export const systemLocaleProvider =
  new TauriSystemLocaleProvider();

export const projectRepository = new TauriProjectRepository();
export const chatGroupRepository = new TauriChatGroupRepository();
export const conversationRepository = new TauriConversationRepository();
export const workToolRepository = new TauriWorkToolRepository();
export const trashRepository = new TauriTrashRepository();
export const modelRunRepository = new TauriModelRunRepository();
export const providerRepository = new TauriProviderRepository();
export const modelCatalogRepository = new TauriModelCatalogRepository();
export const agentExecutionRepository = new TauriAgentExecutionRepository();
export const changeSetRepository = new TauriChangeSetRepository();
export const workPermissionRepository = new TauriWorkPermissionRepository();
export const attachmentRepository = new TauriAttachmentRepository();
export const projectFilePreviewRepository = new TauriProjectFilePreviewRepository();
export const projectDirectoryRepository = new TauriProjectDirectoryRepository();
export const contextRepository = new TauriContextRepository();
export const webSearchRepository = new TauriWebSearchRepository();
export const memoryRepository = new TauriMemoryRepository();
export const skillRepository = new TauriSkillRepository();

import { TauriAppUpdateRepository } from "./tauri/TauriAppUpdateRepository";
export const appUpdateRepository = new TauriAppUpdateRepository();
