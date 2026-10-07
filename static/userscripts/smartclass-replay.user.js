// ==UserScript==
// @name         QwenASR 南邮回放跳转
// @namespace    qwenasr.local/smartclass-replay
// @version      1.0.1
// @description  从录音库打开南邮 Smart Class，检查浏览器登录并准确定位课堂时间。
// @match        http://127.0.0.1/course/replay*
// @match        http://localhost/course/replay*
// @match        https://njupt.smartclass.cn/*
// @run-at       document-end
// @noframes
// @grant        unsafeWindow
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_addValueChangeListener
// @grant        GM_removeValueChangeListener
// @grant        GM_getTab
// @grant        GM_saveTab
// @grant        GM_openInTab
// ==/UserScript==

(() => {
  'use strict';
  const PORTAL = 'https://njupt.smartclass.cn';
  const CHANNEL = 'qwenasr-userscript-v1';
  const PREFIX = 'qwenasr-replay/job/';
  const TAB_KEY = 'qwenasrReplayJob';
  const VERSION = '1.0.1';
  const MAX_NAVIGATIONS = 2;
  const page = unsafeWindow;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const jobKey = id => PREFIX + id;
  const tabInfo = () => new Promise(resolve => GM_getTab(resolve));
  const saveTab = data => new Promise(resolve => GM_saveTab(data, resolve));

  function validateTarget(input) {
    const url = new URL(input?.url || '');
    if (url.origin !== PORTAL || url.username || url.password || url.pathname !== '/PlayPages/Video.aspx'
        || !/^[a-f0-9]{32}$/i.test(url.searchParams.get('NewID') || '')
        || !/^[1-9][0-9]*$/.test(url.searchParams.get('SegementID') || '')
        || !Number.isFinite(input.seconds) || input.seconds < 0 || input.seconds > 7200) {
      throw new Error('回放地址或时间无效');
    }
    const clean = new URL('/PlayPages/Video.aspx', PORTAL);
    clean.searchParams.set('NewID', url.searchParams.get('NewID'));
    clean.searchParams.set('SegementID', url.searchParams.get('SegementID'));
    return {url:clean.href, seconds:input.seconds};
  }
  function validTicket(value) {
    try {
      const url = new URL(value);
      return url.origin === PORTAL && !url.username && !url.password
        && url.pathname === '/SystemSpace/Redirect.aspx' && Boolean(url.searchParams.get('ticket'));
    } catch (_) { return false; }
  }
  function updateJob(id, patch) {
    const job = GM_getValue(jobKey(id));
    if (job) GM_setValue(jobKey(id), {...job, ...patch});
  }
  function usableJob(id) {
    const job = GM_getValue(jobKey(id));
    return job && job.expires > Date.now() && !['done','error'].includes(job.phase) ? job : null;
  }

  function attachLocal() {
    let current = null;
    const send = payload => window.postMessage({channel:CHANNEL, ...payload}, location.origin);
    window.addEventListener('message', event => {
      if ((event.source !== window && event.source !== page) || event.origin !== location.origin
          || event.data?.channel !== CHANNEL) return;
      if (event.data.type === 'probe') { send({type:'ready', version:VERSION}); return; }
      if (event.data.type !== 'open' || current) return;
      let target;
      try { target = validateTarget(event.data.target); }
      catch (error) { send({type:'result', ok:false, error:error.message}); return; }
      const id = crypto.randomUUID();
      const key = jobKey(id);
      let authenticating = false;
      const finish = result => {
        if (current?.id !== id) return;
        clearTimeout(current.timer);
        GM_removeValueChangeListener(current.listener);
        GM_deleteValue(key);
        current = null;
        send({type:'result', ...result});
      };
      const observe = async (_key, _old, job) => {
        if (current?.id !== id || !job) return;
        if (job.message) send({type:'progress', text:job.message});
        if (job.phase === 'done') { finish({ok:true, seconds:job.observed}); return; }
        if (job.phase === 'error') { finish({ok:false, error:job.error, needs_credentials:job.needs_credentials}); return; }
        if (job.phase !== 'need_login' || authenticating) return;
        authenticating = true;
        try {
          const response = await page.fetch('/course/api/browser-login', {
            method:'POST', headers:{'X-QwenASR-Client':'smartclass','Content-Type':'application/json'},
            body:'{}', cache:'no-store', signal:AbortSignal.timeout(90000),
          });
          const data = await response.json();
          if (current?.id !== id || !usableJob(id)) return;
          if (!response.ok || !validTicket(data.login_url)) {
            updateJob(id,{phase:'error', needs_credentials:response.status === 401,
              error:typeof data.detail === 'string' ? data.detail : '学校未返回有效登录票据'});
            return;
          }
          updateJob(id,{phase:'ticket_ready', ticket:data.login_url});
        } catch (_) {
          if (current?.id === id) updateJob(id,{phase:'error', error:'登录服务暂时无法连接，请检查本地服务与校园网络'});
        }
      };
      const listener = GM_addValueChangeListener(key, observe);
      current = {id, listener, timer:setTimeout(() => finish({ok:false,
        error:'回放跳转超时，请确认油猴脚本已在南邮网站启用，并检查校园网络'}), 180000)};
      GM_setValue(key,{target, phase:'checking', expires:Date.now()+180000, authAttempts:0, navigationAttempts:0,
        message:'正在检查浏览器中的 Smart Class 登录…'});
      try {
        const replay = GM_openInTab(PORTAL + '/UserSpace/CourseCenter.aspx#qwenasr-replay=' + id,
          {active:true, setParent:true});
        replay.onclose = () => {
          if (current?.id === id) finish({ok:false, error:'回放窗口已关闭，可以重新跳转'});
        };
      } catch (_) { finish({ok:false, error:'无法打开回放窗口，请检查油猴是否允许打开标签页'}); }
    });
    send({type:'ready', version:VERSION});
  }

  function activePlayer() {
    try { return page.getActivePlayObj?.(); } catch (_) { return null; }
  }
  function primaryMedia() {
    const videos = [...document.querySelectorAll('video')];
    return videos.find(video => !video.muted && video.readyState >= 1)
      || videos.find(video => video.readyState >= 1);
  }
  function inspectMedia() {
    const player = activePlayer(), media = primaryMedia();
    const duration = player?.duration?.() || media?.duration;
    return {ready:Number.isFinite(duration) && duration > 0, duration,
      segment:String(page.nowSegementID || ''),
      mediaKey:String(player?.currentSrc?.() || media?.currentSrc || '').split('?')[0]};
  }
  function portalError() {
    if (document.readyState === 'loading') return '';
    const path = location.pathname.toLowerCase();
    // The portal also sends anonymous visitors to 401.aspx; let them log in once.
    if (path === '/401.aspx' && Array.isArray(page.userInfo) && !page.userInfo[0]?.UserID) return '';
    if (/\/(?:401|403|404|500|error|accessdenied|notfound)(?:\.aspx)?\/?$/.test(path)) {
      return '学校拒绝访问此回放，视频可能无权限、已删除或不存在';
    }
    const text = [document.title, document.querySelector('#tip_word')?.textContent,
      document.querySelector('.center-box')?.textContent].filter(Boolean).join(' ');
    const body = document.body?.innerText || '';
    if (/无权限访问|无权访问|拒绝访问|访问被拒绝|主讲教师未授权|(?:视频|回放|资源).{0,12}(?:已删除|不存在|未找到)|access denied|not found/i.test(text)
        || /无权限访问|无权访问|拒绝访问|访问被拒绝|主讲教师未授权|(?:视频|回放|资源).{0,12}(?:已删除|不存在|未找到)/.test(body)) {
      return '学校拒绝访问此回放，视频可能无权限、已删除或不存在';
    }
    return '';
  }
  function navigateToTarget(id, url) {
    const job = usableJob(id);
    if (!job) throw new Error('回放请求已经结束');
    const attempts = Number(job.navigationAttempts) || 0;
    if (attempts >= MAX_NAVIGATIONS) throw new Error('学校未能打开目标回放，已停止重复跳转；请检查视频权限或是否已删除');
    updateJob(id,{phase:'loading', navigationAttempts:attempts + 1,
      message:'登录已确认，正在打开对应课堂回放…'});
    location.replace(url);
  }
  function historyResumeReady() {
    // Current school Video.js sets evaluationStart in the history response,
    // including zero when no saved progress exists.
    if ('evaluationStart' in page) return page.evaluationStart !== '' && Number.isFinite(Number(page.evaluationStart));
    return !(page.jQuery?.active > 0);
  }
  function monitorHistoryResume() {
    const services = page.services, original = services?.CreateGetUserViewVideoInfo;
    let pending = 0;
    const wrapped = function (...args) {
      const request = original.apply(this, args), send = request.send;
      request.send = function (...sendArgs) {
        pending += 1;
        let settled = false;
        const finish = () => { if (!settled) { settled = true; pending -= 1; } };
        try {
          const response = send.apply(this, sendArgs);
          if (typeof response.always === 'function') response.always(finish);
          else { response.done?.(finish); response.fail?.(finish); }
          return response;
        } catch (error) { finish(); throw error; }
      };
      return request;
    };
    if (typeof original === 'function') services.CreateGetUserViewVideoInfo = wrapped;
    return {ready:() => !pending && historyResumeReady(), close:() => {
      if (services?.CreateGetUserViewVideoInfo === wrapped) services.CreateGetUserViewVideoInfo = original;
    }};
  }
  async function until(id, condition, timeout = 60000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (!usableJob(id)) throw new Error('回放请求已经结束，请重新跳转');
      const error = portalError();
      if (error) throw new Error(error);
      const result = condition();
      if (result) return result;
      await wait(300);
    }
    throw new Error('Smart Class 加载超时，请检查校园网络后重试');
  }
  async function selectMediaSegment(id, target) {
    await until(id, () => inspectMedia().ready);
    const wanted = new URL(target.url).searchParams.get('SegementID');
    const before = inspectMedia();
    if (before.segment === wanted) return;
    const entry = document.querySelector(`.fragment_wrap li[index="${wanted}"]`);
    if (!entry) throw new Error('Smart Class 没有找到对应视频片段，请核对课程来源');
    entry.click();
    await until(id, () => {
      const state = inspectMedia();
      return state.ready && state.segment === wanted && state.mediaKey && state.mediaKey !== before.mediaKey;
    });
  }
  async function seekMedia(id, seconds, resume = {ready:historyResumeReady}) {
    await until(id, () => resume.ready());
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (!usableJob(id)) throw new Error('回放请求已结束');
      const player = activePlayer(), media = primaryMedia();
      const duration = player?.duration?.() || media?.duration;
      if (!Number.isFinite(duration) || seconds > duration + 1) throw new Error('目标时间超过当前视频片段');
      const wanted = Math.min(seconds, Math.max(0, duration-.05));
      const observedTime = () => player?.currentTime?.() ?? media?.currentTime;
      let seeked = Math.abs(observedTime() - wanted) < 1 && !media?.seeking;
      let stableSince = null;
      const onSeeking = () => { seeked = false; stableSince = null; };
      const onSeeked = () => { seeked = true; stableSince = null; };
      const eventSource = media || player;
      const listen = (event, callback) => media ? media.addEventListener(event, callback) : player?.on?.(event, callback);
      const unlisten = (event, callback) => media ? media.removeEventListener(event, callback) : player?.off?.(event, callback);
      listen('seeking', onSeeking); listen('seeked', onSeeked);
      try {
        if (player && typeof page.SetCurrentTime === 'function') {
          page.Pause?.();
          page.SetCurrentTime(wanted);
          player.pause?.();
        } else if (media) {
          for (const video of document.querySelectorAll('video')) {
            if (video.readyState >= 1) { video.pause(); video.currentTime = Math.min(wanted, Math.max(0, video.duration-.05)); }
          }
        } else throw new Error('Smart Class 播放器尚未就绪');
        const result = await until(id, () => {
          const observed = observedTime();
          if (!resume.ready() || media?.seeking || (media && media.readyState < 2)) {
            stableSince = null; return null;
          }
          // If the player exposes no event API, require a stable ready clock.
          const canObserve = media || typeof eventSource?.on === 'function';
          if (canObserve && !seeked) return null;
          if (!Number.isFinite(observed)) return null;
          if (Math.abs(observed - wanted) >= 1) return {retry:true};
          if (stableSince === null) stableSince = Date.now();
          return Date.now() - stableSince >= 600 ? {observed} : null;
        }, 30000);
        if (!result.retry) return result.observed;
      } finally {
        unlisten('seeking', onSeeking); unlisten('seeked', onSeeked);
      }
    }
    throw new Error('Smart Class 未能保持所选时间，请重新跳转');
  }

  async function attachPortal() {
    const tab = await tabInfo();
    const incoming = /^#qwenasr-replay=([a-f0-9-]{36})$/i.exec(location.hash)?.[1];
    const id = incoming || tab[TAB_KEY];
    if (!id || !usableJob(id)) return;
    tab[TAB_KEY] = id;
    await saveTab(tab);
    let resume;
    try {
      const job = usableJob(id), target = validateTarget(job.target);
      // A normal school tab has no per-tab job ID and is left untouched.
      await until(id, () => document.readyState !== 'loading' && Array.isArray(page.userInfo));
      if (!page.userInfo[0]?.UserID) {
        if (job.authAttempts >= 1) throw new Error('浏览器登录未成功，请重新配置南邮账号后再试');
        updateJob(id,{phase:'need_login', authAttempts:1,
          message:'浏览器尚未登录，正在使用本机保存的南邮账号登录…'});
        const auth = await until(id, () => {
          const value = GM_getValue(jobKey(id));
          if (value?.phase === 'error') throw new Error(value.error);
          return value?.phase === 'ticket_ready' ? value : null;
        }, 100000);
        if (!validTicket(auth.ticket)) throw new Error('学校登录票据无效');
        const ticket = auth.ticket;
        const {ticket:unused, ...rest} = auth;
        GM_setValue(jobKey(id), {...rest, phase:'authenticating'});
        location.replace(ticket);
        return;
      }
      const current = new URL(location.href), destination = new URL(target.url);
      if (current.pathname !== destination.pathname
          || current.searchParams.get('NewID') !== destination.searchParams.get('NewID')) {
        navigateToTarget(id, target.url);
        return;
      }
      updateJob(id,{phase:'seeking', message:'正在定位到所选时间…'});
      resume = monitorHistoryResume();
      await selectMediaSegment(id, target);
      const observed = await seekMedia(id, target.seconds, resume);
      delete tab[TAB_KEY];
      await saveTab(tab);
      updateJob(id,{phase:'done', observed});
    } catch (error) {
      const current = GM_getValue(jobKey(id));
      if (current && current.phase !== 'error') updateJob(id,{phase:'error', error:error.message});
      delete tab[TAB_KEY];
      await saveTab(tab);
    } finally {
      resume?.close();
    }
  }

  if (location.protocol === 'http:' && ['127.0.0.1','localhost'].includes(location.hostname)
      && location.pathname === '/course/replay') attachLocal();
  else if (location.origin === PORTAL) attachPortal().catch(() => {});
})();
