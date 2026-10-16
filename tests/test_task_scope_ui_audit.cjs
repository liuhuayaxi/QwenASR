const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/task-center-view.js'), 'utf8');
const load = () => import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const step = (id, type = 'summary', status = 'failed') => ({id,type,family:type==='summary'?'ai':'asr',status,actions:['retry']});
const group = tasks => ({tasks,current_tasks:tasks,history:[],context_tasks:[]});
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
