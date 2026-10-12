const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/smartclass.js'), 'utf8')
  .replace(/\ncheckSession\(\);\nscheduledRefresh\(\);\s*$/, '\nglobalThis.testLogin = {continuePendingReplay, pendingReplayURL, checkSession};');

function environment(returnTo) {
  const elements = new Map(), requests = [], navigations = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value:'',checked:false,dataset:{}, addEventListener(event, callback){this[event]=callback}});
    return elements.get(id);
  };
  const context = {URL, URLSearchParams, Date,
    location:{origin:'http://127.0.0.1:8765',search:'?returnTo='+encodeURIComponent(returnTo),replace:url=>navigations.push(url)},
    document:{getElementById:element,addEventListener(){}}, window:{addEventListener(){}},
    async fetch(url, options) {
      requests.push([url,options?.body ? JSON.parse(options.body) : null]);
      return {ok:true,json:async()=>url==='/api/status' ?
        {identity:{name:'test'},authenticated:true,state:'running'} : {started:true}};
    },
  };
  vm.createContext(context);vm.runInContext(source,context);
  return {context,element,requests,navigations};
}

async function main() {
  const target = 'http://127.0.0.1:8765/course/replay?recording=lesson&seconds=2820';
  for (const action of ['submit','reconnect']) {
    const env=environment(target);
    if(action==='submit') await env.element('login').submit({preventDefault(){}});
    else await env.element('reconnect').onclick();
    assert.equal(env.requests[0][1].replay_only,true);
    assert.deepEqual(env.navigations,[target]);
  }
  const env=environment(target);
  await env.context.testLogin.checkSession();
  assert.equal(env.requests[0][0],'/api/ensure-session');
  assert.equal(env.requests[0][1].replay_only,true);
  assert.deepEqual(env.navigations,[target]);
  env.navigations.length=0;
  vm.runInContext('replayLoginSubmitted = false',env.context);
  env.context.testLogin.continuePendingReplay({identity:{name:'old'},authenticated:true,state:'running'});
  assert.equal(env.navigations.length,0);
  vm.runInContext('replayLoginSubmitted = true',env.context);
  for(const state of ['running','syncing','connected']) {
    env.context.testLogin.continuePendingReplay({identity:{name:'test'},authenticated:true,state});
  }
  assert.equal(env.navigations.length,3);
  env.context.testLogin.continuePendingReplay({identity:{name:'stale'},authenticated:false,state:'connecting'});
  assert.equal(env.navigations.length,3);
  for(const value of ['https://evil.test/course/replay','http://127.0.0.1:8765/other','http://user:pass@127.0.0.1:8765/course/replay']) {
    const env=environment(value);vm.runInContext('replayLoginSubmitted = true',env.context);
    env.context.testLogin.continuePendingReplay({identity:{name:'test'},authenticated:true,state:'connected'});
    assert.equal(env.navigations.length,0);
  }
  console.log('Replay login: authentication independent of queue, dedicated mode, stale identity and return URL validation passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
