const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../..');
const assert=require('node:assert/strict');
const server=http.createServer((req,res)=>{
 let p=new URL(req.url,'http://localhost').pathname;
 if(p==='/bench'){res.setHeader('content-type','text/html');res.end('<link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/vendor/katex/katex.min.css"><script src="/static/vendor/katex/katex.min.js"></script><div id="reader" class="note-summary-reader" style="max-width:850px"></div><textarea id="source" hidden></textarea><nav id="toc"></nav>');return;}
 const file=path.join(root,p);if(!file.startsWith(root+'/')||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
 res.setHeader('content-type',/\.m?js$/.test(file)?'text/javascript':file.endsWith('.css')?'text/css':'application/octet-stream');res.end(fs.readFileSync(file));
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage();await page.goto(`http://127.0.0.1:${server.address().port}/bench`);
 const values=await page.evaluate(async()=>{const {createMarkdownReader}=await import('/static/js/markdown-reader.js');const source=document.querySelector('#source'),container=document.querySelector('#reader');source.value=Array.from({length:140},(_,i)=>`## 知识点 ${i+1}\n\n光电子学半导体发光与探测：${'载流子复合、量子效率和阈值电流。'.repeat(12)}\n\n$$E=h\\nu=\\frac{hc}{\\lambda}$$\n\n`).join('');window.reader=createMarkdownReader({source,container,toc:document.querySelector('#toc')});let render=window.reader.render;let times=[];for(let i=0;i<5;i++){const start=performance.now();await render();void container.offsetHeight;times.push(Math.round(performance.now()-start));}return {times,blocks:container.children.length,text:container.textContent.length};});await page.evaluate(()=>new Promise(requestAnimationFrame));
 const deferred=await page.locator('.deferred-content').count();assert.ok(deferred>0);
 const first=page.locator('.deferred-content').first();const index=await first.getAttribute('data-block');
 await first.evaluate(el=>el.previousElementSibling.focus());await page.keyboard.press('Tab');
 assert.equal(await page.evaluate(()=>document.activeElement.dataset.block),index);
 await page.keyboard.press('Enter');assert.equal(await page.locator('.markdown-block-input').count(),1);
 await page.keyboard.press('Escape');assert.equal(await page.locator('.markdown-block-input').count(),0);
 const same=await page.evaluate(()=>{const node=document.querySelector('#reader').firstElementChild;window.reader.render();return node===document.querySelector('#reader').firstElementChild;});assert.ok(same);
 await page.evaluate(()=>{document.querySelector('#source').value+='\n\n新增内容 <script>alert(1)</script>\n';window.reader.render();});
 assert.equal(await page.locator('#reader script').count(),0);assert.ok(await page.locator('#reader').textContent().then(s=>s.includes('新增内容')));
 const sized=await page.evaluate(()=>{const block=document.querySelector('#reader .markdown-block');block.click();return getComputedStyle(block.querySelector('textarea')).fieldSizing;});
 await page.keyboard.press('Escape');
 const output=process.argv[2]||path.join(root,'output/playwright/modernization/verified.json');fs.mkdirSync(path.dirname(output),{recursive:true});
 await page.screenshot({path:path.join(path.dirname(output),'reader.png'),fullPage:false});
 const report={...values,deferred,keyboardBoundaryPassed:true,cancelRestoresParagraph:true,unchangedDomPreserved:same,sanitized:true,fieldSizing:sized};console.log(JSON.stringify(report));fs.writeFileSync(output,JSON.stringify(report,null,2));await browser.close();server.close();})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
