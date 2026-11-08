import { expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ModelCatalogItem } from "./ModelCatalogItem";
import type { ProviderModelSettings } from "../../types/settings";
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
it.each([true, false])("disables catalog search actions in offline mode, native=%s", native => {
  const noop = () => {};
  const model = { id: "local", enabled: true, source: "discovered", capabilities: { text: "yes", context_length: 8192, max_output_tokens: null }, tool_mode: "unknown", status: "available" } as ProviderModelSettings;
  const html = renderToStaticMarkup(<ModelCatalogItem model={model} routeHealth={[]} isCurrent={false} canProbeNativeSearch={native} onEnabledChange={noop} onAutomaticRoutingChange={noop} onProbeTools={noop} onProbeNativeSearch={noop} onResetRouteHealth={async () => {}} onConfigureExternalSearch={noop} onSetDefault={noop} onRemoveManual={noop} onSaveLimits={async () => {}} onSaveRouting={async () => {}} onResetVision={noop} />);
  const offset = html.indexOf(native ? "models.probeNativeSearch</button>" : "models.configureExternalSearch</button>");
  expect(offset).toBeGreaterThan(0);
  expect(html.slice(html.lastIndexOf("<button", offset), offset)).toContain('disabled=""');
});
