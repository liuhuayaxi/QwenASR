const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/js/cached-json.js'),'utf8');
const modulePromise=import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
test('unchanged library uses ETag and reuses body only on 304',async()=>{
 const {cachedJson}=await modulePromise;
 let calls=0;
 const fetcher=async(url,options)=>{
   calls++;
   if(calls===1)return {ok:true,status:200,headers:new Headers({etag:'"v1"'}),json:async()=>({records:[{id:'one',has_note:true}]})};
   assert.equal(options.headers['If-None-Match'],'"v1"');
   return {ok:false,status:304,json:async()=>{throw new Error('304 has no body');}};
 };
 const first=await cachedJson('/test-library',{},fetcher);
 assert.equal(await cachedJson('/test-library',{},fetcher),first);
});
