// Search, playback, note versions and source navigation are independent of capture.
export function createLibraryTools({getSession, openRecord, saveNote, renderNote, finishEditing, toast, escapeHtml, formatDuration}) {
  const $ = id => document.getElementById(id);
  let queryTimer, queryController, searchOffset = 0, searchTotal = 0, selectedVersion, comparedSummary;
  const player = $('noteAudio');
  const playToggle = $('notePlayToggle');
  const progress = $('noteProgress');
  const currentTime = $('noteCurrentTime');
  const durationLabel = $('noteDuration');
  let playerRecord = '', pendingSeek = null;

  function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function searchTerms(query) {
    return [...new Set(String(query || '').trim().split(/\s+/).filter(Boolean))];
  }

  function clearHighlights(root) {
    root?.querySelectorAll('mark.search-highlight').forEach(mark => {
      mark.replaceWith(document.createTextNode(mark.textContent || ''));
    });
    root?.normalize();
  }

  function highlight(root, query) {
    if (!root) return null;
    clearHighlights(root);
    const terms = searchTerms(query);
    if (!terms.length) return null;
    const pattern = new RegExp(terms.map(escapeRegExp).join('|'), 'giu');
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        return parent?.closest('script,style,textarea,button,mark')
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    let first = null;
    for (const node of nodes) {
      const text = node.nodeValue || '';
      pattern.lastIndex = 0;
      if (!pattern.test(text)) continue;
      pattern.lastIndex = 0;
      const fragment = document.createDocumentFragment();
      let cursor = 0;
      for (const match of text.matchAll(pattern)) {
        if (match.index > cursor) fragment.append(document.createTextNode(text.slice(cursor, match.index)));
        const mark = document.createElement('mark');
        mark.className = 'search-highlight';
        mark.textContent = match[0];
        fragment.append(mark);
        first ||= mark;
        cursor = match.index + match[0].length;
      }
      if (cursor < text.length) fragment.append(document.createTextNode(text.slice(cursor)));
      node.replaceWith(fragment);
    }
    return first;
  }

  function resetPlayer() {
    progress.value = 0;
    progress.max = 1;
    progress.style.setProperty('--progress', '0%');
    currentTime.textContent = formatPlayerDuration(0);
    durationLabel.textContent = formatPlayerDuration(0);
    playToggle.textContent = '播放';
    playToggle.setAttribute('aria-label', '播放录音');
  }

  function updatePlayerProgress() {
    const time = Number(player.currentTime) || 0;
    const duration = Number(player.duration);
    if (Number.isFinite(duration) && duration > 0) {
      progress.max = duration;
      durationLabel.textContent = formatPlayerDuration(duration);
      progress.value = Math.min(time, duration);
      progress.style.setProperty('--progress', `${Math.min(100, Math.max(0, time / duration * 100))}%`);
    } else {
      progress.value = 0;
      progress.style.setProperty('--progress', '0%');
    }
    currentTime.textContent = formatPlayerDuration(time);
  }

  function setPlayerState(playing) {
    playToggle.textContent = playing ? '暂停' : '播放';
    playToggle.setAttribute('aria-label', playing ? '暂停录音' : '播放录音');
  }

  function formatPlayerDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}` : `${minutes}:${String(secs).padStart(2, '0')}`;
  }

  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {cache:'no-store'} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '操作失败');
    return data;
  }
  function attach(session) {
    if (playerRecord !== session.id) {
      player.pause(); player.removeAttribute('src'); player.load();
      playerRecord = session.id; pendingSeek = null; resetPlayer();
    }
    playToggle.disabled = progress.disabled = session.status !== 'completed';
    $('notePlaybackHint').textContent = session.status === 'completed' ? '点击左侧时间戳回听；笔记中的“原文”链接定位转写。' : '结束并保存录音后可以回听。';
  }
  async function seek(seconds, play = true) {
    const session = getSession(); if (!session) return;
    const time = Math.max(0, Number(seconds) || 0);
    document.querySelectorAll('.note-transcript-segment').forEach(node => {
      const selected = Number(node.dataset.start) <= time && Number(node.dataset.end) > time;
      node.classList.toggle('source-highlight', selected);
      if (selected) node.scrollIntoView({behavior:'smooth', block:'center'});
    });
    $('noteColumns').classList.remove('focus-reading');
    if (session.status !== 'completed') { if (play) toast('结束并保存后可以回听'); return; }
    pendingSeek = {time, play};
    if (!player.getAttribute('src')) {
      player.src = `/api/recordings/${encodeURIComponent(session.id)}/playback`;
      player.load();
    }
    if (player.readyState >= 1) await applySeek();
  }
  async function applySeek() {
    if (!pendingSeek) return;
    const {time, play} = pendingSeek; pendingSeek = null;
    player.currentTime = Number.isFinite(player.duration) ? Math.min(time, Math.max(0, player.duration - .01)) : time;
    updatePlayerProgress();
    if (play) { try { await player.play(); } catch { toast('音频已定位，请点击播放'); } }
  }
  player.addEventListener('loadedmetadata', applySeek);
  player.addEventListener('loadedmetadata', updatePlayerProgress);
  player.addEventListener('timeupdate', updatePlayerProgress);
  player.addEventListener('play', () => setPlayerState(true));
  player.addEventListener('pause', () => setPlayerState(false));
  player.addEventListener('ended', () => { setPlayerState(false); updatePlayerProgress(); });
  player.addEventListener('error', () => toast('音频暂时无法播放，请确认录音已归档并重试'));
  $('noteDialog').addEventListener('close', () => player.pause());
  playToggle.addEventListener('click', async () => {
    const session = getSession();
    if (!session || session.status !== 'completed') { toast('结束并保存后可以回听'); return; }
    if (!player.getAttribute('src')) { player.src = `/api/recordings/${encodeURIComponent(session.id)}/playback`; player.load(); }
    try { if (player.paused) await player.play(); else player.pause(); }
    catch { toast('音频已准备，请再次点击播放'); }
  });
  progress.addEventListener('input', () => {
    const time = Number(progress.value) || 0;
    const session = getSession();
    if (session?.status === 'completed' && !player.getAttribute('src')) {
      player.src = `/api/recordings/${encodeURIComponent(session.id)}/playback`;
      player.load();
    }
    pendingSeek = {time, play: !player.paused};
    if (player.readyState >= 1) applySeek();
    updatePlayerProgress();
  });
  $('noteTranscript').addEventListener('click', event => {
    const button = event.target.closest('[data-seek]');
    if (button) seek(button.dataset.seek);
  });
  $('noteSummaryReader').addEventListener('click', event => {
    const link = event.target.closest('a[href^="#source-"]'); if (!link) return;
    event.preventDefault(); event.stopPropagation();
    const source = getSession()?.ai_note_summary?.source_map?.[link.getAttribute('href').slice(8)];
    if (!source) { toast('这条引用没有有效的来源记录，请核对原文'); return; }
    seek(source.start, false);
    toast(`已定位 ${formatDuration(source.start)} 的原文，可点击时间戳回听`, 4000);
  }, true);
  $('locateNoteSource').addEventListener('click', () => {
    const text = window.getSelection()?.toString().trim() || '';
    if (!text) { toast('先选中笔记中的关键词、公式或一句话，再点击查找原文'); return; }
    const terms = text.replace(/[$\\{}_*#]/g, ' ').split(/\s+/).filter(term => term.length > 1);
    const segments = getSession()?.segments || [];
    const ranked = segments.map(segment => ({segment, score:terms.reduce((sum, term) => sum + (String(segment.corrected || segment.raw || '').includes(term) ? term.length : 0), 0)})).sort((a,b) => b.score-a.score);
    if (!ranked[0]?.score) { toast('没有找到相同关键词，请缩短选择范围；未自动推断来源'); return; }
    seek(ranked[0].segment.start, false); toast('已定位关键词匹配的候选原文，请核对是否支持笔记内容', 5000);
  });
  async function search(append = false) {
    queryController?.abort(); queryController = new AbortController();
    const query = $('librarySearch').value.trim();
    if (!append) searchOffset = 0;
    $('librarySearchResults').hidden = !query;
    $('libraryList').hidden = Boolean(query);
    $('libraryPagination').hidden = Boolean(query);
    if (!query) return;
    $('librarySearchStatus').textContent = '正在搜索转写与笔记…';
    try {
      const params = new URLSearchParams({q:query, category:$('libraryCategory').value, offset:searchOffset, limit:30});
      const response = await fetch('/api/library/search?' + params, {signal:queryController.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '搜索失败');
      searchTotal = data.total;
      if (!append) $('libraryMatches').replaceChildren();
      for (const match of data.matches) {
        const button = document.createElement('button'); button.className='search-match'; button.type='button';
        const label = {title:'标题',summary:'笔记',transcript:'转写'}[match.kind];
        button.innerHTML = `<b>${escapeHtml(match.title)}</b><small>${escapeHtml(match.category)} · ${label}${match.start !== null ? ' · ' + formatDuration(match.start) : ''}</small><span>${escapeHtml(match.snippet)}</span>`;
        button.addEventListener('click', async () => {
          await openRecord(match.recording_id, {query, kind: match.kind, start: match.start});
        });
        $('libraryMatches').append(button);
      }
      searchOffset += data.matches.length;
      $('librarySearchStatus').textContent = `找到 ${searchTotal} 处匹配 · 已显示 ${searchOffset} 处`;
      $('moreLibraryMatches').hidden = searchOffset >= searchTotal;
    } catch (error) { if (error.name !== 'AbortError') $('librarySearchStatus').textContent = error.message; }
  }
  $('librarySearch').addEventListener('input', () => { clearTimeout(queryTimer); queryTimer=setTimeout(() => search(), 250); });
  $('libraryCategory').addEventListener('change', () => search());
  $('moreLibraryMatches').addEventListener('click', () => search(true));
  $('openNoteVersions').addEventListener('click', async () => {
    finishEditing(); if (!await saveNote()) return;
    try {
      const data = await api(`/api/recordings/${getSession().id}/versions`);
      $('noteVersionList').replaceChildren(); $('noteVersionDiff').textContent='选择一个历史版本，查看与当前笔记的差异。';
      $('restoreNoteVersion').disabled=true; selectedVersion=null;
      const sources = {edited:'手动编辑',post_recording:'AI 生成',ideashell_pull:'IdeaShell 拉取',restored:'历史恢复'};
      for (const version of data.versions) {
        const button=document.createElement('button'); button.className='ui-button';
        button.textContent=`${version.updated_at ? new Date(version.updated_at).toLocaleString() : '历史版本'} · ${sources[version.source] || version.source || '笔记'} · ${version.characters} 字符`;
        button.addEventListener('click', async () => {
          try {
            const result=await api(`/api/recordings/${getSession().id}/versions/${version.id}`);
            selectedVersion=version.id; comparedSummary=result.current_summary;
            $('noteVersionDiff').textContent=result.diff || '与当前版本内容相同';
            $('restoreNoteVersion').disabled=!result.diff;
          } catch(error) { toast(error.message); }
        }); $('noteVersionList').append(button);
      }
      if (!data.versions.length) $('noteVersionList').textContent='尚无历史版本。笔记被编辑、重新生成或拉取覆盖前会自动保留原版。';
      $('noteVersionsDialog').showModal();
    } catch(error) { toast(error.message); }
  });
  $('closeNoteVersions').addEventListener('click', () => $('noteVersionsDialog').close());
  $('restoreNoteVersion').addEventListener('click', async () => {
    if (!selectedVersion) return;
    $('restoreNoteVersion').disabled=true;
    try {
      const data=await api(`/api/recordings/${getSession().id}/versions/${selectedVersion}/restore`, {expected_summary:comparedSummary});
      renderNote(data.session); $('noteVersionsDialog').close(); toast('已恢复历史版本，恢复前的笔记也已保留');
    } catch(error) { toast(error.message); $('restoreNoteVersion').disabled=false; }
  });
  function focusSearch({query, kind, start = null} = {}) {
    if (!query) return;
    if (kind === 'transcript' && start !== null) {
      // Keep navigation tied to the selected timestamp, even when keywords repeat.
      highlight($('noteTranscript'), query);
      seek(start, false);
      return;
    }
    if (kind === 'summary') {
      const match = highlight($('noteSummaryReader'), query);
      match?.scrollIntoView({behavior: 'smooth', block: 'center'});
      return;
    }
    const match = highlight($('noteDialogTitle'), query);
    match?.scrollIntoView({behavior: 'smooth', block: 'center'});
  }

  return {attach, seek, search, focusSearch};
}
