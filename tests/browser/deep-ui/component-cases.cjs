module.exports=({test,assert,task,COURSE_A})=>{
 const inference={component:'inference'},mineru={component:'mineru'};
 const wait=async t=>t.p.evaluate(()=>__fixture.settle());
 const text=(t,id)=>t.p.locator('#'+id).textContent();
 test('40-inference-save-keeps-later-draft',async t=>{
  const p=t.p;await t.plan({path:'/api/inference/settings',method:'PUT',hold:'save'});
  await p.locator('#inferenceAsrProvider').selectOption('remote_only');await p.locator('#saveInferenceSettings').click();await t.waitGate('save');
  await p.locator('#inferenceAsrProvider').selectOption('local');await p.locator('#inferenceRemoteUrl').fill('http://new-node.invalid:8765');
  await t.resolve('save');assert.equal(await p.locator('#inferenceAsrProvider').inputValue(),'local');assert.equal(await p.locator('#inferenceRemoteUrl').inputValue(),'http://new-node.invalid:8765');
  assert.match(await text(t,'inferenceSettingsStatus'),/\u672a\u4fdd\u5b58/);assert.equal(await p.evaluate(()=>__controller.current().asr_provider),'remote_only');
 },inference);
 test('41-inference-test-result-does-not-approve-edited-url',async t=>{
  await t.plan({path:'/api/inference/test',hold:'test'});await t.p.locator('#testInferenceConnection').click();await t.waitGate('test');await t.p.locator('#inferenceRemoteUrl').fill('http://new-node.invalid');await t.resolve('test');
  assert.match(await text(t,'inferenceSettingsStatus'),/\u91cd\u65b0\u6d4b\u8bd5/);
 },inference);
 test('42-inference-save-and-test-are-mutually-exclusive',async t=>{
  await t.plan({path:'/api/inference/settings',method:'PUT',hold:'save'});await t.p.locator('#saveInferenceSettings').click();await t.waitGate('save');assert(await t.p.locator('#testInferenceConnection').isDisabled());await t.resolve('save');assert(!(await t.p.locator('#testInferenceConnection').isDisabled()));
 },inference);
 test('43-inference-initial-load-protects-inputs',async t=>{
  await t.waitGate('load');assert(await t.p.locator('#inferenceRemoteUrl').isDisabled());await t.resolve('load');assert(!(await t.p.locator('#inferenceRemoteUrl').isDisabled()));
 },{...inference,skipInitialWait:true,plans:[{path:'/api/inference/settings',method:'GET',hold:'load'}]});
 test('44-inference-invalid-url-no-put',async t=>{
  await t.p.locator('#inferenceRemoteUrl').fill('not a url');await t.p.locator('#saveInferenceSettings').click();await wait(t);assert.equal((await t.requests()).filter(r=>r.method==='PUT').length,0);
 },inference);
 test('45-inference-failure-retains-form-and-unlocks',async t=>{
  await t.plan({path:'/api/inference/settings',method:'PUT',status:503,json:{detail:'Fixture unavailable'}});await t.p.locator('#inferenceRemoteUrl').fill('http://edited.invalid');await t.p.locator('#saveInferenceSettings').click();await t.p.waitForFunction(()=>!document.querySelector('#saveInferenceSettings').disabled && document.querySelector('#inferenceSettingsStatus').textContent.includes('Fixture unavailable'));assert.equal(await t.p.locator('#inferenceRemoteUrl').inputValue(),'http://edited.invalid');assert(!(await t.p.locator('#saveInferenceSettings').isDisabled()));assert.equal(await t.p.evaluate(()=>__controller.current().base_url),'http://fixture.invalid:8765');
 },inference);
 test('46-inference-failed-reload-disables-stale-snapshot',async t=>{
  await t.plan({path:'/api/inference/settings',method:'GET',status:503,json:{detail:'Fixture unavailable'}});await t.p.evaluate(()=>__load());assert.equal(await t.p.evaluate(()=>__controller.isReady()),false);assert(await t.p.locator('#saveInferenceSettings').isDisabled());
 },inference);
 test('47-inference-dirty-on-input-before-blur',async t=>{
  await t.p.locator('#inferenceRemoteUrl').fill('http://draft.invalid');assert.match(await text(t,'inferenceSettingsStatus'),/\u672a\u4fdd\u5b58/);
 },inference);
 test('50-mineru-save-keeps-later-tier-and-checkbox',async t=>{
  await t.plan({path:'/api/mineru/settings',method:'PUT',hold:'save'});await t.p.locator('#mineruTier').selectOption('advanced');await t.p.locator('#saveMineruSettings').click();await t.waitGate('save');await t.p.locator('#mineruTier').selectOption('basic');await t.p.locator('#mineruImageAnalysis').uncheck();await t.resolve('save');assert.equal(await t.p.locator('#mineruTier').inputValue(),'basic');assert(!(await t.p.locator('#mineruImageAnalysis').isChecked()));assert.match(await text(t,'mineruSettingsStatus'),/\u672a\u4fdd\u5b58/);
 },mineru);
 test('51-mineru-invalid-collapsed-field-revealed-and-focused',async t=>{
  const p=t.p;await p.locator('details > summary').click();await p.locator('#mineruConcurrency').fill('99');await p.locator('details > summary').click();await p.locator('#saveMineruSettings').click();await wait(t);assert.equal((await t.requests()).filter(r=>r.method==='PUT').length,0);assert(await p.locator('details').getAttribute('open')!==null);assert.equal(await p.evaluate(()=>document.activeElement.id),'mineruConcurrency');
 },mineru);
 test('52-mineru-blank-number-is-not-silently-zero',async t=>{
  await t.p.locator('details > summary').click();await t.p.locator('#mineruConcurrency').fill('');await t.p.locator('#saveMineruSettings').click();await wait(t);assert.equal((await t.requests()).filter(r=>r.method==='PUT').length,0);assert.match(await text(t,'mineruSettingsStatus'),/\u6709\u6548/);
 },mineru);
 test('53-mineru-reset-is-draft-until-explicit-save',async t=>{
  await t.p.locator('#mineruTier').selectOption('advanced');await t.p.locator('#resetMineruSettings').click();assert.equal(await t.p.locator('#mineruTier').inputValue(),'standard');assert.equal((await t.requests()).filter(r=>r.method==='PUT').length,0);await t.p.locator('#saveMineruSettings').click();await wait(t);assert.equal((await t.requests()).find(r=>r.method==='PUT').body.settings.tier,'standard');
 },mineru);
 test('54-mineru-tier-controls-preserve-capabilities',async t=>{
  await t.p.locator('#mineruTier').selectOption('basic');assert(await t.p.locator('#mineruVlmEngine').isDisabled());assert(await t.p.locator('#mineruConcurrency').isDisabled());await t.p.locator('#mineruTier').selectOption('advanced');assert(!(await t.p.locator('#mineruVlmEngine').isDisabled()));
 },mineru);
 test('55-mineru-failed-reload-is-not-ready',async t=>{
  await t.plan({path:'/api/mineru/settings',method:'GET',status:503,json:{detail:'Fixture unavailable'}});await t.p.evaluate(()=>__load());assert.equal(await t.p.evaluate(()=>__controller.isReady()),false);assert(await t.p.locator('#saveMineruSettings').isDisabled());
 },mineru);
 test('56-mineru-save-error-retains-draft',async t=>{
  await t.plan({path:'/api/mineru/settings',method:'PUT',status:500,json:{detail:'Fixture unavailable'}});await t.p.locator('#mineruTier').selectOption('advanced');await t.p.locator('#saveMineruSettings').click();await t.p.waitForFunction(()=>!document.querySelector('#saveMineruSettings').disabled && document.querySelector('#mineruSettingsStatus').textContent.includes('Fixture unavailable'));assert.equal(await t.p.locator('#mineruTier').inputValue(),'advanced');assert(!(await t.p.locator('#saveMineruSettings').isDisabled()));
 },mineru);
 test('57-mineru-dirty-number-before-blur',async t=>{
  await t.p.locator('details > summary').click();await t.p.locator('#mineruWindowSize').fill('8');assert.match(await text(t,'mineruSettingsStatus'),/\u672a\u4fdd\u5b58/);
 },mineru);
};
