const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/task-center-view.js'), 'utf8');
const load = () => import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const step = (id, type = 'summary', status = 'failed') => ({id,type,family:type==='summary'?'ai':'asr',status,actions:['retry']});
const group = tasks => ({tasks,current_tasks:tasks,history:[],context_tasks:[]});
test('single action honors rejected acceptance and displays its reason', async () => {
  const vm = require('node:vm');
  const tasksSource = fs.readFileSync(path.join(__dirname, '../static/js/tasks.js'), 'utf8');
  const perform = tasksSource.slice(tasksSource.indexOf('async function perform('), tasksSource.indexOf("$('statusFilters').addEventListener"));
  for (const result of [{accepted:false,reason:'still stopping'}, {accepted:true}, {}]) {
    const messages=[], toasts=[];
    const context={state:{busy:false,revision:0,selected:new Set()},
      actionFocus:()=>null,render:()=>{},notice:(id,message)=>messages.push(message),
      aiTaskPayload:()=>({}),api:async()=>result,toast:message=>toasts.push(message),
      load:async()=>{},restoreActionFocus:()=>{},actionLabels:{continue:'继续'}};
    await vm.runInNewContext(perform + ";perform([{id:'task'}], 'continue')", context);
    if (result.accepted === false) {
      assert.equal(messages.at(-1),'still stopping');assert.equal(toasts.length,0);
    } else assert.deepEqual(toasts,['任务已加入后台队列']);
  }
});
test('type scope matches family without selecting unrelated steps', async () => {
  const m=await load(), g=group([step('a'),step('b','upload')]);
  assert.deepEqual(m.actionableTasks([g],'summary').map(t=>t.id),['a']);
  assert.deepEqual(m.actionableTasks([g],'asr').map(t=>t.id),['b']);
});
test('shared contextual owner is deduplicated', async () => {
  const m=await load(), queue=step('queue','course_queue','running');
  const groups=[{...group([step('a')]),context_tasks:[queue]},{...group([step('b')]),context_tasks:[queue]}];
  assert.equal(m.actionableTasks(groups).filter(t=>t.id==='queue').length,1);
});
test('completed and recovered receipts are never actionable', async () => {
  const m=await load(); const completed=step('done','summary','completed');
  assert.deepEqual(m.actionableTasks([group([completed])]),[]);
});
test('completed receipt scope keeps original generation and deduplicates', async () => {
  const m=await load(), a={...step('a','summary','completed'),history_generation:'generation-a'}, b=step('b','upload','completed');
  const g={...group([a,b]),history:[a]};
  assert.deepEqual(m.completedTasks([g],'summary'),[a]);
  assert.equal(m.completedTasks([g]).length,2);
});
test('legacy no-type action projection keeps all supported families', async () => {
  const m=await load(), values=[step('a'),step('b','upload')];
  assert.deepEqual(m.actionableTasks([group(values)]).map(t=>t.id),['a','b']);
});
test('scope stays unique with repeated owners in a large projection', async () => {
  const m=await load(); const groups=Array.from({length:500},(_,i)=>group([step('a'+i),step('shared','upload')]));
  assert.equal(m.actionableTasks(groups,'summary').length,500);
  assert.equal(m.actionableTasks(groups,'asr').length,1);
});
