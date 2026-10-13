const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../static/js/resource-status.js'),'utf8');
const modulePromise=import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
test('aggregate queues and local tasks do not inherit a remote telemetry warning',async()=>{
 const {resourceStatus: status}=await modulePromise;
 assert.equal(status({type:'course_queue',family:'asr',status:'running'}),null);
 assert.equal(status({type:'upload',family:'asr',status:'completed',inference:{provider:'local'}}),null);
});
test('pending, absent and unknown results have different meanings',async()=>{
 const {resourceStatus: status}=await modulePromise;
 const task={type:'upload',family:'asr',status:'running',inference:{provider:'remote'}};
 assert.equal(status(task).tone,'pending');
 assert.match(status(task).text,/任务结束后/);
 assert.match(status({...task,status:'completed'}).text,/未记录/);
 assert.match(status({...task,inference:{provider:'remote',metrics:{gpu_release_state:'unknown'}}}).text,/暂无法确认/);
});
test('explicit failures override legacy success; old true flags still count',async()=>{
 const {resourceStatus: status}=await modulePromise;
 const task=metrics=>({family:'asr',status:'completed',inference:{provider:'remote',metrics}});
 assert.equal(status(task({gpu_release_state:'failed',gpu_release_verified:true})).tone,'error');
 assert.equal(status(task({gpu_release_verified:true})).tone,'ok');
 assert.equal(status(task({gpu_release_state:'verified'})).tone,'ok');
 assert.equal(status(task({gpu_release_state:'unknown',gpu_release_verified:true})).tone,'unknown');
});
