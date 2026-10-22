const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root=path.resolve(__dirname,'../..');
const output=path.resolve(process.env.QWEN_ASR_BROWSER_OUTPUT || path.join(root,'output/playwright/transcription-review'));
fs.mkdirSync(output,{recursive:true});
const server=http.createServer((req,res)=>{
  const name=decodeURIComponent(new URL(req.url,'http://localhost').pathname).replace(/^\//,'');
  const file=path.resolve(root,name);
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
  fs.createReadStream(file).pipe(res);
});

async function main(){
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true,executablePath:process.env.QWEN_ASR_BROWSER_EXECUTABLE || undefined});
  const context=await browser.newContext({viewport:{width:1100,height:900}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
  try{
    await page.goto(origin+'/tests/browser/transcription-review.html');
    const first=page.locator('[data-segment-ids="1"]');
    await page.locator('.note-transcript-segment').last().waitFor();
    assert.equal(await page.locator('.note-transcript-segment').count(),2);
    await first.locator('summary').click();
    await first.getByRole('button',{name:'手动修改',exact:true}).click();
    await first.getByLabel('人工核对后的文本').fill('人工核对第一段 40 mW');
    await page.getByRole('button',{name:'模拟段落更新',exact:true}).click();
    assert.equal(await first.locator('details').evaluate(el=>el.open),true);
    assert.equal(await first.getByLabel('人工核对后的文本').inputValue(),'人工核对第一段 40 mW');

    await first.getByRole('button',{name:'保存修改',exact:true}).click();
    await page.getByRole('button',{name:'模拟段落更新',exact:true}).click();
    assert.equal(await first.getByRole('button',{name:'保存修改',exact:true}).isDisabled(),true);
    await page.getByRole('button',{name:'返回保存失败',exact:true}).click();
    await first.getByText('模拟网络失败。原文与建议均保留。',{exact:true}).waitFor();
    assert.equal(await first.getByRole('button',{name:'保存修改',exact:true}).isEnabled(),true);
    assert.equal(await first.getByLabel('人工核对后的文本').inputValue(),'人工核对第一段 40 mW');

    await first.getByRole('button',{name:'保存修改',exact:true}).click();
    await page.getByRole('button',{name:'模拟段落更新',exact:true}).click();
    await page.getByRole('button',{name:'完成延迟保存',exact:true}).click();
    await page.getByText('保存完成；仍有2个独立时间段；剩余1条核对；请求2次',{exact:true}).waitFor();
    assert.equal(await page.locator('.note-transcript-segment').count(),2);
    assert.equal(await first.locator('details').count(),0);
    assert.equal(await page.locator('[data-segment-ids="2"] details').count(),1);
    assert.deepEqual(errors,[]);
    await page.screenshot({path:path.join(output,'saved-separate-segments.png'),fullPage:true});
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({passed:true,scenarios:[
      'independent timestamp ownership','expanded disclosure and draft survive redraw',
      'in-flight save remains single and disabled after redraw','failure restores retry with draft',
      'delayed acknowledgement updates current card and retains neighbor'
    ]},null,2));
    console.log('PASS transcription review: segment ownership, draft retention and delayed save recovery');
  }finally{await context.close();await browser.close();await new Promise(resolve=>server.close(resolve));}
}
main().catch(error=>{console.error(error);server.close();process.exitCode=1;});
