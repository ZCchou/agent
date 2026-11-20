type InterfaceStyle = "classic" | "ragent";
const key = "agent.interfaceStyle";

export function readInterfaceStyle(): InterfaceStyle {
  try {
    return localStorage.getItem(key) === "classic" ? "classic" : "ragent";
  } catch {
    return "ragent";
  }
}

export function initializeInterfaceStyle() {
  document.documentElement.dataset.interfaceStyle = readInterfaceStyle();
}

export function saveInterfaceStyle(style: InterfaceStyle) {
  localStorage.setItem(key, style);
  document.documentElement.dataset.interfaceStyle = style;
}
