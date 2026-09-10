// Share the current summary settings with the local course importer; never copy keys.
document.getElementById("courseSyncLink")?.addEventListener("click", async () => {
  try {
    const saved = JSON.parse(localStorage.getItem("qwen-asr-api-settings-v1") || "{}");
    const raw = saved.function_providers?.summary || {};
    const { api_key, ...provider } = raw;
    const { api_key: correctionKey, ...correction } = saved.function_providers?.correction || {};
    if (!provider.model) return;
    await fetch("http://127.0.0.1:8766/api/options", {
      method: "POST", headers: { "Content-Type": "application/json", "X-QwenASR-Client": "smartclass" },
      body: JSON.stringify({ summary_provider: provider,
        correction_provider: correction,
        summary_system_prompt: saved.detailed_summary_prompt || "", abstract_system_prompt: saved.abstract_prompt || "" }),
      keepalive: true,
    });
  } catch { /* The course page offers editable settings if the service is starting. */ }
});
