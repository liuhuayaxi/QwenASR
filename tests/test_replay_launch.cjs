const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../static/js/replay-launch.js'),'utf8')
  .replace('\ninitialize();','\nglobalThis.initializeReplay = initialize;');

async function environment(search, responseStatus = 200, capability = null) {
  const elements = new Map(), requests = [], messages = [], handlers = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id,{hidden:true,dataset:{},addEventListener(){}});
    return elements.get(id);
  };
  const ctx = {URLSearchParams,location:{search,href:'http://localhost:8765/course/replay'+search,origin:'http://localhost:8765'},
    document:{getElementById:element},window:{addEventListener(name,handler){if(name==='message')handlers.push(handler)},postMessage(message){messages.push(message)}},setTimeout:fn=>fn(),
    fetch:async url=>{requests.push(url);return {ok:responseStatus===200,status:responseStatus,json:async()=>responseStatus===200 ?
      {url:'https://njupt.smartclass.cn/PlayPages/Video.aspx?NewID='+'a'.repeat(32)+'&SegementID=101',seconds:0,source_title:'课堂回放'} : {detail:'请登录'}};}};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  if (capability !== null) handlers[0]({source:ctx.window,origin:ctx.location.origin,data:{channel:'qwenasr-userscript-v1',type:'ready',sameTab:capability}});
  await ctx.initializeReplay();
  return {element,requests,messages};
}
async function main() {
  const video = await environment('?video='+'a'.repeat(32));
  assert.equal(video.requests[0],'/course/api/replay-video-target?video='+'a'.repeat(32));
  assert.equal(video.element('replayTime').textContent,'从此节课的开头播放');
  assert.equal(video.element('replayBack').href,'/course/');
  const recording = await environment('?recording=lesson&seconds=60');
  assert.equal(recording.requests[0],'/api/recordings/lesson/replay-target?seconds=60');
  const invalid = await environment('?video=invalid');assert.equal(invalid.requests.length,0);
  const expired = await environment('?video='+'a'.repeat(32),401);
  assert.equal(expired.element('loginReplay').hidden,false);
  assert.equal(expired.element('retryReplay').hidden,false);
  const direct=await environment('?recording=lesson&seconds=60',200,true);
  assert.equal(direct.element('replayNotice').hidden,true);
  assert.equal(direct.messages.filter(message=>message.type==='open').length,1);
  assert.equal(direct.messages.find(message=>message.type==='open').sameTab,true);
  const legacy=await environment('?video='+'a'.repeat(32),200,false);
  assert.equal(legacy.messages.filter(message=>message.type==='open').length,0);
  assert.equal(legacy.element('replayNotice').hidden,false);
  assert.equal(legacy.element('bridgeInstall').hidden,false);
  const html=fs.readFileSync(require('node:path').join(__dirname,'../static/replay-launch.html'),'utf8');
  assert.match(html,/<main[^>]+id="replayNotice"[^>]+hidden/);
  console.log('Replay launch: hidden redirect, same-tab handshake, legacy-script guard, recording/video targets and errors passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
