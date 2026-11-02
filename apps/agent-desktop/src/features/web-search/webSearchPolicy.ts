export type WebSearchMenuOption = "inherit" | "off" | "auto" | "once";

export function webSearchMenuOptions(isProjectWork: boolean): WebSearchMenuOption[] {
  return isProjectWork ? ["inherit", "off", "auto", "once"] : ["off", "auto", "once"];
}
