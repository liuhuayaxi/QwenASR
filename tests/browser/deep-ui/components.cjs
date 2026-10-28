const fs=require('node:fs'),path=require('node:path');
exports.mount=async function(p,{root,options,source,moduleURL,runtime}){
 const native=fs.readFileSync(process.env.QWEN_ASR_INDEX_FIXTURE||path.join(root,'static/index.html'),'utf8');
 const selectors={inference:['#inferenceSettingsGroup','#uploadUseMac'],mineru:['#mineruSettingsGroup'],backup:['section[aria-label="\u65e5\u5e38\u5907\u4efd"]','[data-settings-tab="library"]'],discard:['#discardRecordingButton','#discardRecordingDialog'],continuation:['.continuation-panel']}[options.component];
 await p.setContent('<!doctype html><html><head><meta charset="utf-8"></head><body><main id="testHost"></main><output id="testToast" role="status"></output></body></html>');
 await p.evaluate(({native,selectors,component})=>{
  const d=new DOMParser().parseFromString(native,'text/html');
  document.querySelector('#testHost').innerHTML=selectors.map(s=>{const el=d.querySelector(s);if(!el)throw new Error('Native element missing: '+s);return el.outerHTML}).join('');
  document.documentElement.dataset.testSurface='isolated-native-'+component+'-panel';
 },{native,selectors,component:options.component});
 await p.addStyleTag({content:source('static/ui.css')+'\n'+source('static/custom-controls.css')+'\n#testHost{max-width:900px;margin:32px auto;padding:24px}.settings-group-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.wide{grid-column:1/-1}label{display:grid;gap:8px}.settings-actions,.inline-setting{display:flex;gap:12px;margin-top:20px}.settings-group-heading{margin-bottom:24px}summary{padding:15px 0}#testToast{display:block;margin:20px}'});
 const inference={mineru_provider:'local',asr_provider:'local',knowledge_provider:'local',classroom_power_saving:false,base_url:'http://fixture.invalid:8765'};
 const mineru={tier:'standard',ocr_mode:'auto',image_analysis:true,small_backend:'auto',vlm_engine:'llama-cpp',model_source:'modelscope',processing_window_size:0,vlm_concurrency:4,virtual_vram_gb:0};
 await p.evaluate(f=>window.__fixture=f,{tasks:[],records:[],plans:[],inference,mineru,currentSession:{id:'r1',title:'Fixture recording'},...options});
 await p.addScriptTag({content:runtime});
 const file={inference:'inference-settings',mineru:'mineru-settings',backup:'library-backup',discard:'recording-discard',continuation:'workbench/continuation'}[options.component];
 await p.evaluate(async({url,kind,skip})=>{
  const m=await import(url),f=window.__fixture;f.events=[];
  const toast=text=>{f.events.push({type:'toast',text});document.querySelector('#testToast').textContent=text};
  if(kind==='inference'||kind==='mineru'){
   window.__controller=m[kind==='inference'?'createInferenceSettings':'createMineruSettings']();
   window.__load=()=>window.__controller.load().catch(e=>{f.loadError=e.message});
   const pending=window.__load();if(!skip)await pending;
  }else if(kind==='backup')window.__controller=m.createLibraryBackup();
  else if(kind==='discard'){
   const c={button:document.querySelector('#discardRecordingButton'),dialog:document.querySelector('#discardRecordingDialog'),getSession:()=>f.currentSession,toast};
   for(const name of ['onAccepted','onDiscarded','onRestored','onFailed','onChange'])c[name]=(id,value)=>f.events.push({type:name,id,value:value?.message||value});
   window.__controller=m.createRecordingDiscard(c);window.__controller.update();
  }else{
   window.__controller=m.createContinuation({openRecord:async id=>{f.events.push({type:'openRecord',id});if(f.openError)throw new Error(f.openError)},toast});
   const pending=window.__controller.refresh();if(!skip)await pending;
  }
 },{url:moduleURL('static/js/'+file+'.js'),kind:options.component,skip:options.skipInitialWait});
 if(options.component==='backup'&&!options.skipInitialWait)await p.waitForFunction(()=>!document.querySelector('#exportLibraryBackup').disabled);
};
