import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";


vi.mock(
  "@tauri-apps/api/core",
  () => ({
    invoke:
      vi.fn(),
  }),
);


import {
  invoke,
} from "@tauri-apps/api/core";

import {
  TauriSystemLocaleProvider,
} from "./TauriSystemLocaleProvider";


const invokeMock =
  vi.mocked(invoke);


describe(
  "TauriSystemLocaleProvider",
  () => {
    beforeEach(
      () => {
        invokeMock
          .mockReset();
      },
    );


    it(
      "reads system locale through the Rust native locale command",
      async () => {
        invokeMock
          .mockResolvedValue(
            "zh-CN",
          );


        const provider =
          new TauriSystemLocaleProvider();


        const locale =
          await provider
            .getLocale();


        expect(
          invokeMock,
        ).toHaveBeenCalledWith(
          "get_system_locale",
        );


        expect(locale)
          .toBe(
            "zh-CN",
          );
      },
    );
  },
);