// AI task actions carry only task identity and user intent. The backend
// rebuilds providers, prompts, and credentials from its durable local files.
export function aiTaskPayload(task, action) {
  return {id: task.id, action, use_saved_ai_settings: true};
}
