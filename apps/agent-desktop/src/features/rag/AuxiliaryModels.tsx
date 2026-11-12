import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { ragRepository } from "../../services/runtime";
import type {
  RagModelList,
  RagPathSelection,
  RagProfile,
} from "../../types/rag";
import { OcrPanel } from "./OcrPanel";
import "./rag.css";
type Action = (operation: () => Promise<void>) => Promise<void>;
interface PanelProps {
  busy: boolean;
  act: Action;
  refresh: () => Promise<void>;
}
const profiles: RagProfile[] = [
  "qwen3-embedding-0.6b",
  "qwen3-reranker-0.6b",
  "bge-m3",
  "bge-reranker-v2-m3",
];
export function AuxiliaryConfiguration({
  kind,
}: {
  kind: "retrieval" | "ocr";
}) {
  const [models, setModels] = useState<RagModelList>({
    models: [],
    root: "",
    runtime: {},
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = async () => {
    setModels(await ragRepository.listModels());
  };
  useEffect(() => {
    let alive = true;
    void ragRepository
      .listModels()
      .then((result) => {
        if (alive) setModels(result);
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
    };
  }, []);
  const act: Action = async (operation) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await operation();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="rag-page">
      <h2>{kind === "ocr" ? "OCR 模型" : "检索模型"}</h2>
      {error && <p role="alert">{error}</p>}
      {kind === "ocr" ? (
        <OcrPanel />
      ) : (
        <ModelsPanel models={models} busy={busy} act={act} refresh={refresh} />
      )}
    </section>
  );
}
export function ModelsPanel({
  models,
  busy,
  act,
  refresh,
}: PanelProps & { models: RagModelList }) {
  const { t } = useTranslation("common");
  const [profile, setProfile] = useState<RagProfile>(profiles[0]);
  const [device, setDevice] = useState("cpu");
  const [selection, setSelection] = useState<RagPathSelection | null>(null);
  const [inspected, setInspected] = useState(false);
  const [removeId, setRemoveId] = useState<string | null>(null);
  useEffect(
    () => () => {
      if (selection)
        void ragRepository.revokeSelection(selection).catch(() => {});
    },
    [selection],
  );
  async function choose(modelFile: boolean) {
    const selected = await ragRepository.selectPaths("model", modelFile);
    if (!selected) return;
    setSelection(selected);
    setInspected(false);
    const inspection = await ragRepository.inspectModel(selected, profile);
    if (inspection.valid === false)
      throw new Error(
        Array.isArray(inspection.errors)
          ? inspection.errors.join("\n")
          : t("rag.error"),
      );
    setInspected(true);
  }
  return (
    <div className="rag-stack">
      <p>{t("rag.modelHelp")}</p>
      <p>
        {t("rag.runtime")}{" "}
        {t(
          models.runtime.available
            ? "rag.statuses.available"
            : "rag.statuses.unavailable",
        )}
      </p>
      {typeof models.runtime.error === "string" && (
        <p role="alert">{models.runtime.error}</p>
      )}
      <Card className="rag-card">
        <h3>{t("rag.root")}</h3>
        <p className="rag-path">{models.root || "—"}</p>
        <Button
          disabled={busy}
          onClick={() =>
            void act(async () => {
              const selected = await ragRepository.selectPaths("root");
              if (selected) {
                await ragRepository.setModelRoot(selected);
                await refresh();
              }
            })
          }
        >
          {t("rag.chooseRoot")}
        </Button>
      </Card>
      <Card className="rag-card">
        <h3>{t("rag.importModel")}</h3>
        <div className="rag-fields">
          <label>
            {t("rag.modelProfile")}
            <select
              value={profile}
              disabled={busy}
              onChange={(event) => {
                setProfile(event.target.value as RagProfile);
                setInspected(false);
                setSelection(null);
              }}
            >
              {profiles.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          <label>
            {t("rag.device")}
            <DeviceSelect value={device} disabled={busy} onChange={setDevice} />
          </label>
        </div>
        <div className="rag-actions">
          <Button disabled={busy} onClick={() => void act(() => choose(false))}>
            {t("rag.importFolder")}
          </Button>
          <Button disabled={busy} onClick={() => void act(() => choose(true))}>
            {t("rag.importFile")}
          </Button>
        </div>
        {selection && (
          <>
            <p className="rag-path">{selection.paths[0]}</p>
            <Button
              disabled={busy}
              onClick={() => {
                setSelection(null);
                setInspected(false);
              }}
            >
              {t("rag.cancel")}
            </Button>
          </>
        )}
        {inspected && (
          <>
            <p>{t("rag.inspected")}</p>
            <Button
              variant="primary"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  if (!selection) return;
                  const selected = selection;
                  try {
                    await ragRepository.importModel(selected, profile, device);
                  } finally {
                    setSelection(null);
                    setInspected(false);
                  }
                  await refresh();
                })
              }
            >
              {t("rag.importModel")}
            </Button>
          </>
        )}
      </Card>
      {!models.models.length && <p>{t("rag.empty")}</p>}
      {models.models.map((model) => (
        <Card key={model.id} className="rag-card">
          <h3>{model.name}</h3>
          <p>
            {model.profile} · {model.backend}
          </p>
          <p>
            {t("rag.status")}{" "}
            {t(`rag.statuses.${model.status}`, { defaultValue: model.status })}{" "}
            · {t("rag.dimension")} {model.dimension ?? "—"} ·{" "}
            {(model.size_bytes / 1024 ** 3).toFixed(2)} GB
          </p>
          {model.error && <p role="alert">{model.error}</p>}
          <label className="rag-field">
            {t("rag.device")}
            <DeviceSelect
              value={model.device}
              disabled={busy}
              onChange={(value) =>
                void act(async () => {
                  await ragRepository.setModelDevice(model.id, value);
                  await refresh();
                })
              }
            />
          </label>
          <div className="rag-actions">
            <Button
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  await ragRepository.probeModel(model.id);
                  await refresh();
                })
              }
            >
              {t("rag.probe")}
            </Button>
            <Button disabled={busy} onClick={() => setRemoveId(model.id)}>
              {t("rag.remove")}
            </Button>
          </div>
          {removeId === model.id && (
            <div role="alert">
              <p>{t("rag.removeConfirm", { name: model.name })}</p>
              <div className="rag-actions">
                <Button
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      await ragRepository.removeModel(model.id);
                      setRemoveId(null);
                      await refresh();
                    })
                  }
                >
                  {t("rag.confirm")}
                </Button>
                <Button onClick={() => setRemoveId(null)}>
                  {t("rag.cancel")}
                </Button>
              </div>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
function DeviceSelect({
  value,
  disabled,
  onChange,
}: {
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation("common");
  const devices = ["cpu", "cuda"];
  if (!devices.includes(value)) devices.push(value);
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
    >
      {devices.map((device) => (
        <option key={device} value={device}>
          {t(`rag.${device}`, { defaultValue: device })}
        </option>
      ))}
    </select>
  );
}
