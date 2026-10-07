const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const section = source.slice(source.indexOf('let apiSettingsReady'), source.indexOf('function upgradeKnownContextDefaults'));
const silicon = {base_url:'https://api.siliconflow.cn/v1', model:'saved-model', detailed_summary_prompt:'保留公式',
  function_providers:{summary:{base_url:'https://api.siliconflow.cn/v1',model:'saved-summary',api_mode:'chat_completions'}}};

function environment(disk, browser, available = true) {
  const fields = new Map(), requests = [], notices = [], cache = new Map();
  if (browser) cache.set('settings', JSON.stringify(browser));
  const field = name => {
    if (!fields.has(name)) fields.set(name,{value:'',checked:false});
    return fields.get(name);
  };
  const ctx = {
    els:new Proxy({}, {get:(_, key)=>field(key)}), $:id=>field(id.slice(1)), API_STORAGE_KEY:'settings',
    DEFAULT_CONTEXT_TOKENS:131072, DEFAULT_API_PARAMS:{}, DEFAULT_TITLE_PROMPT:'title',
    DEFAULT_NOTE_SUMMARY_PROMPT:'summary', LEGACY_NOTE_SUMMARY_PROMPT:'legacy', DEFAULT_NOTE_ABSTRACT_PROMPT:'abstract',
    localStorage:{getItem:key=>cache.get(key)||null,setItem:(key,value)=>cache.set(key,value)},
    fetch:async (url, options={}) => {
      requests.push({url,method:options.method||'GET',body:options.body ? JSON.parse(options.body) : null});
      return {ok:available,json:async()=>({settings:disk})};
    },
    restoreFunctionProviders(saved) {
      for (const [mode,p] of Object.entries(saved.function_providers||{})) {
        field(mode+'BaseUrl').value=p.base_url;field(mode+'Model').value=p.model;
      }
    },
    rawProvider:mode=>({base_url:field(mode==='sentence'?'aiBaseUrl':mode+'BaseUrl').value,
      model:field(mode==='sentence'?'aiModel':mode+'Model').value}),
    upgradeKnownContextDefaults(){},renderProviderSummaries(){},toast:message=>notices.push(message),
    setTimeout,clearTimeout,
  };
  vm.createContext(ctx);vm.runInContext(section,ctx);
  return {ctx,field,requests,notices,cache};
}

async function main() {
  for (const cache of [null,{base_url:'https://api.openai.com/v1',model:'gpt-5-mini'}]) {
    const env=environment(silicon,cache);
    await env.ctx.restoreApiSettings();
    assert.equal(env.field('summaryBaseUrl').value,silicon.base_url);
    assert.equal(env.field('summaryModel').value,'saved-summary');
    assert.equal(env.requests.length,1); // Loading a page does not rewrite its settings.
    await env.ctx.saveApiSettings();
    assert.equal(env.requests.at(-1).url,'/api/ai/settings');
    assert.equal(env.requests.at(-1).body.settings.function_providers.summary.model,'saved-summary');
  }
  const migration=environment({},silicon);
  await migration.ctx.restoreApiSettings();
  assert.equal(migration.requests.at(-1).method,'PUT');
  assert.equal(migration.requests.at(-1).body.settings.detailed_summary_prompt,'保留公式');
  const failed=environment(silicon,null,false);
  await failed.ctx.saveApiSettings();
  assert.equal(failed.requests.length,0); // Uninitialized defaults cannot overwrite the disk.
  await failed.ctx.restoreApiSettings();await failed.ctx.saveApiSettings();
  assert.equal(failed.requests.length,1);
  assert.ok(failed.notices.length);
  console.log('AI settings: empty/stale browser cache, durable save, one-time migration and failed-load protection passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
