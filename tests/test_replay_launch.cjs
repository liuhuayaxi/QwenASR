const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../static/js/replay-launch.js'),'utf8')
  .replace('\ninitialize();','\nglobalThis.initializeReplay = initialize;');

async function environment(search, responseStatus = 200) {
  const elements = new Map(), requests = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id,{hidden:true,dataset:{},addEventListener(){}});
    return elements.get(id);
  };
  const ctx = {URLSearchParams,location:{search,href:'http://localhost:8765/course/replay'+search,origin:'http://localhost:8765'},
    document:{getElementById:element},window:{addEventListener(){},postMessage(){}},setTimeout:fn=>fn(),
    fetch:async url=>{requests.push(url);return {ok:responseStatus===200,status:responseStatus,json:async()=>responseStatus===200 ?
      {url:'https://njupt.smartclass.cn/PlayPages/Video.aspx?NewID='+'a'.repeat(32)+'&SegementID=101',seconds:0,source_title:'课堂回放'} : {detail:'请登录'}};}};
  vm.createContext(ctx);vm.runInContext(source,ctx);await ctx.initializeReplay();
  return {element,requests};
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
  console.log('Replay launch: unimported lesson, recording timestamp, invalid ID and expired login passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
