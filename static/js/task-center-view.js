// Task center presents one study item at a time. Durable task identities remain
// the action targets; display grouping never changes their receipts or status.
export const activeStatuses = new Set(['running', 'queued', 'waiting_retry', 'stopping', 'active']);
export const errorStatuses = new Set(['failed', 'error', 'interrupted', 'stale', 'completed_with_errors', 'attention']);
const archived = task => task.recovered || task.display_status === 'recovered' || ['completed', 'cancelled'].includes(task.status);
const recordingId = task => {
  if (task.recording_id) return task.recording_id;
  try { return new URL(task.result_url || '', 'http://localhost').searchParams.get('recording'); } catch { return null; }
};
export function taskGroups(payload = {}) {
  const source = Array.isArray(payload.groups) ? payload.groups : (() => {
    const groups = new Map();
    for (const task of payload.tasks || []) {
      const recording = recordingId(task);
      // A course may contain many unrelated PDFs and lessons. A missing record
      // identity must never silently combine those operations by course name.
      const id = recording ? `recording:${recording}` : `task:${task.id}`;
      if (!groups.has(id)) groups.set(id, {id, recording_id: recording, title: task.title, category: task.category, tasks: []});
      groups.get(id).tasks.push(task);
    }
    return [...groups.values()];
  })();
  return source.map(group => {
    const tasks = group.tasks || [];
    const current = (group.current_tasks || tasks.filter(task => !archived(task))).filter(task => !archived(task));
    const history = group.history || tasks.filter(archived);
    const errors = group.errors || current.filter(task => errorStatuses.has(task.status) || task.error).map(task => ({...task, label: task.label || task.title}));
    const status = group.status || (current.some(task => activeStatuses.has(task.status)) ? 'running' : errors.length ? 'failed' : current.some(task => task.status === 'paused') ? 'paused' : current.length ? 'pending' : tasks.length && tasks.every(task => task.status === 'cancelled') ? 'cancelled' : 'completed');
    const leading = current.find(task => activeStatuses.has(task.status)) || current[0];
    return {...group, tasks, current_tasks: current, history, errors, status,
      title: group.title || leading?.title || '未命名任务', category: group.category || '',
      current_stage: group.current_stage || leading?.stage || (status === 'completed' ? '处理已完成' : ''),
      result_url: group.result_url || tasks.find(task => task.result_url)?.result_url || '',
      review_count: group.review_count ?? current.filter(task => task.status === 'pending' && task.attention_kind !== 'auto_review' && (task.attention_kind === 'review' || Number(task.review_required_blocks) > 0)).length,
      recovered_count: group.recovered_count ?? history.filter(task => task.recovered || task.display_status === 'recovered').length};
  });
}
export function groupMatchesStatus(group, filter) {
  if (filter === 'all') return true;
  if (filter === 'daily') return activeStatuses.has(group.status) || errorStatuses.has(group.status) || ['paused', 'pending'].includes(group.status) || !!group.errors.length;
  // A lesson can be processing one step while another needs attention. Both
  // filters must include it, even when the error takes headline priority.
  if (filter === 'active') return activeStatuses.has(group.status) || group.current_tasks.some(task => activeStatuses.has(task.status));
  if (filter === 'attention') return !!group.errors.length || errorStatuses.has(group.status);
  if (filter === 'review') return group.review_count > 0;
  if (filter === 'recovered') return group.recovered_count > 0;
  return group.status === filter;
}
export function filterTaskGroups(groups, {filter = 'daily', type = '', category = '', query = ''} = {}) {
  const search = query.trim().toLocaleLowerCase();
  return groups.filter(group => groupMatchesStatus(group, filter)
    && (!type || group.tasks.some(task => task.type === type || task.family === type))
    && (!category || (category === '__local__' ? !group.category : group.category === category))
    && (!search || [group.title, group.category, ...group.tasks.flatMap(task => [task.title, task.label, task.stage, ...(task.files || []).map(file => file.source_name)])].join(' ').toLocaleLowerCase().includes(search)));
}
export function groupCounts(groups) {
  return Object.fromEntries(['all', 'active', 'attention', 'review', 'pending', 'paused', 'completed', 'cancelled', 'recovered'].map(key => [key, groups.filter(group => groupMatchesStatus(group, key)).length]));
}
export function taskMatchesType(task, type = '') {
  return !type || task.type === type || task.family === type;
}
export function completedTasks(groups, type = '') {
  return [...new Map(groups.flatMap(group => [...group.tasks, ...group.history])
    .filter(task => task.status === 'completed' && taskMatchesType(task, type))
    .map(task => [task.id, task])).values()];
}
export function actionableTasks(groups, type = '') {
  return [...new Map(groups.flatMap(group => [...group.current_tasks, ...(group.context_tasks || [])]).filter(task => !archived(task) && taskMatchesType(task, type) && (task.actions || []).length).map(task => [task.id, task])).values()];
}

export function leadingTask(group) {
  const tasks=group.current_tasks || [];
  return tasks.find(t=>['running','active','stopping'].includes(t.status))
    || tasks.find(t=>activeStatuses.has(t.status)) || tasks.find(t=>errorStatuses.has(t.status))
    || tasks.find(t=>t.attention_kind==='review') || tasks[0] || group.history?.at(-1) || {};
}

export function taskProgressView(task) {
  const rawStage=String(task.stage || task.phase || '');
  const stage=task.type === 'knowledge_index' ? ({
    waiting_asr:'等待本机转写完成后建立知识索引',
    queued:'等待建立知识索引', indexing:'正在建立课程知识索引',
    completed:'课程知识索引已完成', failed:'课程知识索引失败',
    cancelled:'知识索引已取消', interrupted:'知识索引已中断',
  }[rawStage] || rawStage) : rawStage;
  const done=task.status==='completed' || task.recovered;
  if (task.attention_kind==='auto_review' || (task.type==='refinement' && stage.startsWith('AI 自动复核'))) {
    return {label:stage || 'AI 自动复核',count:task.verification?.total?`已复核 ${task.verification.completed || 0}/${task.verification.total} 条修改`:`剩余 ${task.review_required_blocks || 0} 条修改`,detail:'AI 逐项检查改动；无法确认时保留原文，无需逐条确认',percent:null};
  }
  if (task.status==='pending' && (task.attention_kind==='review' || Number(task.review_required_blocks)>0)) {
    return {label:`${task.review_required_blocks || task.total || 0} 段待核对`,detail:'校对结果已保存，核对数字与公式后即可使用',count:'',percent:null};
  }
  if (task.type==='summary' && task.status==='queued' && Number(task.queue_position)>0) {
    return {label:'排队中',count:`第 ${task.queue_position} 位`,detail:'前面的笔记完成后自动开始；可随时取消',percent:null};
  }
  if (task.type==='summary' && !done) {
    // 逐页审查 only exists when the saved AI 逐页审核 switch is on. "排版" is
    // the LaTeX/PDF layout pass, not a review.
    const reviewing=task.note_review_enabled!==false || /独立审查|修订/.test(stage);
    // The AI LaTeX pass is opt-in; by default the PDF is printed directly
    // (seconds) and is part of 保存.
    const latex=task.note_ai_latex_enabled===true || /LaTeX/.test(stage);
    const steps=['核对资料','梳理知识','编写图文',...(latex?[reviewing?'排版':'排版（PDF）']:[]),...(reviewing?['逐页审查']:[]),'保存'];
    const save=steps.length-1,latexAt=latex?3:-1,reviewAt=reviewing?(latex?4:3):-1;
    const index=/识别本节/.test(stage)?1:/准备配图|编写笔记/.test(stage)?2:/LaTeX|排版/.test(stage)&&latex?latexAt:/独立审查|修订/.test(stage)&&reviewing?reviewAt:/生成 PDF|核对课件跳转|摘要|取名|完成/.test(stage)?save:0;
    const measured=/识别本节|LaTeX|排版|独立审查/.test(stage) && Number(task.total)>0;
    const count=measured?`本阶段 ${task.completed || 0}/${task.total} ${task.progress_unit==='页'?'页':'批'}`:'';
    return {label:(stage.split(' · ')[0] || '准备开始')+(task.review_round>1?` · 第 ${task.review_round} 轮`:''),count,detail:'',steps,index,percent:measured?Math.min(100,Math.round((task.completed||0)/task.total*100)):null};
  }
  const total=Number(task.total)||0,completed=Number(task.completed)||0;
  const unit=task.progress_unit || (task.type==='course_queue'?(task.course_item?'阶段':'堂课'):task.family==='mineru'?'个文件':'段');
  const count=total?`${task.type==='refinement'?'已保存 ':''}${completed}/${total} ${unit}`:'';
  const percent=done?100:Object.hasOwn(task,'progress')?(task.progress==null?null:Number(task.progress))
    :total?Math.round(completed/total*100):null;
  return {label:stage || (done?'处理完成':task.status==='queued'?'等待开始':task.status==='paused'?'已暂停':task.status==='cancelled'?'已取消，进度已保存':'处理中'),count,
    detail:task.type==='course_queue' && task.course_item?(task.next_action || '继续未完成阶段'):task.type==='refinement' && task.remaining_segments!=null?`剩余 ${task.remaining_segments} 段 · 继续时只补未完成部分`:'',
    percent:percent===null?null:Math.max(0,Math.min(done?100:99,percent))};
}

export function progressGroups(groups) {
  const running=group=>(group.current_tasks||[]).some(t=>activeStatuses.has(t.status));
  return groups.toSorted((a,b)=>Number(running(b))-Number(running(a)));
}
