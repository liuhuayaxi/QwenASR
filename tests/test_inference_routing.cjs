const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const base = path.join(__dirname, '../static');

test('saved provider survives draft changes; failed load blocks start', async () => {
  const elements = new Map();
  const root = {querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, {value:'', addEventListener(){}});
    return elements.get(selector);
  }};
  let fail = false;
  const ctx = {root, fetcher:async () => ({ok:!fail,json:async () => fail ? {detail:'offline'} :
    {settings:{asr_provider:'remote_only',mineru_provider:'local',base_url:'http://100.81.62.69:8765'}}})};
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(base,'js/inference-settings.js'),'utf8').replace('export function','function'),ctx);
  const panel = vm.runInContext('createInferenceSettings({root, fetcher})',ctx);
  await panel.load();
  root.querySelector('#inferenceAsrProvider').value='local';
  assert.equal(panel.current().asr_provider,'remote_only');
  fail=true;
  await assert.rejects(panel.load(),/offline/);
  assert.equal(panel.isReady(),false);
});

test('task with unknown document progress stays indeterminate', () => {
  const source=fs.readFileSync(path.join(base,'js/tasks.js'),'utf8');
  const ctx={labels:{running:'处理中'},state:{selected:new Set(),busy:false},active:new Set(['running']),
    esc:String,dateLabel:()=>'',friendlyError:()=>'',operationLabel:()=>'',actionLabels:{}};
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(base,'js/mineru-progress-view.js'),'utf8').replaceAll('export function','function'),ctx);
  vm.runInContext(fs.readFileSync(path.join(base,'js/resource-status.js'),'utf8').replaceAll('export function','function'),ctx);
  vm.runInContext(source.slice(source.indexOf('function card('),source.indexOf('function selectionControls(')),ctx);
  const html=ctx.card({id:'fixture',status:'running',family:'mineru',progress:null,total:1,completed:0,files:[{status:'running',progress:null,stage:'OCR 12/24'}]});
  assert.match(html, /role="progressbar"/);
  assert.doesNotMatch(html, /<progress[^>]*value=/);
  assert.doesNotMatch(html,/0%/);
  assert.match(html,/12\/24/);
});

test('completed task has result links and no action buttons even from stale API', () => {
  const source=fs.readFileSync(path.join(base,'js/tasks.js'),'utf8');
  const ctx={labels:{completed:'已完成'},state:{selected:new Set(),busy:false},active:new Set(),
    esc:String,dateLabel:()=>'',friendlyError:()=>'',operationLabel:()=>'',actionLabels:{retry:'重试'}};
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(base,'js/mineru-progress-view.js'),'utf8').replaceAll('export function','function'),ctx);
  vm.runInContext(fs.readFileSync(path.join(base,'js/resource-status.js'),'utf8').replaceAll('export function','function'),ctx);
  vm.runInContext(source.slice(source.indexOf('function card('),source.indexOf('function selectionControls(')),ctx);
  const html=ctx.card({id:'fixture',status:'completed',family:'mineru',actions:['retry'],
    result_url:'/course-files',download_url:'/saved.pdf'});
  assert.doesNotMatch(html, /<button|task-select|class="ui-button/);
  assert.match(html, /href="\/saved.pdf"/);
  assert.match(html, /task-result-link/);
});
