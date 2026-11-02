import {
  describe,
  expect,
  it,
} from "vitest";

import {
  isLanguagePreference,
  normalizeSystemLanguage,
  resolveLanguage,
} from "./language";


describe(
  "agent language resolution",
  () => {
    it(
      "maps Chinese system locales to zh-CN",
      () => {
        expect(
          normalizeSystemLanguage(
            "zh-CN",
          ),
        ).toBe("zh-CN");

        expect(
          normalizeSystemLanguage(
            "zh-TW",
          ),
        ).toBe("zh-CN");
      },
    );


    it(
      "falls back to English for unsupported system locales",
      () => {
        expect(
          normalizeSystemLanguage(
            "en-US",
          ),
        ).toBe("en");

        expect(
          normalizeSystemLanguage(
            "ja-JP",
          ),
        ).toBe("en");
      },
    );


    it(
      "uses the system language when preference is system",
      () => {
        expect(
          resolveLanguage(
            "system",
            "zh-CN",
          ),
        ).toBe("zh-CN");

        expect(
          resolveLanguage(
            "system",
            "en-US",
          ),
        ).toBe("en");
      },
    );


    it(
      "explicit language overrides system language",
      () => {
        expect(
          resolveLanguage(
            "en",
            "zh-CN",
          ),
        ).toBe("en");

        expect(
          resolveLanguage(
            "zh-CN",
            "en-US",
          ),
        ).toBe("zh-CN");
      },
    );


    it(
      "accepts only supported language preferences",
      () => {
        expect(
          isLanguagePreference(
            "system",
          ),
        ).toBe(true);

        expect(
          isLanguagePreference(
            "en",
          ),
        ).toBe(true);

        expect(
          isLanguagePreference(
            "zh-CN",
          ),
        ).toBe(true);

        expect(
          isLanguagePreference(
            "ja",
          ),
        ).toBe(false);
      },
    );
  },
);