const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/js/task-center-view.js'), 'utf8');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
const task = (id, status, extra = {}) => ({id, status, title: '同名课堂', label: '课后笔记', actions: ['continue'], ...extra});
test('legacy groups use record identity and never combine independent PDFs by course name', async () => {
 const {taskGroups} = await modulePromise;
 const groups = taskGroups({tasks: [task('a','running',{recording_id:'r1'}),task('b','pending',{result_url:'/library?recording=r1'}),task('c','failed',{recording_id:'r2'}),task('pdf1','failed',{category:'课程'}),task('pdf2','completed',{category:'课程'})]});
 assert.equal(groups.length,4);assert.equal(groups[0].current_tasks.length,2);
 assert.notEqual(groups[2].id,groups[3].id);
});
test('a recovered error keeps its original receipt and is excluded from current errors and actions', async () => {
 const {taskGroups,groupCounts,actionableTasks} = await modulePromise;
 const original=task('old','failed',{error:'old failure',recovered:true,display_status:'recovered'});
 const groups=taskGroups({tasks:[original]});
 assert.equal(groups[0].status,'completed');assert.equal(groups[0].history[0].status,'failed');
 assert.equal(groups[0].errors.length,0);assert.equal(groupCounts(groups).attention,0);
 assert.equal(groupCounts(groups).recovered,1);assert.deepEqual(actionableTasks(groups),[]);
 assert.equal(original.error,'old failure');assert.deepEqual(original.actions,['continue']);
});
test('pending work and review requirements are separate from failures', async () => {
 const {taskGroups,groupCounts,filterTaskGroups} = await modulePromise;
 const groups=taskGroups({tasks:[task('pending','pending'),task('review','pending',{attention_kind:'review',review_required_blocks:27})]});
 const counts=groupCounts(groups);assert.equal(counts.attention,0);assert.equal(counts.review,1);assert.equal(counts.pending,2);
 assert.equal(filterTaskGroups(groups,{filter:'review'}).length,1);
});
test('remaining review blocks do not relabel an active or failed step as waiting review', async () => {
 const {taskGroups,groupCounts} = await modulePromise;
 const groups=taskGroups({tasks:[task('running','running',{review_required_blocks:4}),task('failed','failed',{review_required_blocks:4})]});
 assert.equal(groupCounts(groups).review,0);assert.equal(groupCounts(groups).attention,1);assert.equal(groupCounts(groups).active,1);
 const vm=require('node:vm'),source=fs.readFileSync(path.join(__dirname,'../static/js/tasks.js'),'utf8');
 const context={labels:{running:'进行中',failed:'失败'}};vm.createContext(context);
 vm.runInContext(source.slice(source.indexOf('function taskStatus('),source.indexOf('function card(')),context);
 assert.equal(context.taskStatus({status:'running',review_required_blocks:4}),'进行中');
 assert.equal(context.taskStatus({status:'failed',review_required_blocks:4,attention_kind:'review'}),'失败');
 assert.equal(context.taskStatus({status:'pending',review_required_blocks:4}),'待核对');
});
test('the server group contract retains errors next to active processing', async () => {
 const {taskGroups,groupCounts,filterTaskGroups} = await modulePromise;
 const groups=taskGroups({groups:[{id:'r1',title:'课堂',status:'running',tasks:[task('run','running'),task('bad','failed')],current_tasks:[task('run','running'),task('bad','failed')],history:[],errors:[{id:'bad',error:'bad'}]}]});
 assert.equal(groupCounts(groups).active,1);assert.equal(groupCounts(groups).attention,1);
 assert.equal(filterTaskGroups(groups,{filter:'attention'}).length,1);
});
test('a failure-priority headline does not hide another running step from the active filter', async () => {
 const {taskGroups,groupCounts,filterTaskGroups} = await modulePromise;
 const running=task('run','running'), failed=task('bad','failed');
 const groups=taskGroups({groups:[{id:'mixed',status:'failed',tasks:[running,failed],current_tasks:[running,failed]}]});
 assert.equal(groupCounts(groups).active,1);assert.equal(groupCounts(groups).attention,1);
 assert.equal(filterTaskGroups(groups,{filter:'active'}).length,1);
 assert.equal(filterTaskGroups(groups,{filter:'attention'}).length,1);
});
test('filters and totals count whole lessons while matching child types and filenames', async () => {
 const {taskGroups,groupCounts,filterTaskGroups} = await modulePromise;
 const groups=taskGroups({tasks:[task('a','running',{recording_id:'r',type:'summary',category:'课程A'}),task('b','pending',{recording_id:'r',type:'refinement',category:'课程A'}),task('pdf','failed',{type:'pdf',files:[{source_name:'测试讲义.pdf'}]})]});
 assert.equal(groupCounts(groups).all,2);
 assert.equal(filterTaskGroups(groups,{filter:'all',type:'refinement',category:'课程A'}).length,1);
 assert.equal(filterTaskGroups(groups,{filter:'all',query:'测试讲义'}).length,1);
 assert.equal(filterTaskGroups(groups,{filter:'all',category:'不存在'}).length,0);
});
test('shared course queue controls are deduplicated by durable task identity', async () => {
 const {taskGroups,actionableTasks} = await modulePromise;
 const q=task('course_queue','running',{type:'course_queue'});
 const groups=taskGroups({groups:[{id:'r1',tasks:[q],current_tasks:[q]},{id:'r2',tasks:[q],current_tasks:[q]}]});
 assert.equal(actionableTasks(groups).length,1);
});
test('context queue controls retain their original action identity and are deduplicated', async () => {
 const {taskGroups,actionableTasks} = await modulePromise;
 const q=task('course_queue','running',{type:'course_queue',is_context:true,actions:['pause']});
 const groups=taskGroups({groups:['r1','r2'].map(id=>({id,tasks:[q],current_tasks:[],context_tasks:[q]}))});
 assert.equal(actionableTasks(groups).length,1);assert.equal(actionableTasks(groups)[0].id,'course_queue');
 assert.deepEqual(actionableTasks(groups)[0].actions,['pause']);
});
test('review-only cards show a check link and count, with compute actions under explicit reprocessing', () => {
 const vm=require('node:vm'),source=fs.readFileSync(path.join(__dirname,'../static/js/tasks.js'),'utf8');
 const context={labels:{pending:'待处理'},actionLabels:{continue:'继续',retry:'重试',cancel:'取消'},state:{selected:new Set(),busy:false},active:new Set(),esc:text=>String(text??''),dateLabel:()=>'',friendlyError:()=>'',operationLabel:()=>'',aiActivityView:()=>null,legacyMineruProgress:()=>null,mineruEstimateLabel:()=>'',resourceStatus:()=>null};
 vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function taskStatus('),source.indexOf('function selectionControls(')),context);
 const review=task('refinement:r','pending',{attention_kind:'review',review_required_blocks:45,total:45,completed:0,progress:0,stage:'转写已完成，数字或公式待核对',result_url:'/library?recording=r',actions:['continue','retry','cancel']});
 const html=context.card(review);
 assert.match(html,/45 段待核对/);assert.doesNotMatch(html,/0%|已完成 0\/45|<progress|role="progressbar"/);
 assert.match(html,/<a href="\/library\?recording=r"[^>]*>查看并核对<\/a>/);
 assert.match(html,/<details class="task-details task-reprocess"[\s\S]*重新处理选项[\s\S]*data-action="continue"[\s\S]*重新处理（继续）[\s\S]*<\/details>/);
 const ordinary=task('normal','running'),queue=task('course_queue','running',{is_context:true});
 assert.deepEqual(Array.from(context.defaultBulkTasks([review,ordinary,queue]),item=>item.id),['normal']);
 assert.equal(review.id,'refinement:r');assert.deepEqual(review.actions,['continue','retry','cancel']);
});
test('completed and cancelled history stays available without becoming daily errors', async () => {
 const {taskGroups,filterTaskGroups} = await modulePromise;
 const groups=taskGroups({tasks:[task('done','completed'),task('cancel','cancelled')]});
 assert.equal(filterTaskGroups(groups,{filter:'daily'}).length,0);
 assert.equal(filterTaskGroups(groups,{filter:'all'}).length,2);
 assert.equal(filterTaskGroups(groups,{filter:'completed'}).length,1);
});
