const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/userscripts/smartclass-replay.user.js'), 'utf8');
const instrumented = source.replace("  if (location.protocol === 'http:'", `  globalThis.testReplay = {
    validateTarget, validTicket, attachLocal, attachPortal, seekMedia, selectMediaSegment, monitorHistoryResume
  }; return;
  if (location.protocol === 'http:'`);
let now = 1000, sequence = 0;
const calls = [];
const clone = value => value === undefined ? undefined : structuredClone(value);

function environment(url, tab = {}, loggedIn = false, credentials = true) {
  const address = new URL(url), messages = [], handlers = [], navigations = [];
  const state = {time:0, segment:'101', restoreOnce:false, resumeUntil:0, seekDelay:0, seeking:false, events:[]};
  const mediaListeners = new Map();
  const emit = event => {for (const callback of mediaListeners.get(event) || []) callback()};
  const player = {duration:()=>3000,currentTime:()=>state.time,currentSrc:()=>`/video/${state.segment}.mp4`,pause(){},
    on(event, callback) {if (!mediaListeners.has(event)) mediaListeners.set(event,new Set());mediaListeners.get(event).add(callback)},
    off(event, callback) {mediaListeners.get(event)?.delete(callback)}};
  const media = {duration:3000, readyState:2, muted:false, get seeking(){return state.seeking},
    get currentTime(){return state.time}, get currentSrc(){return player.currentSrc()},pause(){},
    addEventListener:player.on,removeEventListener:player.off};
  const location = {origin:address.origin, protocol:address.protocol, hostname:address.hostname,
    pathname:address.pathname, hash:address.hash, href:address.href,
    replace(value) {navigations.push(value); calls.push(['navigate',value]);}};
  const window = {
    alert:message=>calls.push(['error',message]),
    userInfo:[{UserID:loggedIn ? 123 : 0}],
    get evaluationStart() {return now < state.resumeUntil ? '' : 0},
    get nowSegementID() {return state.segment},
    getActivePlayObj:()=>player, Pause(){},
    SetCurrentTime(value) {
      state.time=value; calls.push(['seek',value,now]);state.seeking=true;emit('seeking');
      if (state.seekDelay) state.events.push({at:now+state.seekDelay,callback(){state.seeking=false;emit('seeked')}});
      else {state.seeking=false;emit('seeked')}
    },
    addEventListener(name, callback) {if (name==='message') handlers.push(callback)},
    postMessage(value) {
      messages.push(clone(value));
      for (const callback of handlers) callback({source:window,origin:address.origin,data:clone(value)});
    },
    async fetch(url) {
      calls.push(['login',url]);
      return {ok:credentials,status:credentials?200:401,json:async()=>credentials ?
        {login_url:'https://njupt.smartclass.cn/SystemSpace/Redirect.aspx?ticket=ST-once'} : {detail:'请配置南邮账号'}};
    },
  };
  const context = {
    window, unsafeWindow:window, location, URL, Date:{now:()=>now}, AbortSignal,
    crypto:{randomUUID:()=>`${String(++sequence).padStart(8,'0')}-0000-4000-8000-000000000000`},
    document:{readyState:'complete',title:'课堂回放',body:{innerText:''},querySelectorAll:()=>[media],querySelector(selector) {
      return selector.includes('102') ? {click(){state.segment='102'}} : null;
    }},
    setTimeout(callback, milliseconds) {
      if (milliseconds>=180000) return {callback};
      Promise.resolve().then(()=>{
        now+=milliseconds;
        if (milliseconds===300 && state.restoreOnce) {state.time=777;state.restoreOnce=false;emit('seeked')}
        for (const event of [...state.events]) if(event.at<=now) {
          state.events.splice(state.events.indexOf(event),1);event.callback();
        }
        callback();
      }); return {};
    }, clearTimeout(){},
    GM_getTab:callback=>callback(tab), GM_saveTab(data, callback){Object.assign(tab,data);for(const key of Object.keys(tab)) if(!(key in data)) delete tab[key];callback?.()},
  };
  vm.createContext(context); vm.runInContext(instrumented,context);
  return {context,api:context.testReplay,window,location,tab,state,messages,navigations,handlers};
}
const target = {url:'https://njupt.smartclass.cn/PlayPages/Video.aspx?NewID='+'a'.repeat(32)+'&SegementID=101',seconds:2542};
async function request(env, wanted=target) {
  await env.api.attachLocal();
  env.window.postMessage({channel:'qwenasr-userscript-v1',type:'open',target:wanted,sameTab:true});
  for (let i=0;i<20 && !env.navigations.length;i++) await Promise.resolve();
  assert.equal(env.navigations.length,1);
  return env.navigations[0];
}
const localURL = 'http://127.0.0.1:8765/course/replay?recording=lesson&seconds=2542';

async function main() {
  const local = environment(localURL);
  assert.throws(()=>local.api.validateTarget({...target,url:'https://evil.test/PlayPages/Video.aspx'}));
  assert.throws(()=>local.api.validateTarget({...target,seconds:NaN}));
  assert.equal(local.api.validTicket('https://evil.test/?ticket=ST-once'),false);
  const opened = await request(local), tab = local.tab;
  assert.equal(opened,'https://njupt.smartclass.cn/UserSpace/CourseCenter.aspx');
  assert.equal(local.messages.find(message=>message.type==='ready').sameTab,true);
  let portal = environment(opened,tab,false);
  await portal.api.attachPortal();
  assert.equal(portal.navigations[0],localURL);
  const login=environment(localURL,tab,false);await login.api.attachLocal();
  assert.ok(login.navigations[0].includes('ticket=ST-once'));
  assert.equal(tab.qwenasrReplayDirect.ticket,undefined);
  assert.equal(calls.filter(call=>call[0]==='login').length,1);
  portal = environment('https://njupt.smartclass.cn/Default.aspx',tab,true);
  await portal.api.attachPortal();
  assert.equal(portal.navigations[0],target.url);
  portal = environment(target.url,tab,true);
  portal.state.restoreOnce=true;
  await portal.api.attachPortal();
  assert.equal(portal.state.time,2542);
  assert.equal(calls.filter(call=>call[0]==='seek').length,2);
  assert.equal(tab.qwenasrReplayDirect,undefined);

  calls.length=0;
  const second = environment('http://localhost:8765/course/replay');
  portal=environment(await request(second,{...target,seconds:0}),second.tab,true);
  await portal.api.attachPortal();
  portal=environment(target.url,second.tab,true);await portal.api.attachPortal();
  assert.equal(calls.filter(call=>call[0]==='login').length,0);
  assert.equal(portal.state.time,0);assert.equal(second.tab.qwenasrReplayDirect,undefined);

  calls.length=0;
  const denied=environment(localURL,{},false,false);
  portal=environment(await request(denied),denied.tab,false);await portal.api.attachPortal();
  const deniedLogin=environment(portal.navigations[0],denied.tab,false,false);await deniedLogin.api.attachLocal();
  const deniedResult=deniedLogin.messages.filter(message=>message.type==='result').at(-1);
  assert.equal(deniedResult.ok,false);assert.equal(deniedResult.needs_credentials,true);
  assert.equal(deniedLogin.navigations.length,0);assert.equal(denied.tab.qwenasrReplayDirect,undefined);

  for (const [address, body, missingIdentity] of [
    ['https://njupt.smartclass.cn/401.aspx','',false],
    ['https://njupt.smartclass.cn/404.aspx','',true],
    [target.url,'此视频已删除',false],
  ]) {
    calls.length=0;
    const local=environment(localURL);
    portal=environment(await request(local),local.tab,true);await portal.api.attachPortal();
    portal=environment(address,local.tab,true);portal.context.document.body.innerText=body;
    if(missingIdentity) delete portal.window.userInfo;
    await portal.api.attachPortal();
    assert.match(calls.find(call=>call[0]==='error')[1],/权限|已删除|不存在/);
    assert.equal(portal.navigations.length,0);assert.equal(local.tab.qwenasrReplayDirect,undefined);
  }

  calls.length=0;
  const looping=environment(localURL);
  portal=environment(await request(looping),looping.tab,true);await portal.api.attachPortal();
  portal=environment('https://njupt.smartclass.cn/Unknown.aspx',looping.tab,true);await portal.api.attachPortal();
  portal=environment('https://njupt.smartclass.cn/Unknown.aspx',looping.tab,true);await portal.api.attachPortal();
  assert.equal(portal.navigations.length,0);
  assert.match(calls.find(call=>call[0]==='error')[1],/停止重复跳转/);

  calls.length=0;
  const slow=environment(localURL);
  portal=environment(await request(slow),slow.tab,true);await portal.api.attachPortal();
  portal=environment(target.url,slow.tab,true);
  const started=now;portal.state.resumeUntil=now+5000;portal.state.seekDelay=2400;
  await portal.api.attachPortal();
  assert.equal(portal.state.time,2542);
  assert.ok(calls.find(call=>call[0]==='seek')[2]>=started+5000);
  assert.ok(now>=started+5000+2400);

  calls.length=0;
  const segment=environment(localURL);
  portal=environment(await request(segment,{...target,url:target.url.replace('101','102'),seconds:20}),segment.tab,true);
  await portal.api.attachPortal();
  portal=environment(target.url.replace('101','102'),segment.tab,true);await portal.api.attachPortal();
  assert.equal(portal.state.segment,'102');assert.equal(portal.state.time,20);
  assert.equal(segment.tab.qwenasrReplayDirect,undefined);

  const before=calls.length; portal=environment('https://njupt.smartclass.cn/Default.aspx',{},false);
  await portal.api.attachPortal();assert.equal(calls.length,before);

  // Source-page messages cannot create arbitrary jobs, and the old multi-tab request is ignored.
  const blocked=environment(localURL);await blocked.api.attachLocal();
  blocked.handlers[0]({source:{},origin:'https://evil.test',data:{channel:'qwenasr-userscript-v1',type:'open',target,sameTab:true}});
  blocked.window.postMessage({channel:'qwenasr-userscript-v1',type:'open',target});
  assert.equal(blocked.navigations.length,0);
  const expired=environment(localURL);await request(expired);
  now+=180001;
  portal=environment(target.url,expired.tab,true);await portal.api.attachPortal();
  assert.equal(expired.tab.qwenasrReplayDirect,undefined);
  assert.match(calls.at(-1)[1],/已过期/);
  // Closing a tab discards its only job store; no persistent values or cross-tab listeners are used.
  assert.doesNotMatch(source,/GM_(?:openInTab|getValue|setValue|addValueChangeListener)/);
  console.log('Direct replay: one tab, CAS return, late restore, slow seek, segment switch, zero, errors, expiry-safe tab storage and unrelated tabs passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
