import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { unzipSync, strFromU8 } from "fflate";
import { installTauriMock } from "./tauri-mock.mjs";

test("exports grouped findings with working links to the original evidence in both formats", async ({ page }, testInfo) => {
  const groups = [["已确认问题", "不符合", 2], ["待补充材料", "待补充", 1], ["无法判断", "无法判断", 2]];
  const report = ["# 文档审阅正文", "## 问题与待核事项汇总", "已确认问题 1 项 · 待补充材料 1 项 · 无法判断 1 项",
    ...groups.flatMap(([title, verdict, ref]) => ["### " + title,
      "| 问题项及位置 | 判断 | 原文 | 建议 | 证据 |\n| --- | --- | --- | --- | --- |\n" +
      `| ${title}项 | ${verdict} | 对应位置 | 建议 | [${ref}](#审阅出处-${ref}) |`]),
    ...[1, 2].flatMap(n => ["##### 审阅出处 " + n, "**来源**　标准第 " + n + " 页", "> 对应的完整证据 " + n]),
  ].join("\n\n");
  await installTauriMock(page);
  await page.route("**/__report_fixture.md", route => route.fulfill({ body: report, contentType: "text/plain; charset=utf-8" }));
  await page.addInitScript(() => { Object.defineProperty(window, "showSaveFilePicker", { value: undefined }); });
  await page.goto(new URL("/tests-ui/fixtures/markdown-preview.html", testInfo.project.use.baseURL ?? "http://127.0.0.1:5173").href);
  await expect(page.locator(".markdown-body table")).toHaveCount(3);
  async function download(pattern) {
    const pending = page.waitForEvent("download");
    await page.getByRole("button", { name: pattern }).click();
    return readFile(await (await pending).path());
  }
  const html = (await download(/Export HTML|导出网页/)).toString("utf8");
  const htmlRows = await page.evaluate(markup => {
    const doc = new DOMParser().parseFromString(markup, "text/html");
    return [...doc.querySelectorAll("table")].map(table => {
      const link = table.querySelector("td a");
      const detail = doc.querySelector(link.getAttribute("href"));
      return [table.previousElementSibling.textContent, table.rows[1].cells[1].textContent, detail.querySelector("blockquote").textContent.trim()];
    });
  }, html);
  const file = await download(/Export Word|导出 Word/);
  const xml = strFromU8(unzipSync(file, { filter: e => e.name === "word/document.xml" })["word/document.xml"]);
  const wordRows = await page.evaluate(markup => {
    const doc = new DOMParser().parseFromString(markup, "application/xml");
    const w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    return [...doc.getElementsByTagNameNS(w, "tbl")].map(table => {
      const row = table.getElementsByTagNameNS(w, "tr")[1];
      const anchor = row.getElementsByTagNameNS(w, "hyperlink")[0].getAttributeNS(w, "anchor");
      const bookmark = [...doc.getElementsByTagNameNS(w, "bookmarkStart")].find(el => el.getAttributeNS(w, "name") === anchor);
      return [table.previousElementSibling.textContent, row.getElementsByTagNameNS(w, "tc")[1].textContent,
        bookmark.parentElement.nextElementSibling.textContent];
    });
  }, xml);
  const expected = groups.map(([title, verdict, ref]) => [title, verdict, "对应的完整证据 " + ref]);
  expect(htmlRows).toEqual(expected);
  expect(wordRows).toEqual(expected);
});
