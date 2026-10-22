const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/js/asr-resource-view.js'), 'utf8');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const now = 100000;
const phase = {state:'running', execution:{provider:'remote',updated_at:90}};
const live = () => ({provider:'remote',updated_at:90, usage:{device:'Windows',scope:'host',sampled_at:100,cpu_percent:25,gpu_percent:74}});

test('shows device with actual host CPU and GPU occupancy', async()=>{
  const {asrResourceView:view}=await modulePromise;
  assert.equal(view(phase,live(),now).text,'Windows 转写 · CPU 25% · GPU 74%');
  assert.match(view(phase,live(),now).title,/整机/);
});
test('zero is valid while missing and stale values are unavailable',async()=>{
  const {asrResourceView:view}=await modulePromise;
  const data=live();data.usage.cpu_percent=null;data.usage.gpu_percent=0;
  assert.equal(view(phase,data,now).text,'Windows 转写 · CPU 暂不可用 · GPU 0%');
  assert.equal(view(phase,data,now+16000).text,'Windows 转写 · CPU 暂不可用 · GPU 暂不可用');
});
test('Mac fallback cannot inherit Windows measurements',async()=>{
  const {asrResourceView:view}=await modulePromise;
  const fallback={state:'running',execution:{provider:'local',fallback:true,updated_at:95}};
  assert.equal(view(fallback,live(),now).text,'Mac 转写（已回退） · CPU 暂不可用 · GPU 暂不可用');
  const data={provider:'local',fallback:true,updated_at:95,usage:{...live().usage,device:'Mac'}};
  assert.equal(view(fallback,data,now).text,'Mac 转写（已回退） · CPU 25% · GPU 74%');
});
test('a new audio segment on the same machine retains fresh host measurements',async()=>{
  const {asrResourceView:view}=await modulePromise;
  const nextSegment={...phase,execution:{provider:'remote',updated_at:99}};
  assert.equal(view(nextSegment,live(),now).text,'Windows 转写 · CPU 25% · GPU 74%');
});
test('wrong host or WSL-only CPU scope is never displayed',async()=>{
  const {asrResourceView:view}=await modulePromise;
  for(const patch of [{device:'Mac'},{scope:'wsl'}]) {
    const data=live();Object.assign(data.usage,patch);
    assert.equal(view(phase,data,now).text,'Windows 转写 · CPU 暂不可用 · GPU 暂不可用');
  }
});
test('finished/paused steps show provenance without pretending utilization is live',async()=>{
  const {asrResourceView:view}=await modulePromise;
  for(const state of ['completed','paused'])assert.equal(view({...phase,state},live(),now).text,'Windows 转写');
  assert.equal(view({state:'running'},null,now).text,'转写设备待确认');
});
