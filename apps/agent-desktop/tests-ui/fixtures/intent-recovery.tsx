import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { AppContext, AppProvider, useApp } from "../../src/contexts/AppContext";
import { RemoteContentPermissionProvider } from "../../src/contexts/RemoteContentPermissionContext";
import { ChatInspectorStateProvider } from "../../src/contexts/ChatInspectorContext";
import { ResourceActionsProvider } from "../../src/features/resources/ResourceActionsProvider";
import { AppUpdateProvider } from "../../src/features/updater/AppUpdateProvider";
import { ChatPage } from "../../src/features/chat/ChatPage";
import { initializeI18n } from "../../src/i18n";
import "../../src/styles/tokens.css";
import "../../src/styles/global.css";
import "../../src/styles/components.css";

function ScopedChat() {
  const app = useApp();
  const [originalDraft, setOriginalDraft] = useState(true);
  const scoped = { ...app,
    activeConversationId: originalDraft ? null : app.activeConversationId,
    activeConversation: originalDraft ? null : app.activeConversation,
    conversationCreated: async (conversation: Parameters<typeof app.conversationCreated>[0]) => {
      setOriginalDraft(false);
      await app.conversationCreated(conversation);
    },
  };
  return <AppContext.Provider value={scoped}>
    <nav aria-label="Test scope navigation">
      <button onClick={() => { setOriginalDraft(false); app.openConversation("work-1"); }}>Other scope</button>
      <button onClick={() => setOriginalDraft(true)}>Original draft</button>
      <output aria-label="Current scope">{scoped.activeConversationId ?? `draft-${app.draft.revision}`}</output>
    </nav>
    <RemoteContentPermissionProvider><ChatInspectorStateProvider><ResourceActionsProvider><AppUpdateProvider>
      <ChatPage />
    </AppUpdateProvider></ResourceActionsProvider></ChatInspectorStateProvider></RemoteContentPermissionProvider>
  </AppContext.Provider>;
}

await initializeI18n();
createRoot(document.getElementById("root")!).render(<AppProvider><ScopedChat /></AppProvider>);
