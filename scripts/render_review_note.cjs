// Offline Chromium export using the application's existing Markdown/KaTeX renderer.
const fs=require('node:fs');
const path=require('node:path');
async function main(){
 const request=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
 let playwright;
 for(const root of request.module_roots){try{playwright=require(path.join(root,'playwright'));break;}catch{}}
 if(!playwright) throw new Error('PDF 渲染器缺少 Playwright，请安装 scripts/note-export 的依赖');
 const candidates=[process.env.QWEN_ASR_CHROMIUM,playwright.chromium.executablePath(),'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
 const executablePath=candidates.find(value=>value&&fs.existsSync(value));
 const browser=await playwright.chromium.launch({headless:true,...(executablePath?{executablePath}:{})});
 try{
  const page=await browser.newPage();
  const failedScripts=new Set();
  page.on('requestfailed',request=>{
   if(request.resourceType()==='script')failedScripts.add(new URL(request.url()).pathname);
  });
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.origin!=='https://qwenasr.invalid')return route.abort();
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><link rel="stylesheet" href="/static/vendor/katex/katex.min.css"><style>
    @page{size:A4}*{box-sizing:border-box}body{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;color:#172438;font-size:11pt;line-height:1.75;overflow-wrap:anywhere}
    h1{font-size:23pt;line-height:1.35;color:#183f63}h2{font-size:16pt;color:#1c5275;border-bottom:1px solid #b7cbd7;padding-bottom:5pt;margin-top:20pt}h3{font-size:13pt;color:#254f69;margin-top:15pt}h1,h2,h3,h4{break-after:avoid}p{orphans:3;widows:3}img{display:block;max-width:100%;max-height:175mm;object-fit:contain;margin:10pt auto 3pt}figure{break-inside:avoid;margin:12pt 0}figcaption{font-size:9pt;color:#42586a;line-height:1.5}table{border-collapse:collapse;width:100%;font-size:9pt}th,td{border:1px solid #bfcbd4;padding:5pt;vertical-align:top}thead{display:table-header-group}tr{break-inside:avoid}blockquote{border-left:3pt solid #7197b1;margin:10pt 0;padding:6pt 12pt;background:#f2f6fa}a{color:#315b79;text-decoration:none}.katex-display{overflow:visible;font-size:.95em}pre{white-space:pre-wrap;font-size:9pt}code{font-size:.9em}hr{border:0;border-top:1px solid #ddd}
    body{line-break:strict}p{break-inside:avoid}.lesson-time-range{break-after:avoid}a[href^="#source-"]{white-space:nowrap}li{break-inside:avoid}li>ul,li>ol{break-before:avoid}figure img{max-height:130mm}figcaption p{margin:3pt 0}figcaption ul,figcaption ol{margin:3pt 0;padding-left:17pt}figcaption li{break-inside:avoid}p:has(+figure){break-after:avoid}
    .note-chapter-subtitle{font-size:13pt;line-height:1.6;color:#415b70;margin-top:-7pt;margin-bottom:16pt;break-after:avoid}
    </style><script src="/static/vendor/katex/katex.min.js"></script><main id="note"></main><script type="module">
    import('/static/js/markdown-reader.js').then(({renderMarkdown})=>{window.renderNote=renderMarkdown;})
     .catch(error=>{window.renderNoteError=error.message||String(error);});
    </script></html>`});
   const rel=decodeURIComponent(url.pathname).slice(1);
   // Include every local dependency of the shared Markdown renderer. Keep
   // external requests and unrelated application modules blocked offline.
   if(!rel.startsWith('static/vendor/')&&!['static/js/markdown-reader.js','static/js/ppt-jump.js','static/js/rendering.js'].includes(rel))return route.abort();
   const local=path.resolve(request.repo_root,rel);
   if(!local.startsWith(path.resolve(request.repo_root,'static')+path.sep))return route.abort();
   if(!fs.existsSync(local))return route.fulfill({status:404,body:'Offline renderer asset missing'});
   return route.fulfill({path:local,contentType:local.endsWith('.js')?'text/javascript':undefined});
  });
  await page.goto('https://qwenasr.invalid/');
  await page.waitForFunction(()=>typeof window.renderNote==='function'||Boolean(window.renderNoteError));
  const initializationError=await page.evaluate(()=>window.renderNoteError||'');
  if(initializationError)throw new Error('PDF 渲染器初始化失败：'+initializationError+
   (failedScripts.size?'；未能加载本地模块：'+[...failedScripts].join(', '):''));
  await page.evaluate(markdown=>{
   document.querySelector('#note').innerHTML=window.renderNote(markdown);
   for(const block of document.querySelectorAll('p,li')){
    if(!/^\s*授课时间段\s*[:：]/.test(block.textContent))continue;
    block.classList.add('lesson-time-range');
    const list=block.parentElement;
    if(block.tagName==='LI'&&['UL','OL'].includes(list.tagName)&&list.children.length===1)list.classList.add('lesson-time-range');
   }
   for(const p of document.querySelectorAll('p')){
    const picture=p.querySelector(':scope > img');
    if(picture&&p.querySelectorAll('img').length===1){
     const figure=document.createElement('figure');const caption=document.createElement('figcaption');
     picture.remove();figure.append(picture);
     while(p.firstChild)caption.append(p.firstChild);
     // Only explicitly labelled adjacent captions belong to this image.
     // Writers also use blockquotes for 图注/读图说明; unrelated quotations
     // remain ordinary blocks and retain their own page-flow semantics.
     for(let next=p.nextElementSibling;next;next=p.nextElementSibling){
      const text=next.textContent.trim();
      const prose=next.tagName==='P'&&/^(图\s*\d|图注|读图|来源图)/.test(text);
      const reading=['UL','OL'].includes(next.tagName)&&/^读图说明/.test(text);
      const quoted=next.tagName==='BLOCKQUOTE'&&/^(?:图注|读图说明)(?:\s|[:：]|$)/.test(text);
      if(!prose&&!reading&&!quoted)break;
      caption.append(next);
     }
     if(caption.textContent.trim())figure.append(caption);
     p.replaceWith(figure);
    }
   }
  },request.markdown);
  await page.evaluate(chapters=>{
   if(!Array.isArray(chapters)||!chapters.length)return;
   const subtitle=document.createElement('p');subtitle.className='note-chapter-subtitle';
   subtitle.textContent=chapters.join('；');
   const title=document.querySelector('#note h1');
   if(title)title.after(subtitle);else document.querySelector('#note').prepend(subtitle);
  },request.chapters||[]);
  await page.evaluate(async()=>{await document.fonts.ready;await Promise.all([...document.images].map(im=>im.decode()));});
  const problems=await page.evaluate(()=>({math:document.querySelectorAll('.katex-error').length,broken:[...document.images].filter(i=>!i.naturalWidth).length,images:document.images.length,
   captionBlockquotes:document.querySelectorAll('figcaption > blockquote').length,
   standaloneBlockquotes:document.querySelectorAll('#note > blockquote').length,
   timeMetadataParagraphs:document.querySelectorAll('p.lesson-time-range').length,
   timeMetadataLists:document.querySelectorAll('ul.lesson-time-range,ol.lesson-time-range').length}));
  // A missing figure fails the export. A formula KaTeX cannot parse stays
  // visible as its source (counted in the result) instead of losing the PDF.
  if(problems.broken)throw new Error('PDF 图片未完成渲染 '+JSON.stringify(problems));
  await page.pdf({path:request.output,format:'A4',printBackground:true,displayHeaderFooter:true,
   headerTemplate:'<span></span>',footerTemplate:'<div style="font-size:8px;width:100%;text-align:center;color:#68778a">课后复习笔记 · <span class="pageNumber"></span> / <span class="totalPages"></span></div>',
   margin:{top:'17mm',right:'18mm',bottom:'18mm',left:'18mm'},preferCSSPageSize:true});
  process.stdout.write(JSON.stringify({renderer:'chromium-markdown-katex-v1',...problems}));
 }finally{await browser.close();}
}
main().catch(error=>{process.stderr.write(error.message+'\n');process.exit(1)});
