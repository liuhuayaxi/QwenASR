module.exports=({test,assert,task,COURSE_A})=>{
 const backup={component:'backup'},discard={component:'discard'};
 const pause=async t=>t.p.evaluate(()=>__fixture.settle()),events=t=>t.p.evaluate(()=>__fixture.events);
 const submit=async t=>{await t.p.locator('#discardRecordingButton').click();await t.p.locator('[data-discard-continue]').click();await t.p.locator('[data-discard-confirm]').click();};
 const message=(t,type)=>t.p.evaluate(type=>__controller.receive({type,session_id:'r1'}),type);
 test('60-backup-read-retry-never-creates-new-archive',async t=>{
  await t.p.locator('#exportLibraryBackup').click();await pause(t);assert.equal((await t.posts()).length,0);assert.equal((await t.requests()).filter(r=>r.method==='GET').length,2);
 },{...backup,plans:[{path:'/api/library/backup',method:'GET',error:'network'}]});
 test('61-backup-tab-refresh-cannot-unlock-inflight-create',async t=>{
  await t.plan({path:'/api/library/backup',method:'POST',hold:'create'});await t.p.locator('#exportLibraryBackup').click();await t.waitGate('create');await t.p.locator('[data-settings-tab=library]').click();await pause(t);assert(await t.p.locator('#exportLibraryBackup').isDisabled());await t.resolve('create');assert.equal((await t.posts()).length,1);
 },backup);
 test('62-backup-unknown-post-outcome-is-reconciled-by-read',async t=>{
  await t.plan({path:'/api/library/backup',method:'POST',error:'network'});await t.p.locator('#exportLibraryBackup').click();await pause(t);await t.p.locator('#exportLibraryBackup').click();await pause(t);assert.equal((await t.posts()).length,1);assert.equal((await t.requests()).at(-1).method,'GET');
 },backup);
 test('63-backup-recovery-clears-stale-error',async t=>{
  await t.p.locator('[data-settings-tab=library]').click();await t.p.waitForFunction(()=>!document.querySelector('#exportLibraryBackup').disabled && !document.querySelector('#libraryBackupStatus').textContent.includes('Network request failed'));assert(!(await t.p.locator('#libraryBackupStatus').textContent()).includes('Network request failed'));
 },{...backup,plans:[{path:'/api/library/backup',method:'GET',error:'network'}]});
 test('64-backup-released-result-removes-stale-download',async t=>{
  assert.equal(await t.p.locator('#downloadLibraryBackup').getAttribute('href'),'/fixture/archive.zip');await t.p.evaluate(()=>__fixture.backup={status:'released'});await t.p.locator('[data-settings-tab=library]').click();await t.p.waitForFunction(()=>document.querySelector('#downloadLibraryBackup').getAttribute('href')===null);assert.equal(await t.p.locator('#downloadLibraryBackup').getAttribute('href'),null);assert(await t.p.locator('#downloadLibraryBackup').isHidden());
 },{...backup,backup:{status:'completed',file_count:2,archive_bytes:4000,filename:'fixture.zip',download_url:'/fixture/archive.zip'}});
 test('65-backup-initial-read-prevents-premature-export',async t=>{
  await t.waitGate('read');assert(await t.p.locator('#exportLibraryBackup').isDisabled());await t.resolve('read');assert(!(await t.p.locator('#exportLibraryBackup').isDisabled()));
 },{...backup,skipInitialWait:true,plans:[{path:'/api/library/backup',method:'GET',hold:'read'}]});
 test('70-discard-cancel-first-confirmation',async t=>{
  await t.p.locator('#discardRecordingButton').click();assert.equal(await t.p.evaluate(()=>document.activeElement.hasAttribute('data-discard-cancel')),true);await t.p.locator('[data-discard-cancel]').click();assert.equal((await t.posts()).length,0);
 },discard);
 test('71-discard-cancel-second-confirmation',async t=>{
  await t.p.locator('#discardRecordingButton').click();await t.p.locator('[data-discard-continue]').click();await t.p.keyboard.press('Escape');assert.equal((await t.posts()).length,0);assert.equal(await t.p.locator('#discardRecordingDialog').getAttribute('open'),null);
 },discard);
 test('72-discard-freezes-confirmed-recording-identity',async t=>{
  await t.p.locator('#discardRecordingButton').click();await t.p.evaluate(()=>__fixture.currentSession={id:'r2',title:'Successor recording'});await t.p.locator('[data-discard-continue]').click();await t.p.locator('[data-discard-confirm]').click();await pause(t);assert.equal((await t.posts())[0].body.expected_session_id,'r1');assert((await t.posts())[0].path.includes('/r1/'));
 },discard);
 test('73-discard-completed-event-wins-over-late-post',async t=>{
  await t.plan({path:'/api/recordings/r1/discard',method:'POST',hold:'post'});await submit(t);await t.waitGate('post');await message(t,'recording_discarded');await t.resolve('post');assert.equal(await t.p.evaluate(()=>__controller.isSettled('r1')),true);assert.equal(await t.p.evaluate(()=>__controller.isPending('r1')),false);assert.equal((await events(t)).filter(e=>e.type==='onAccepted').length,1);assert.equal((await t.requests()).filter(r=>r.method==='GET').length,0);
 },discard);
 test('74-discard-late-post-failure-cannot-reverse-completion',async t=>{
  await t.plan({path:'/api/recordings/r1/discard',method:'POST',hold:'post'});await submit(t);await t.waitGate('post');await message(t,'recording_discarded');await t.resolve('post',{status:500,json:{detail:'Late failure'}});assert.equal((await events(t)).filter(e=>e.type==='onFailed').length,0);assert.equal(await t.p.evaluate(()=>__controller.isDiscarded('r1')),true);
 },discard);
 test('75-discard-stale-poll-cannot-resurrect-pending-state',async t=>{
  await t.plan({path:'/api/recordings/r1/discard',method:'GET',hold:'poll'});await submit(t);await t.waitGate('poll');await message(t,'recording_discarded');await t.resolve('poll',{json:{session_id:'r1',status:'discarding'}});assert.equal(await t.p.evaluate(()=>__controller.isPending('r1')),false);assert.equal(await t.p.evaluate(()=>__controller.isSettled('r1')),true);
 },discard);
 test('76-discard-unparseable-post-result-is-polled',async t=>{
  await t.plan({path:'/api/recordings/r1/discard',method:'POST',raw:'incomplete-response'});await submit(t);await pause(t);assert.equal((await events(t)).filter(e=>e.type==='onFailed').length,0);assert.equal(await t.p.evaluate(()=>__controller.isDiscarded('r1')),true);assert.equal((await t.requests()).filter(r=>r.method==='GET').length,1);
 },discard);
 test('77-discard-duplicate-events-complete-once',async t=>{
  await t.plan({path:'/api/recordings/r1/discard',method:'POST',hold:'post'});await submit(t);await t.waitGate('post');await message(t,'recording_discarded');await message(t,'recording_discarded');await t.resolve('post',{json:{session_id:'r1',status:'discarded'}});assert.equal((await events(t)).filter(e=>e.type==='onDiscarded').length,1);
 },discard);
 const continuation={component:'continuation',records:[{id:'r1',title:'Fixture lesson',started_at:'2026-10-16T00:00:00Z'}],tasks:[task('s1')]};
 test('80-continuation-click-meets-backend-request-contract',async t=>{
  await t.p.locator('[data-task=s1]').click();await pause(t);const r=(await t.posts())[0];assert.equal(r.status,200);assert.equal(r.headers['x-qwenasr-client'],'task-center');assert.equal(r.body.id,'s1');assert.equal(r.body.use_saved_ai_settings,true);
 },continuation);
 test('81-continuation-open-recording-errors-are-visible',async t=>{
  await t.p.locator('[data-recording=r1]').click();await pause(t);assert.equal(await t.p.locator('#testToast').textContent(),'Fixture note unavailable');assert.deepEqual(t.errors,[]);
 },{...continuation,openError:'Fixture note unavailable'});
 test('82-continuation-failure-leaves-action-usable',async t=>{
  await t.plan({path:'/api/tasks/action',method:'POST',status:409,json:{detail:'Fixture state changed'}});await t.p.locator('[data-task=s1]').click();await pause(t);assert(!(await t.p.locator('[data-task=s1]').isDisabled()));assert.equal(await t.p.locator('#testToast').textContent(),'Fixture state changed');
 },continuation);
 test('83-continuation-old-refresh-after-action-cannot-show-stale-retry',async t=>{
  await t.plan({path:'/api/tasks',method:'GET',hold:'old'});await t.plan({path:'/api/tasks/action',method:'POST',hold:'operation'});await t.p.evaluate(()=>{void __controller.refresh()});await t.waitGate('old');await t.p.locator('[data-task=s1]').click();await t.waitGate('operation');await t.p.evaluate(()=>{__fixture.tasks[0].status='queued';__fixture.tasks[0].actions=['pause','cancel']});await t.resolve('operation',{json:{ok:true}});await t.resolve('old',{json:{tasks:[task('s1')]}});await t.p.waitForTimeout(80);assert.equal(await t.p.locator('[data-task=s1]').count(),0);
 },continuation);
};
