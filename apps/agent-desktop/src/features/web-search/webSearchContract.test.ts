import { describe, expect, it } from "vitest";
import contract from "../../../../../contracts/web-search-v2.json";
import { WEB_OPERATION_STATUSES, WEB_SEARCH_ROUTES } from "../../types/domain";

describe("web-search shared contract", () => {
  it("keeps TypeScript routes and operation states aligned with Host and Core", () => {
    expect([...WEB_SEARCH_ROUTES]).toEqual(contract.routes);
    expect([...WEB_OPERATION_STATUSES]).toEqual(contract.operation_statuses);
    expect(contract.conversation_modes).toEqual(["inherit", "off", "auto"]);
    expect(contract.native_statuses).toContain("declared");
    expect(contract.native_adapters).toContain("anthropic_messages_web_search");
  });
});
