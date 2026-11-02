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
  TauriSettingsRepository,
} from "./TauriSettingsRepository";


const invokeMock =
  vi.mocked(invoke);


describe(
  "TauriSettingsRepository",
  () => {
    beforeEach(
      () => {
        invokeMock
          .mockReset();
      },
    );


    it(
      "loads application settings through the Rust SettingsManager command",
      async () => {
        const expected = {
          schema_version: 1,

          general: {
            interface_language:
              "system" as const,
          },
        };


        invokeMock
          .mockResolvedValue(
            expected,
          );


        const repository =
          new TauriSettingsRepository();


        const result =
          await repository
            .getAppSettings();


        expect(
          invokeMock,
        ).toHaveBeenCalledWith(
          "get_app_settings",
        );


        expect(result)
          .toEqual(expected);
      },
    );


    it(
      "updates interface language through the Rust SettingsManager command",
      async () => {
        const expected = {
          schema_version: 1,

          general: {
            interface_language:
              "zh-CN" as const,
          },
        };


        invokeMock
          .mockResolvedValue(
            expected,
          );


        const repository =
          new TauriSettingsRepository();


        const result =
          await repository
            .updateInterfaceLanguage(
              "zh-CN",
            );


        expect(
          invokeMock,
        ).toHaveBeenCalledWith(
          "update_interface_language",
          {
            language:
              "zh-CN",
          },
        );


        expect(result)
          .toEqual(expected);
      },
    );

    it("persists sidebar width through its dedicated command", async () => {
      const expected = {
        schema_version: 3,
        general: { interface_language: "system" as const },
        sidebar: { width_px: 336 },
      };
      invokeMock.mockResolvedValue(expected);
      const repository = new TauriSettingsRepository();

      const result = await repository.updateSidebarWidth(336);

      expect(invokeMock).toHaveBeenCalledWith("update_sidebar_width", { widthPx: 336 });
      expect(result).toEqual(expected);
    });

    it("persists the global remote image policy through its dedicated command", async () => {
      const expected = {
        schema_version: 7,
        general: { interface_language: "system" as const },
        content: { remote_image_policy: "allow" as const },
      };
      invokeMock.mockResolvedValue(expected);
      const repository = new TauriSettingsRepository();

      const result = await repository.updateRemoteImagePolicy("allow");

      expect(invokeMock).toHaveBeenCalledWith("update_remote_image_policy", { policy: "allow" });
      expect(result).toEqual(expected);
    });
  },
);
