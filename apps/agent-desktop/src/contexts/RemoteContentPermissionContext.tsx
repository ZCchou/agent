import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { settingsRepository } from "../services/runtime";
import type { RemoteImagePolicy } from "../types/settings";

interface RemoteContentPermissionValue {
  policy: RemoteImagePolicy;
  loading: boolean;
  saving: boolean;
  error: boolean;
  allowAllRemoteImages: () => Promise<void>;
  revokeRemoteImages: () => Promise<void>;
}

const RemoteContentPermissionContext = createContext<RemoteContentPermissionValue | null>(null);

export function RemoteContentPermissionProvider({ children }: { children: ReactNode }) {
  const [policy, setPolicy] = useState<RemoteImagePolicy>("ask");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const updateInFlight = useRef(false);

  useEffect(() => {
    let cancelled = false;
    void settingsRepository.getAppSettings()
      .then((settings) => {
        if (!cancelled) {
          setPolicy(settings.content?.remote_image_policy ?? "ask");
          setError(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPolicy("ask");
          setError(true);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const updatePolicy = useCallback(async (next: RemoteImagePolicy) => {
    if (updateInFlight.current) return;
    updateInFlight.current = true;
    setSaving(true);
    setError(false);
    try {
      const settings = await settingsRepository.updateRemoteImagePolicy(next);
      setPolicy(settings.content?.remote_image_policy ?? next);
    } catch (updateError) {
      setError(true);
      throw updateError;
    } finally {
      updateInFlight.current = false;
      setSaving(false);
    }
  }, []);

  const value = useMemo<RemoteContentPermissionValue>(() => ({
    policy,
    loading,
    saving,
    error,
    allowAllRemoteImages: () => updatePolicy("allow"),
    revokeRemoteImages: () => updatePolicy("ask"),
  }), [policy, loading, saving, error, updatePolicy]);

  return (
    <RemoteContentPermissionContext.Provider value={value}>
      {children}
    </RemoteContentPermissionContext.Provider>
  );
}

export function useRemoteContentPermission() {
  const value = useContext(RemoteContentPermissionContext);
  if (!value) {
    throw new Error("useRemoteContentPermission must be used inside RemoteContentPermissionProvider");
  }
  return value;
}
