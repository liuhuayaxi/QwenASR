const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const start=source.indexOf('async function refreshOpenNoteTranscript()');
const end=source.indexOf('\nasync function loadAiTasks()',start);

function harness() {
  let reply,renders=0;
  const state={noteSession:{id:'record-one',segments:[{id:1,raw:'原文'}],refined_transcript:[{segment_ids:[1],text:'原文'}]}};
  const context={state,JSON,encodeURIComponent,fetch:()=>new Promise(resolve=>{reply=resolve;}),renderNoteTranscript:()=>renders++};
  vm.createContext(context);vm.runInContext(source.slice(start,end),context);
  return {state,run:context.refreshOpenNoteTranscript,reply:session=>reply({ok:true,json:async()=>({session})}),renders:()=>renders};
}

test('unchanged polling leaves open review controls and edits in their existing DOM',async()=>{
  const h=harness(),result=h.run();h.reply(structuredClone(h.state.noteSession));await result;
  assert.equal(h.renders(),0);
});

test('a delayed poll cannot overwrite a correction accepted while it was in flight',async()=>{
  const h=harness(),old=structuredClone(h.state.noteSession),result=h.run();
  h.state.noteSession.refined_transcript[0].text='刚确认的文字';
  h.reply(old);await result;
  assert.equal(h.state.noteSession.refined_transcript[0].text,'刚确认的文字');
  assert.equal(h.renders(),0);
});

test('new durable segment results still refresh once and preserve separate IDs',async()=>{
  const h=harness(),updated=structuredClone(h.state.noteSession),result=h.run();
  updated.refined_transcript=[{segment_ids:[1],text:'第一段'},{segment_ids:[2],text:'第二段'}];
  h.reply(updated);await result;
  assert.equal(h.renders(),1);
  assert.deepEqual(h.state.noteSession.refined_transcript,updated.refined_transcript);
});
