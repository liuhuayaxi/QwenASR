export const IDEASHELL_SERVERS = {
  china: "https://mcp.ideashell.cn/mcp",
  global: "https://api.ideashell.com/ideashell/mcp",
};

export function normalizeIdeashellServer(value) {
  const url = String(value || "").trim().replace(/\/+$/, "");
  if (url === "https://api.ideashell.cn/ideashell/mcp") return IDEASHELL_SERVERS.china;
  return Object.values(IDEASHELL_SERVERS).includes(url) ? url : "";
}
