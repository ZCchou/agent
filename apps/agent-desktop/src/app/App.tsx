import { AppUpdateProvider } from "../features/updater/AppUpdateProvider";
import { AppProvider } from "../contexts/AppContext";
import { RemoteContentPermissionProvider } from "../contexts/RemoteContentPermissionContext";
import { ChatInspectorStateProvider } from "../contexts/ChatInspectorContext";
import { ResourceActionsProvider } from "../features/resources/ResourceActionsProvider";

import { AppShell } from "./AppShell";

export default function App() {
  return (
    <AppProvider>
      <RemoteContentPermissionProvider>
        <ChatInspectorStateProvider>
          <ResourceActionsProvider><AppUpdateProvider><AppShell /></AppUpdateProvider></ResourceActionsProvider>
        </ChatInspectorStateProvider>
      </RemoteContentPermissionProvider>
    </AppProvider>
  );
}
