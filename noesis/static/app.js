(() => {
  'use strict';

  const CAMERA_FALLBACKS = [
    { id: 'closeup1', name: 'Close-up 1', participant: 'A' },
    { id: 'closeup2', name: 'Close-up 2', participant: 'C' },
    { id: 'closeup3', name: 'Close-up 3', participant: 'D' },
    { id: 'closeup4', name: 'Close-up 4', participant: 'B' },
    { id: 'corner', name: 'Corner', participant: 'Room view' },
  ];
  const $ = (id) => document.getElementById(id);
  const refs = {
    connection: $('connection-indicator'), connectionLabel: $('connection-label'),
    modeCard: $('mode-card'), modeTitle: $('mode-title'), modeDetail: $('mode-detail'),
    inputMode: $('input-mode'), outputMode: $('output-mode'), start: $('start-session'),
    pause: $('pause-session'), stop: $('stop-session'), resume: $('resume-autopilot'),
    programImage: $('program-image'), previewPlaceholder: $('preview-placeholder'), stage: $('program-stage'),
    outputChip: $('output-chip'), previewWatermark: $('preview-watermark'), liveChip: $('live-chip'),
    stageLive: $('stage-live'), sessionTime: $('session-time'), sceneKicker: $('scene-kicker'),
    programCamera: $('program-camera'), programReason: $('program-reason'),
    currentShot: $('current-shot-name'), currentScene: $('current-shot-scene'),
    cuts: $('cuts-count'), fallbacks: $('fallbacks-count'), sessionStatus: $('session-status'),
    cameraGrid: $('camera-grid'), sourceCount: $('source-count'), faultCamera: $('fault-camera'),
    crewState: $('crew-state'), crewSummary: $('flower-summary'), agentList: $('agent-list'),
    traceList: $('trace-list'), traceCount: $('trace-count'), obsStatus: $('obs-status'),
    obsIndicator: $('obs-indicator'), opsFootnote: $('ops-footnote'), audio: $('preview-audio'),
    audioToggle: $('preview-audio-toggle'), toastRegion: $('toast-region'), creditsDialog: $('credits-dialog'),
  };
  let latestState = null;
  let latestAgentSnapshot = null;
  let eventSource = null;
  let lastStateAt = 0;
  let polling = false;
  let toastTimer = 0;
  let audioEnabled = false;
  let audioSessionId = null;
  let audioEpoch = null;
  let audioMissing = false;
  let inputChoiceTouched = false;
  let outputChoiceTouched = false;
  const imageRequests = new Map();
  const imageUrls = new Map();

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  }

  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function fmtTime(seconds) {
    if (!finite(seconds) || seconds < 0) return '00:00';
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  function fmtAge(ms) {
    if (!finite(ms) || ms < 0) return 'AGE —';
    if (ms < 1000) return `AGE ${Math.round(ms)}ms`;
    return `AGE ${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
  }
  function toast(message, tone = 'info') {
    refs.toastRegion.replaceChildren();
    const item = document.createElement('div');
    item.className = 'toast'; item.dataset.tone = tone; item.textContent = message;
    refs.toastRegion.append(item);
    clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => item.remove(), 5200);
  }
  function setConnection(kind, label) {
    refs.connection.dataset.state = kind;
    refs.connectionLabel.textContent = label;
  }
  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      if (label) button.setAttribute('aria-label', label);
    } else {
      button.removeAttribute('aria-busy');
      button.removeAttribute('aria-label');
      renderControls();
    }
  }
  function readError(payload, fallback) {
    if (payload && typeof payload.detail === 'string') return payload.detail;
    if (payload && payload.detail && typeof payload.detail.message === 'string') return payload.detail.message;
    return fallback;
  }
  async function getJson(path, options = {}) {
    const response = await fetch(path, { signal: AbortSignal.timeout(options.method === 'POST' ? 20000 : 4000), cache: 'no-store', ...options, headers: { Accept: 'application/json', ...(options.headers || {}) } });
    const raw = await response.text();
    let payload = null;
    if (raw) { try { payload = JSON.parse(raw); } catch { payload = null; } }
    if (!response.ok) throw new Error(readError(payload, `${response.status} ${response.statusText}`));
    return payload;
  }
  async function post(path, body = {}) { return getJson(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }

  async function runAction(path, body, button, successMessage) {
    setBusy(button, true, 'Working…');
    try {
      const result = await post(path, body);
      if (result && result.session) applyState(result);
      else if (result && result.state && result.state.session) applyState(result.state);
      else await fetchState();
      if (successMessage) toast(successMessage, 'success');
      return result;
    } catch (error) {
      toast(error.message || 'The controller could not complete that action.', 'error');
      throw error;
    } finally {
      setBusy(button, false);
    }
  }
  async function guardedAction(path, body, button, message) {
    try { await runAction(path, body, button, message); } catch { /* The error is shown in the live status region. */ }
  }

  function actualCameras() {
    if (latestState && Array.isArray(latestState.cameras)) return latestState.cameras;
    return [];
  }
  function cameraMeta(camera) {
    const known = CAMERA_FALLBACKS.find((item) => item.id === camera.id);
    return { name: camera.name || (known && known.name) || camera.id || 'Camera', participant: camera.participant || (known && known.participant) || 'Participant' };
  }
  function renderControls() {
    const session = latestState && latestState.session;
    const status = session && session.status;
    const active = status === 'running' || status === 'paused';
    const idle = !status || status === 'idle' || status === 'stopped';
    const manuallyLatched = latestState && latestState.mode === 'manual';
    const stopPending = latestState && latestState.obs && latestState.obs.stop_pending;
    if (!refs.start.hasAttribute('aria-busy')) refs.start.disabled = !idle || stopPending;
    if (!refs.pause.hasAttribute('aria-busy')) refs.pause.disabled = !active;
    if (!refs.stop.hasAttribute('aria-busy')) refs.stop.disabled = !active && !stopPending;
    if (!refs.resume.hasAttribute('aria-busy')) refs.resume.disabled = !manuallyLatched;
    refs.pause.innerHTML = status === 'paused' ? '<span class="button-icon" aria-hidden="true">▶</span> Resume' : '<span class="button-icon" aria-hidden="true">Ⅱ</span> Pause';
    refs.audioToggle.disabled = !(latestState && latestState.data && latestState.data.ami_available && session && session.input_mode === 'ami' && session.status === 'running') || audioMissing;
    refs.audioToggle.title = refs.audioToggle.disabled ? 'Audio preview is available when an AMI audio mix is ready.' : (audioEnabled ? 'Disable audio in this preview' : 'Enable audio in this preview');
  }
  function renderMode(state) {
    const mode = state.mode || 'unknown';
    refs.modeCard.dataset.mode = ['autopilot', 'manual', 'degraded'].includes(mode) ? mode : 'unknown';
    const labels = { autopilot: 'Autopilot', manual: 'Manual control', degraded: 'Degraded operation' };
    refs.modeTitle.textContent = labels[mode] || 'Mode not reported';
    const flower = state.flower || {};
    const obs = state.obs || {};
    let detail = 'Controller is waiting for session state';
    if (mode === 'autopilot') detail = 'Automatic shot selection is active';
    else if (mode === 'manual') detail = 'Camera selection is latched until Resume Autopilot';
    else if (mode === 'degraded') detail = flower.error || obs.error || 'Controller reports a dependency or agent issue';
    refs.modeDetail.textContent = detail;
  }
  function renderSession(state) {
    const session = state.session || {};
    const program = state.program || {};
    const status = session.status || 'unknown';
    const active = status === 'running';
    const paused = status === 'paused';
    const outputMode = session.output_mode || refs.outputMode.value || 'preview';
    refs.liveChip.dataset.active = String(active);
    refs.liveChip.textContent = active ? '● LIVE' : paused ? 'Ⅱ PAUSED' : String(status).toUpperCase();
    refs.stageLive.dataset.active = String(active);
    refs.stageLive.innerHTML = `<i></i> ${active ? 'ON AIR' : paused ? 'PAUSED' : status === 'idle' || status === 'stopped' ? 'STANDBY' : escapeHtml(status).toUpperCase()}`;
    refs.sessionTime.innerHTML = `${fmtTime(session.time_s)} <span>/ ${fmtTime(session.duration_s)}</span>`;
    refs.sessionStatus.textContent = status[0] ? status[0].toUpperCase() + status.slice(1) : 'Unknown';
    refs.outputChip.textContent = outputMode === 'obs' ? 'OBS DESTINATION' : 'PREVIEW OUTPUT';
    refs.previewWatermark.textContent = outputMode === 'obs' ? 'LOCAL PROGRAM MONITOR' : 'LOCAL PREVIEW';
    refs.sceneKicker.textContent = outputMode === 'obs' ? 'OBS PROGRAM SIGNAL' : 'PREVIEW PROGRAM';
    const camera = actualCameras().find((item) => item.id === program.camera_id);
    const meta = camera ? cameraMeta(camera) : null;
    const cameraName = meta ? meta.name : (program.camera_id === 'slate' ? 'Unavailable slate' : 'Awaiting program source');
    refs.programCamera.textContent = cameraName;
    refs.programReason.textContent = program.reason || (program.scene ? `Scene · ${program.scene}` : 'The controller’s selected camera will appear here.');
    refs.currentShot.textContent = cameraName;
    refs.currentScene.textContent = program.scene || '—';
    refs.cuts.textContent = state.metrics && finite(state.metrics.cuts) ? String(state.metrics.cuts) : '—';
    refs.fallbacks.textContent = state.metrics && finite(state.metrics.fallbacks) ? String(state.metrics.fallbacks) : '—';
    refs.stage.classList.toggle('has-image', refs.programImage.classList.contains('is-visible'));
    if (session.input_mode && !inputChoiceTouched) refs.inputMode.value = session.input_mode;
    if (session.output_mode && !outputChoiceTouched) refs.outputMode.value = session.output_mode;
  }
  function renderCameras(state) {
    const cameras = Array.isArray(state.cameras) ? state.cameras : [];
    const priorImages = new Map([...refs.cameraGrid.querySelectorAll('img[data-frame-key]')].map((image) => [image.dataset.frameKey, image]));
    const selectedFault = refs.faultCamera.value;
    const focusedCamera = document.activeElement && document.activeElement.dataset.cameraId;
    const programId = state.program && state.program.camera_id;
    refs.sourceCount.textContent = String(cameras.length);
    if (!cameras.length) {
      refs.cameraGrid.innerHTML = '<div class="source-loading"><span class="loading-pulse"></span> No camera state reported by the controller.</div>';
      refs.faultCamera.innerHTML = CAMERA_FALLBACKS.map((cam) => `<option value="${cam.id}">${cam.name}</option>`).join('');
      return;
    }
    refs.cameraGrid.innerHTML = cameras.map((camera) => {
      const id = String(camera.id || '');
      const meta = cameraMeta(camera);
      const selected = id === programId;
      const healthy = camera.healthy === true;
      const unhealthy = camera.healthy === false;
      const speaking = camera.speaking === true;
      const energy = finite(camera.energy) ? Math.max(0, Math.min(1, camera.energy)) : (speaking ? 0.65 : 0);
      const bars = Array.from({ length: 5 }, (_, index) => `<i style="opacity:${speaking && energy >= (index + 1) / 6 ? 1 : .38}"></i>`).join('');
      const healthText = healthy ? 'HEALTHY' : unhealthy ? String(camera.status || 'UNHEALTHY').replaceAll('_', ' ').toUpperCase() : 'UNKNOWN';
      const participant = escapeHtml(meta.participant);
      const label = escapeHtml(meta.name);
      const age = fmtAge(camera.age_ms);
      const sourceUrl = `/api/frame/${encodeURIComponent(id)}.jpg`;
      return `<article class="camera-card${selected ? ' is-program' : ''}${unhealthy ? ' is-unhealthy' : ''}" data-camera-card="${escapeHtml(id)}">
        <button type="button" class="camera-select" data-camera-id="${escapeHtml(id)}" aria-label="Take ${label} as a manual camera override">
          <div class="camera-thumb"><div class="camera-empty" aria-hidden="true">▧</div><img data-frame-key="${escapeHtml(id)}" data-frame-url="${sourceUrl}" alt="${label} camera feed" decoding="async"><span class="camera-state" data-healthy="${healthy ? 'true' : unhealthy ? 'false' : 'unknown'}" aria-label="${healthText.toLowerCase()}"></span>${selected ? '<span class="on-air-badge">ON AIR</span>' : ''}<span class="speaker-badge">${speaking ? '● SPEAKING' : 'LISTENING'}</span></div>
          <div class="camera-info"><div class="camera-title-row"><span class="camera-title">${label}</span><span class="camera-qual">${age}</span></div><div class="camera-person">${participant}</div><div class="camera-metrics"><span class="health-label">${healthText}</span><span class="audio-meter" data-speaking="${speaking ? 'true' : 'false'}" role="img" aria-label="${speaking ? 'Speaking detected' : 'No speaking detected'}">${bars}</span></div></div>
        </button>
      </article>`;
    }).join('');
    // State events update badges often. Preserve decoded image nodes between
    // events so all five thumbnails stay visible instead of resetting to blank.
    for (const image of refs.cameraGrid.querySelectorAll('img[data-frame-key]')) {
      const previous = priorImages.get(image.dataset.frameKey);
      if (previous) {
        previous.dataset.frameUrl = image.dataset.frameUrl;
        image.replaceWith(previous);
        const placeholder = previous.parentElement.querySelector('.camera-empty');
        if (placeholder) placeholder.hidden = previous.classList.contains('is-visible');
      }
    }
    if (focusedCamera) {
      refs.cameraGrid.querySelector(`[data-camera-id="${CSS.escape(focusedCamera)}"]`)?.focus({ preventScroll: true });
    }
    const keep = new Set(cameras.map((camera) => camera.id));
    refs.faultCamera.innerHTML = cameras.map((camera) => {
      const meta = cameraMeta(camera);
      return `<option value="${escapeHtml(camera.id)}">${escapeHtml(meta.name)}</option>`;
    }).join('');
    if (keep.has(selectedFault)) refs.faultCamera.value = selectedFault;
    if (latestState && latestState.faults && latestState.faults.length) { /* Fault status is shown through reported source health and trace. */ }
    for (const [key, url] of imageUrls) if (!keep.has(key) && key !== 'program') { URL.revokeObjectURL(url); imageUrls.delete(key); }
  }
  function renderFlower(state) {
    const flower = state.flower || {};
    const agents = Array.isArray(flower.agents) ? flower.agents : (latestAgentSnapshot && Array.isArray(latestAgentSnapshot.agents) ? latestAgentSnapshot.agents : []);
    const flowerStatus = String(flower.status || (latestAgentSnapshot && latestAgentSnapshot.status) || 'not reported');
    const decisionMode = flower.decision_modes && flower.decision_modes.director === 'llm' ? 'model-assisted decisions' : 'rules mode · no model calls';
    refs.crewSummary.textContent = flowerStatus === 'not reported' ? 'No Flower status reported' : `${flowerStatus} · ${decisionMode}${flower.model ? ` · ${flower.model}` : ''}`;
    const hasHealthy = agents.some((agent) => agent.healthy === true);
    const isUnavailable = /unavailable|offline|stopped|error|degraded/i.test(flowerStatus);
    refs.crewState.dataset.state = isUnavailable ? 'degraded' : hasHealthy ? 'healthy' : '';
    refs.crewState.textContent = hasHealthy ? `${agents.filter((agent) => agent.healthy === true).length} LIVE` : isUnavailable ? 'DEGRADED' : agents.length ? 'NO HEARTBEAT' : 'NO AGENTS';
    refs.agentList.innerHTML = agents.length ? agents.map((agent) => {
      const runtime = agent.runtime || agent.role || 'Agent runtime';
      const identity = agent.agent_id || agent.role || 'Agent';
      const details = [agent.role, agent.camera_id ? `camera ${agent.camera_id}` : null, agent.run_id ? `run ${agent.run_id}` : null].filter(Boolean).join(' · ');
      return `<div class="agent-row"><span class="agent-dot" data-healthy="${agent.healthy === true ? 'true' : 'false'}" aria-hidden="true"></span><span class="agent-copy"><strong>${escapeHtml(identity)}</strong><span title="${escapeHtml(details || runtime)}">${escapeHtml(details || runtime)}</span></span><span class="agent-age">${agent.healthy === true ? fmtAge(agent.age_ms).replace('AGE ', '') : 'STALE'}</span></div>`;
    }).join('') : `<div class="empty-inline">${escapeHtml(flower.error || 'No agent heartbeat received yet.')}</div>`;
  }
  function renderTrace(state) {
    const events = Array.isArray(state.events) ? state.events.filter((event) => event.kind !== 'agent_heartbeat' && event.kind !== 'camera_observation').slice(0, 8) : [];
    refs.traceCount.textContent = events.length ? `${events.length} EVENTS` : '—';
    refs.traceList.innerHTML = events.length ? events.map((event) => {
      const kind = String(event.kind || 'event');
      const source = event.source || 'controller';
      const time = fmtTime(event.time_s);
      const camera = event.camera_id ? ` · ${event.camera_id}` : '';
      return `<li data-kind="${escapeHtml(kind.toLowerCase())}"><div class="trace-meta"><span class="trace-source">${escapeHtml(source)} · ${escapeHtml(kind)}${escapeHtml(camera)}</span><time>${time}</time></div><div class="trace-message">${escapeHtml(event.message || 'Event reported without a description.')}</div></li>`;
    }).join('') : '<li class="empty-trace">No controller or agent events reported yet.</li>';
  }
  function renderObs(state) {
    const obs = state.obs || {};
    if (obs.stop_pending) {
      refs.obsIndicator.dataset.state = 'offline';
      refs.obsStatus.textContent = 'Recording stop unconfirmed · reconnect OBS, then press Stop';
    } else if (obs.connected === true) {
      refs.obsIndicator.dataset.state = 'healthy';
      refs.obsStatus.textContent = obs.status || (obs.recording ? 'Connected · recording' : 'Connected');
    } else if (obs.connected === false) {
      refs.obsIndicator.dataset.state = 'offline';
      refs.obsStatus.textContent = obs.error || obs.status || 'Disconnected';
    } else {
      refs.obsIndicator.dataset.state = 'unknown';
      refs.obsStatus.textContent = obs.status || 'Status not reported';
    }
    refs.opsFootnote.textContent = obs.output_path ? `Recording path · ${obs.output_path}` : 'Test faults affect the source used by the program and health monitor.';
  }
  function applyState(state) {
    if (!state || typeof state !== 'object') return;
    latestState = state;
    lastStateAt = Date.now();
    setConnection('connected', 'Controller online');
    renderMode(state); renderSession(state); renderCameras(state); renderFlower(state); renderTrace(state); renderObs(state); renderControls();
    syncPreviewAudio(state);
      if (state.data && Array.isArray(state.data.missing_files) && state.data.missing_files.length && !(state.obs && state.obs.output_path)) {
        refs.opsFootnote.textContent = 'AMI dataset is not installed yet. Generated test feeds are ready; see README for the verified download workflow.';
    }
  }
  async function fetchState() {
    if (polling) return;
    polling = true;
    try {
      const state = await getJson('/api/state');
      applyState(state);
    } catch (error) {
      setConnection('error', 'Controller unavailable');
      if (!latestState) {
        refs.modeTitle.textContent = 'Controller unavailable';
        refs.modeDetail.textContent = error.message || 'The local API has not responded.';
        refs.cameraGrid.innerHTML = '<div class="source-loading">Waiting for the local production controller…</div>';
      }
    } finally { polling = false; }
  }
  async function fetchAgentSnapshot() {
    try {
      const snapshot = await getJson('/api/agents/snapshot');
      latestAgentSnapshot = snapshot;
      if (latestState) renderFlower(latestState);
    } catch { /* The main state and empty crew state remain authoritative. */ }
  }

  function startEvents() {
    if (!('EventSource' in window)) return;
    try {
      eventSource = new EventSource('/api/events');
      eventSource.addEventListener('state', (event) => {
        try { applyState(JSON.parse(event.data)); } catch { /* Ignore malformed event data and let polling recover. */ }
      });
      eventSource.onopen = () => { if (latestState) setConnection('connected', 'Live updates'); };
      eventSource.onerror = () => {
        if (Date.now() - lastStateAt > 3500) setConnection('connecting', 'Reconnecting');
      };
    } catch { eventSource = null; }
  }

  async function loadFrame(key, url) {
    if (imageRequests.get(key)) return;
    imageRequests.set(key, true);
    try {
      const separator = url.includes('?') ? '&' : '?';
      const response = await fetch(`${url}${separator}_=${Date.now()}`, { signal: AbortSignal.timeout(2000), cache: 'no-store', headers: { Accept: 'image/jpeg' } });
      if (!response.ok) return;
      const blob = await response.blob();
      if (!blob.type.includes('image') && blob.size === 0) return;
      const objectUrl = URL.createObjectURL(blob);
      const oldUrl = imageUrls.get(key);
      imageUrls.set(key, objectUrl);
      const image = key === 'program' ? refs.programImage : refs.cameraGrid.querySelector(`img[data-frame-key="${CSS.escape(key)}"]`);
      if (image) {
        image.onload = () => {
          image.classList.add('is-visible');
          if (key === 'program') refs.stage.classList.add('has-image');
          const empty = image.parentElement && image.parentElement.querySelector('.camera-empty');
          if (empty) empty.hidden = true;
        };
        image.src = objectUrl;
      } else {
        URL.revokeObjectURL(objectUrl);
      }
      if (oldUrl) URL.revokeObjectURL(oldUrl);
    } catch { /* Keep the last successful frame while the source recovers. */ }
    finally { imageRequests.delete(key); }
  }
  function refreshFrames() {
    const session = latestState && latestState.session;
    if (!session || !['running', 'paused'].includes(session.status)) return;
    void loadFrame('program', '/api/program.jpg');
    const cameraImages = [...refs.cameraGrid.querySelectorAll('img[data-frame-key]')];
    if (cameraImages.length) {
      const index = refreshFrames.cameraIndex % cameraImages.length;
      const image = cameraImages[index];
      refreshFrames.cameraIndex = (refreshFrames.cameraIndex + 1) % cameraImages.length;
      void loadFrame(image.dataset.frameKey, image.dataset.frameUrl);
    }
  }
  refreshFrames.cameraIndex = 0;

  function syncPreviewAudio(state) {
    const session = state.session || {};
    const available = Boolean(state.data && state.data.ami_available && session.input_mode === 'ami');
    if (!available) {
      if (!refs.audio.paused) refs.audio.pause();
      return;
    }
    if (audioMissing) return;
    const newSession = session.id !== audioSessionId;
    const newEpoch = session.epoch !== audioEpoch;
    if (!refs.audio.src || newSession) {
      refs.audio.src = '/api/audio';
      audioSessionId = session.id;
      audioEpoch = session.epoch;
    }
    if (newSession || newEpoch) {
      try { refs.audio.currentTime = Math.max(0, Number(session.time_s) || 0); } catch { /* Metadata may not have loaded yet. */ }
      audioEpoch = session.epoch;
    } else if (finite(session.time_s) && Number.isFinite(refs.audio.currentTime) && Math.abs(refs.audio.currentTime - session.time_s) > 1.25) {
      try { refs.audio.currentTime = Math.max(0, session.time_s); } catch { /* The media clock is not seekable yet. */ }
    }
    if (audioEnabled && session.status === 'running' && refs.audio.paused) {
      refs.audio.play().catch(() => { audioEnabled = false; refs.audioToggle.setAttribute('aria-pressed', 'false'); toast('Audio playback needs a fresh click in this browser.', 'error'); });
    } else if (session.status !== 'running' && !refs.audio.paused) {
      refs.audio.pause();
    }
  }

  $('start-session').addEventListener('click', () => guardedAction('/api/session/start', { input_mode: refs.inputMode.value, output_mode: refs.outputMode.value, start_s: 0 }, refs.start, 'Session start requested.'));
  refs.inputMode.addEventListener('change', () => { inputChoiceTouched = true; });
  refs.outputMode.addEventListener('change', () => { outputChoiceTouched = true; });
  $('pause-session').addEventListener('click', () => {
    const paused = latestState && latestState.session && latestState.session.status === 'paused';
    void guardedAction(paused ? '/api/session/resume' : '/api/session/pause', {}, refs.pause, paused ? 'Session resume requested.' : 'Session pause requested.');
  });
  $('stop-session').addEventListener('click', () => guardedAction('/api/session/stop', {}, refs.stop, 'Session stop requested.'));
  $('resume-autopilot').addEventListener('click', () => guardedAction('/api/control/autopilot', {}, refs.resume, 'Autopilot resume requested.'));
  refs.cameraGrid.addEventListener('click', (event) => {
    const button = event.target.closest('[data-camera-id]');
    if (!button) return;
    const id = button.dataset.cameraId;
    if (!id) return;
    const target = actualCameras().find((camera) => camera.id === id);
    const name = target ? cameraMeta(target).name : id;
    void guardedAction('/api/control/override', { camera_id: id }, button, `Manual override requested · ${name}.`);
  });
  $('inject-fault').addEventListener('click', (event) => {
    const cameraId = refs.faultCamera.value;
    const kind = $('fault-kind').value;
    void guardedAction('/api/fault', { camera_id: cameraId, kind, duration_s: 10 }, event.currentTarget, `Fault request sent · ${kind} on ${cameraId}.`);
  });
  $('obs-connect').addEventListener('click', (event) => guardedAction('/api/obs/connect', {}, event.currentTarget, 'OBS connection request sent.'));
  $('obs-setup').addEventListener('click', (event) => guardedAction('/api/obs/setup', {}, event.currentTarget, 'OBS scene setup request sent.'));
  refs.audio.addEventListener('error', () => {
    if (!refs.audio.src) return;
    audioMissing = true; audioEnabled = false; refs.audioToggle.setAttribute('aria-pressed', 'false');
    renderControls(); toast('AMI audio could not be loaded from the controller.', 'error');
  });
  refs.audioToggle.addEventListener('click', async () => {
    if (refs.audioToggle.disabled) return;
    if (audioEnabled) {
      audioEnabled = false; refs.audio.pause(); refs.audioToggle.setAttribute('aria-pressed', 'false');
      refs.audioToggle.innerHTML = ' <span aria-hidden="true">♫</span> Preview audio <i></i>';
      return;
    }
    const session = latestState && latestState.session;
    if (session && finite(session.time_s)) {
      try { refs.audio.currentTime = session.time_s; } catch { /* The source initializes at the current session time on metadata load. */ }
    }
    try {
      await refs.audio.play();
      audioEnabled = true; refs.audioToggle.setAttribute('aria-pressed', 'true');
      refs.audioToggle.innerHTML = ' <span aria-hidden="true">♫</span> Audio on <i></i>';
    } catch {
      toast('Audio is unavailable or blocked by this browser.', 'error');
      audioMissing = true; renderControls();
    }
  });

  function openCredits() {
    if (typeof refs.creditsDialog.showModal === 'function') refs.creditsDialog.showModal();
    else refs.creditsDialog.setAttribute('open', '');
  }
  $('credits-open').addEventListener('click', openCredits);
  $('credits-open-footer').addEventListener('click', openCredits);

  refs.audioToggle.innerHTML = ' <span aria-hidden="true">♫</span> Preview audio <i></i>';
  void fetchState();
  void fetchAgentSnapshot();
  startEvents();
  window.setInterval(() => { if (Date.now() - lastStateAt > 3500) void fetchState(); }, 2200);
  window.setInterval(() => { void fetchAgentSnapshot(); }, 5000);
  window.setInterval(refreshFrames, 200);
})();
