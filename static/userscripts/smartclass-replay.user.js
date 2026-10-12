// ==UserScript==
// @name         QwenASR 南邮回放跳转
// @namespace    qwenasr.local/smartclass-replay
// @version      1.2.0
// @description  自动登录南邮 Smart Class，并从录音库准确定位课堂回放。
// @updateURL    http://127.0.0.1:8765/course/replay-script.user.js
// @downloadURL  http://127.0.0.1:8765/course/replay-script.user.js
// @match        http://127.0.0.1/course/replay*
// @match        http://localhost/course/replay*
// @match        https://njupt.smartclass.cn/*
// @run-at       document-end
// @noframes
// @grant        unsafeWindow
// @grant        GM_getTab
// @grant        GM_saveTab
// ==/UserScript==

(() => {
  'use strict';
  const PORTAL = 'https://njupt.smartclass.cn';
  const CHANNEL = 'qwenasr-userscript-v1';
  const TAB_KEY = 'qwenasrReplayDirect';
  const VERSION = '1.2.0';
  const LOCAL = 'http://127.0.0.1:8765';
  const MAX_NAVIGATIONS = 2;
  const page = unsafeWindow;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  let currentJob = null;
  const tabInfo = () => new Promise(resolve => GM_getTab(resolve));
  const saveTab = data => new Promise(resolve => GM_saveTab(data, resolve));

  function validateTarget(input) {
    if (input?.kind === 'portal') return {kind:'portal', url:portalURL(input.url)};
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
  function portalURL(value) {
    const url = new URL(value);
    if (url.origin !== PORTAL || url.username || url.password
        || /(?:logout|signout|redirect|login)\.aspx$/i.test(url.pathname)
        || [...url.searchParams.keys()].some(key => /^(?:ticket|token|password|authKey)$/i.test(key))) {
      throw new Error('学校页面地址无效');
    }
    return url.href;
  }
  function validTicket(value) {
    try {
      const url = new URL(value);
      return url.origin === PORTAL && !url.username && !url.password
        && url.pathname === '/SystemSpace/Redirect.aspx' && Boolean(url.searchParams.get('ticket'));
    } catch (_) { return false; }
  }
  function localURL(value) {
    const url = new URL(value);
    if (url.protocol !== 'http:' || !['127.0.0.1','localhost'].includes(url.hostname)
        || url.username || url.password || url.pathname !== '/course/replay') {
      throw new Error('本机回放入口无效');
    }
    return url.href;
  }
  function updateJob(id, patch) {
    if (currentJob?.id === id) currentJob = {...currentJob, ...patch};
  }
  function usableJob(id) {
    return currentJob?.id === id && currentJob.expires > Date.now() ? currentJob : null;
  }
  async function saveJob() {
    const tab = await tabInfo();
    if (currentJob) tab[TAB_KEY] = currentJob;
    else delete tab[TAB_KEY];
    await saveTab(tab);
  }
  async function clearJob() {
    currentJob = null;
    await saveJob();
  }
  async function navigate(url) {
    await saveJob();
    location.replace(url);
  }

  async function attachLocal() {
    const send = payload => window.postMessage({channel:CHANNEL, ...payload}, location.origin);
    currentJob = (await tabInfo())[TAB_KEY] || null;
    const resuming = currentJob && usableJob(currentJob.id) && currentJob.phase === 'need_login'
      && currentJob.returnURL === location.href;
    if (!resuming) await clearJob();
    let starting = Boolean(resuming);
    const ready = () => send({type:'ready', version:VERSION, sameTab:true, resuming:Boolean(resuming)});
    window.addEventListener('message', async event => {
      if ((event.source !== window && event.source !== page) || event.origin !== location.origin
          || event.data?.channel !== CHANNEL) return;
      if (event.data.type === 'probe') { ready(); return; }
      // Never execute the old two-tab protocol if an older launch page is cached.
      if (event.data.type !== 'open' || !event.data.sameTab || starting) return;
      starting = true;
      try {
        const target = validateTarget(event.data.target);
        currentJob = {id:crypto.randomUUID(), target, returnURL:localURL(location.href),
          phase:'checking', expires:Date.now()+180000, authAttempts:0, navigationAttempts:0};
        send({type:'accepted'});
        await navigate(PORTAL + '/UserSpace/CourseCenter.aspx');
      } catch (error) {
        await clearJob(); starting = false;
        send({type:'result', ok:false, error:error.message});
      }
    });
    ready();
    if (!resuming) return;
    try {
      const response = await page.fetch('/course/api/browser-login', {
        method:'POST', headers:{'X-QwenASR-Client':'smartclass','Content-Type':'application/json'},
        body:'{}', cache:'no-store', signal:AbortSignal.timeout(90000),
      });
      const data = await response.json();
      if (!response.ok || !validTicket(data.login_url)) {
        const error = new Error(typeof data.detail === 'string' ? data.detail : '学校未返回有效登录票据');
        error.needs_credentials = response.status === 401;
        throw error;
      }
      if (!usableJob(currentJob.id)) throw new Error('回放请求已过期，请重新跳转');
      updateJob(currentJob.id, {phase:'authenticating'});
      // The ticket is consumed directly by this tab, never saved in extension storage.
      await navigate(data.login_url);
    } catch (error) {
      await clearJob(); starting = false;
      send({type:'result', ok:false, error:error.message, needs_credentials:Boolean(error.needs_credentials)});
    }
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
  async function navigateToTarget(id, url) {
    const job = usableJob(id);
    if (!job) throw new Error('回放请求已经结束');
    const attempts = Number(job.navigationAttempts) || 0;
    if (attempts >= MAX_NAVIGATIONS) throw new Error('学校未能打开目标回放，已停止重复跳转；请检查视频权限或是否已删除');
    updateJob(id,{phase:'loading', navigationAttempts:attempts + 1,
      message:'登录已确认，正在打开对应课堂回放…'});
    await navigate(url);
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
    currentJob = (await tabInfo())[TAB_KEY] || null;
    if (!currentJob) {
      // The normal portal also exposes userInfo for anonymous visitors.
      for (let attempt = 0; attempt < 30 && !Array.isArray(page.userInfo); attempt += 1) await wait(300);
      if (!Array.isArray(page.userInfo) || page.userInfo[0]?.UserID || portalError()) return;
      const destination = location.pathname.toLowerCase() === '/401.aspx'
        ? PORTAL + '/UserSpace/CourseList.aspx' : portalURL(location.href);
      currentJob = {id:crypto.randomUUID(), target:{kind:'portal', url:destination},
        returnURL:LOCAL + '/course/replay?portal=' + encodeURIComponent(destination),
        phase:'need_login', expires:Date.now()+180000, authAttempts:1, navigationAttempts:0};
      await navigate(currentJob.returnURL);
      return;
    }
    const id = currentJob.id;
    let resume;
    try {
      if (!usableJob(id)) throw new Error('回放请求已过期，请从原页面重新跳转');
      const target = validateTarget(currentJob.target);
      const returnURL = localURL(currentJob.returnURL);
      await until(id, () => document.readyState !== 'loading' && Array.isArray(page.userInfo));
      if (!page.userInfo[0]?.UserID) {
        if (currentJob.authAttempts >= 1) throw new Error('浏览器登录未成功，请在课程回放页重新配置南邮账号');
        updateJob(id,{phase:'need_login', authAttempts:1});
        // Briefly return to the invisible local endpoint for its same-origin login call.
        await navigate(returnURL);
        return;
      }
      if (target.kind === 'portal') {
        await clearJob();
        if (location.href !== target.url) location.replace(target.url);
        return;
      }
      const current = new URL(location.href), destination = new URL(target.url);
      if (current.pathname !== destination.pathname
          || current.searchParams.get('NewID') !== destination.searchParams.get('NewID')) {
        await navigateToTarget(id, target.url);
        return;
      }
      updateJob(id,{phase:'seeking'});
      resume = monitorHistoryResume();
      await selectMediaSegment(id, target);
      await seekMedia(id, target.seconds, resume);
      await clearJob();
    } catch (error) {
      await clearJob();
      page.alert('QwenASR：' + error.message);
    } finally {
      resume?.close();
    }
  }

  if (location.protocol === 'http:' && ['127.0.0.1','localhost'].includes(location.hostname)
      && location.pathname === '/course/replay') attachLocal().catch(() => {});
  else if (location.origin === PORTAL) attachPortal().catch(() => {});
})();
