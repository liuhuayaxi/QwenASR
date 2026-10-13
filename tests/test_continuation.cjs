const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const config = moduleUrl(fs.readFileSync(path.join(__dirname,'../static/js/task-config.js'),'utf8'));
const cached = moduleUrl(fs.readFileSync(path.join(__dirname,'../static/js/cached-json.js'),'utf8'));
const implementation = fs.readFileSync(path.join(__dirname,'../static/js/workbench/continuation.js'),'utf8').replace("'../task-config.js'",JSON.stringify(config)).replace("'../cached-json.js'",JSON.stringify(cached));
const modulePromise = import(moduleUrl(implementation));
test('a remote policy does not claim an actual provider or remote connection',async()=>{
 const {recognitionText}=await modulePromise;
 const value=recognitionText({asr_provider:'remote_only'});
 assert.match(value,/识别来源：待确认/);assert.match(value,/远程连接未验证/);assert.match(value,/不自动回退/);
 assert.match(recognitionText({asr_provider:'remote_preferred'},{provider:'local',fallback:true}),/Mac 本机 · 已回退 Mac/);
});
test('tasks from the same recording group by identity rather than title',async()=>{
 const {groupTasks}=await modulePromise;
 const groups=groupTasks([{id:'1',title:'same',recording_id:'a'},{id:'2',title:'renamed',result_url:'/library?recording=a'},{id:'3',title:'same',recording_id:'b'}]);
 assert.equal(groups.length,2);assert.equal(groups[0].tasks.length,2);
});
test('raw text remains readable when note generation fails',async()=>{
 const {recordStages}=await modulePromise;
 const stages=recordStages({status:'interrupted',has_transcript:true},[{label:'课后复习笔记',status:'failed',stage:'生成失败',error:'模型限流',inference:{}}]);
 assert.match(stages[0],/可阅读/);assert.match(stages[1],/尚未生成/);assert.match(stages.at(-1),/来源待确认/);
});
test('course and recording labels cannot inject markup',async()=>{
 const {escapeHtml}=await modulePromise;
 assert.equal(escapeHtml('<img src=x onerror="bad()">'), '&lt;img src=x onerror=&quot;bad()&quot;&gt;');
});
