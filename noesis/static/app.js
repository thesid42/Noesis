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
    inputMode: $('input-mode'), outputMode: $('output-mode'), modelProfile: $('model-profile'),
    broadcastDelay: $('broadcast-delay'), broadcastStatus: $('broadcast-status'), broadcastMetrics: $('broadcast-metrics'),
    perceptionStatus: $('perception-status'), transcriptPreview: $('transcript-preview'),
    modelProfileStatus: $('model-profile-status'), modelVerificationStatus: $('model-verification-status'),
    aiProvenance: $('ai-provenance'), start: $('start-session'),
    pause: $('pause-session'), stop: $('stop-session'), resume: $('resume-autopilot'),
    programImage: $('program-image'), previewPlaceholder: $('preview-placeholder'), stage: $('program-stage'),
    outputChip: $('output-chip'), previewWatermark: $('preview-watermark'), liveChip: $('live-chip'),
    stageLive: $('stage-live'), sessionTime: $('session-time'), sceneKicker: $('scene-kicker'),
    programCamera: $('program-camera'), programReason: $('program-reason'),
    currentShot: $('current-shot-name'), currentScene: $('current-shot-scene'),
    cuts: $('cuts-count'), fallbacks: $('fallbacks-count'), sessionStatus: $('session-status'),
    cameraGrid: $('camera-grid'), sourceCount: $('source-count'), faultCamera: $('fault-camera'),
    crewState: $('crew-state'), crewSummary: $('flower-summary'), agentList: $('agent-list'),
    gridRun: $('grid-run'), gridRunLabel: $('grid-run-label'), gridRunId: $('grid-run-id'), gridRunStatus: $('grid-run-status'),
    traceList: $('trace-list'), traceCount: $('trace-count'), obsStatus: $('obs-status'),
    obsIndicator: $('obs-indicator'), opsFootnote: $('ops-footnote'), audio: $('preview-audio'),
    audioToggle: $('preview-audio-toggle'), audioVolume: $('preview-audio-volume'), audioStatus: $('preview-audio-status'),
    toastRegion: $('toast-region'), creditsDialog: $('credits-dialog'),
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
  let audioStartPending = false;
  let profileBusy = false;
  let profileOptionSignature = '';
  let inputChoiceTouched = false;
  let outputChoiceTouched = false;
  let programStreamKey = null;
  const imageRequests = new Map();
  const imageUrls = new Map();

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  }

  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function playbackSession(state) {
    const session = state && state.session || {};
    const broadcast = state && state.broadcast;
    if (!broadcast || !finite(broadcast.time_s)) return session;
    return { ...session, time_s: broadcast.time_s,
      status: session.status === 'running' && !broadcast.ready ? 'buffering' : session.status };
  }
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
    refs.broadcastDelay.disabled = active;
    if (!refs.start.hasAttribute('aria-busy')) refs.start.disabled = !idle || stopPending;
    if (!refs.pause.hasAttribute('aria-busy')) refs.pause.disabled = !active;
    if (!refs.stop.hasAttribute('aria-busy')) refs.stop.disabled = !active && !stopPending;
    if (!refs.resume.hasAttribute('aria-busy')) refs.resume.disabled = !manuallyLatched || status !== 'running';
    refs.pause.innerHTML = status === 'paused' ? '<span class="button-icon" aria-hidden="true">▶</span> Resume' : '<span class="button-icon" aria-hidden="true">Ⅱ</span> Pause';
    renderAudioControls(latestState || {});
  }
  function renderMode(state) {
    const mode = state.mode || 'unknown';
    refs.modeCard.dataset.mode = ['autopilot', 'manual', 'degraded'].includes(mode) ? mode : 'unknown';
    const labels = { autopilot: 'Autopilot', manual: 'Manual control', degraded: 'Degraded operation' };
    refs.modeTitle.textContent = labels[mode] || 'Mode not reported';
    const flower = state.flower || {};
    const obs = state.obs || {};
    if (['idle', 'stopped'].includes(state.session && state.session.status) && !obs.stop_pending) {
      refs.modeTitle.textContent = 'Ready to start';
      refs.modeDetail.textContent = 'Start a session to enable automatic directing';
      return;
    }
    let detail = 'Controller is waiting for session state';
    if (mode === 'autopilot') detail = 'Automatic shot selection is active';
    else if (mode === 'manual') detail = 'Camera selection is latched until Resume Autopilot';
    else if (mode === 'degraded') detail = flower.error || obs.error || 'Controller reports a dependency or agent issue';
    refs.modeDetail.textContent = detail;
  }
  function renderSession(state) {
    const session = playbackSession(state);
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
    const b = state.broadcast || {};
    refs.broadcastStatus.textContent = `${Number(b.delay_s ?? refs.broadcastDelay.value)}s delay · ${b.phase || 'standby'}`;
    refs.broadcastMetrics.textContent = `Input ${fmtTime(state.session && state.session.time_s)} · On air ${fmtTime(b.time_s)} · ${Number(b.captured_fps || 0).toFixed(1)} captured fps · ${b.pending_cuts || 0} queued cuts · ${state.metrics && state.metrics.ai_deadline_misses || 0} missed deadlines`;
    const p = state.perception || {};
    const ps = p.status || {};
    refs.perceptionStatus.textContent = `Speech: ${ps.speech && ps.speech.state || 'unavailable'} · Visual: ${ps.visual && ps.visual.state || 'unavailable'}`;
    refs.transcriptPreview.textContent = p.transcript && p.transcript.text || 'No recent speech observations.';
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
    const gridRun = flower.grid_run && typeof flower.grid_run === 'object' ? flower.grid_run : {};
    const deployment = String(flower.deployment || gridRun.deployment || '').toLowerCase();
    const deploymentLabel = deployment === 'local' ? 'Local Flower' : deployment === 'supergrid' ? 'SuperGrid' : 'Flower';
    renderModelProfile(state);
    const agents = Array.isArray(flower.agents) ? flower.agents : (latestAgentSnapshot && Array.isArray(latestAgentSnapshot.agents) ? latestAgentSnapshot.agents : []);
    const flowerStatus = String(flower.status || (latestAgentSnapshot && latestAgentSnapshot.status) || 'not reported');
    const selectedModel = state.models && Array.isArray(state.models.options)
      ? state.models.options.find((item) => item.id === state.models.selected) : null;
    const profileStatus = flower.model_status === 'verified' ? 'verified' : flower.model_status === 'configured_not_verified' ? 'configured · not verified' : 'status not reported';
    const profileLabel = selectedModel && selectedModel.label ? selectedModel.label : 'AI profile';
    refs.crewSummary.textContent = flowerStatus === 'not reported'
      ? `${deploymentLabel} · ${profileLabel} · ${profileStatus} · no agent status reported`
      : `${deploymentLabel} · ${flowerStatus} · ${profileLabel} · ${profileStatus}`;
    const gridRunId = typeof gridRun.run_id === 'string' ? gridRun.run_id.trim() : '';
    const gridRunStatus = [gridRun.status, gridRun.sub_status]
      .filter((value) => typeof value === 'string' && value.trim())
      .join(' · ');
    refs.gridRun.hidden = !gridRunId && !gridRunStatus;
    refs.gridRunLabel.textContent = deployment === 'local' ? 'LOCAL FLOWER RUN' : deployment === 'supergrid' ? 'SUPERGRID RUN' : 'FLOWER RUN';
    refs.gridRunId.textContent = gridRunId ? `#${gridRunId}` : 'No run ID reported';
    refs.gridRunStatus.textContent = gridRunStatus ? gridRunStatus.toUpperCase() : 'STATUS NOT REPORTED';
    refs.gridRunStatus.title = gridRun.checked_at ? `Last checked ${String(gridRun.checked_at)}` : '';
    refs.gridRun.dataset.state = String(gridRun.status || 'unknown').toLowerCase();
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
    renderModelProvenance(state);
  }

  const AI_ROLES = [
    { agent: 'camera-closeup1', title: 'Camera · Close-up 1', decisionRole: 'camera' },
    { agent: 'camera-closeup2', title: 'Camera · Close-up 2', decisionRole: 'camera' },
    { agent: 'camera-closeup3', title: 'Camera · Close-up 3', decisionRole: 'camera' },
    { agent: 'camera-closeup4', title: 'Camera · Close-up 4', decisionRole: 'camera' },
    { agent: 'director', title: 'Director', decisionRole: 'director' },
    { agent: 'critic', title: 'Critic', decisionRole: 'critic' },
  ];

  function renderModelProfile(state) {
    const models = state.models;
    if (!models || !Array.isArray(models.options)) {
      refs.modelProfile.disabled = true;
      refs.modelProfileStatus.textContent = 'Model profiles not reported';
      return;
    }
    const options = models.options.filter((item) => item && typeof item.id === 'string' && typeof item.label === 'string');
    const signature = JSON.stringify(options.map((item) => [item.id, item.label, item.available === true]));
    if (signature !== profileOptionSignature) {
      profileOptionSignature = signature;
      refs.modelProfile.replaceChildren(...options.map((item) => {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = `${item.label}${item.available === true ? '' : ' · unavailable'}`;
        option.disabled = item.available !== true;
        return option;
      }));
    }
    const selected = typeof models.selected === 'string' ? models.selected : '';
    if (selected && [...refs.modelProfile.options].some((option) => option.value === selected)) refs.modelProfile.value = selected;
    refs.modelProfile.disabled = profileBusy || !options.some((item) => item.available === true);
    refs.modelProfile.setAttribute('aria-busy', String(profileBusy));
    const cameraModel = models.roles && models.roles.camera;
    const separateCamera = cameraModel && cameraModel.provider === 'flower';
    $('model-profile-label').textContent = separateCamera ? 'DIRECTOR + CRITIC' : 'AI PROFILE';
    refs.modelProfileStatus.textContent = profileBusy ? 'Switching profile…' : '';
    if (separateCamera && !profileBusy) {
      const reasoning = cameraModel.reasoning_effort === 'none' ? 'reasoning off' : `reasoning ${cameraModel.reasoning_effort || 'default'}`;
      refs.modelProfileStatus.textContent = `Cameras: ${cameraModel.model} via Flower · ${reasoning}`;
    }
    refs.modelVerificationStatus.textContent = '';
    refs.modelVerificationStatus.dataset.state = '';
  }

  function roleRecordKey(record) {
    if (!record || typeof record !== 'object') return '';
    if (typeof record.agent_id === 'string') return record.agent_id;
    const role = String(record.role || '').toLowerCase();
    const cameraId = String(record.camera_id || '').toLowerCase();
    if (role === 'camera' && cameraId) return `camera-${cameraId}`;
    return role;
  }

  function renderModelProvenance(state) {
    const flower = state.flower || {};
    const results = Array.isArray(flower.inference_results) ? flower.inference_results : [];
    const latest = new Map();
    for (const result of results) {
      const key = roleRecordKey(result);
      if (key && AI_ROLES.some((role) => role.agent === key)) latest.set(key, result);
    }
    const epoch = state.models && Number.isInteger(state.models.epoch) ? state.models.epoch : null;
    const session = state.session || {};
    const currentSessionId = session.id ?? null;
    const currentSessionEpoch = Number.isInteger(session.epoch) ? session.epoch : null;
    const currentOverrideEpoch = Number.isInteger(state.override_epoch) ? state.override_epoch
      : Number.isInteger(flower.override_epoch) ? flower.override_epoch : null;
    const modes = flower.decision_modes || {};
    const active = state.session && state.session.status === 'running';
    refs.aiProvenance.innerHTML = AI_ROLES.map((role) => {
      const result = latest.get(role.agent);
      const oldEpoch = result && epoch !== null && Number.isInteger(result.model_epoch) && result.model_epoch !== epoch;
      const oldSession = result && (result.session_id !== currentSessionId
        || currentSessionEpoch === null || result.epoch !== currentSessionEpoch
        || (currentOverrideEpoch !== null && result.override_epoch !== currentOverrideEpoch));
      const assignment = state.models && state.models.roles && state.models.roles[role.decisionRole];
      const wrongModel = result && assignment && assignment.model && result.model !== assignment.model;
      const stale = oldEpoch || oldSession || wrongModel;
      const accepted = result && result.status === 'completed' && typeof result.response_id === 'string' && result.response_id.length > 0 && !stale;
      const failed = result && ['failed', 'error', 'rejected', 'incomplete'].includes(String(result.status || '').toLowerCase()) && !stale;
      let status = accepted ? 'VERIFIED' : failed ? 'RESPONSE FAILED' : stale ? (oldEpoch ? 'PREVIOUS PROFILE' : 'PREVIOUS ROUND') : modes[role.decisionRole] === 'llm' ? (active ? 'WAITING FOR RESPONSE' : 'NO RESPONSE YET') : 'ROLE NOT REPORTED';
      const details = [];
      if (assignment && assignment.model && !accepted) details.push(`Configured: ${assignment.model}`);
      if (accepted) {
        // Structured response fields shown instead; no time/tokens/model line.
      } else if (failed) {
        if (finite(result.http_status)) details.push(`HTTP ${Math.round(result.http_status)}`);
        else if (typeof result.error_class === 'string') details.push(result.error_class.slice(0, 40));
      } else if (stale) {
        details.push(oldEpoch ? 'A response from an earlier model profile is retained; waiting for a fresh one.' : 'A response from an earlier session or control epoch is retained; waiting for a fresh one.');
      } else if (modes[role.decisionRole] === 'llm') {
        details.push('Configured AI role has not returned a verified response yet.');
      } else {
        details.push('Waiting for the controller to report this AI role.');
      }
      let responseHtml = '';
      if (accepted && result.result) {
        const r = result.result;
        if (role.decisionRole === 'camera') {
          responseHtml = `<span class="ai-response-fields">recommendation <b>${escapeHtml((r.recommendation || '?'))}</b> confidence <b>${typeof r.confidence === 'number' ? r.confidence.toFixed(2) : '?'}</b></span><span class="ai-response-reason">reason: ${escapeHtml((r.reason || '').slice(0, 200))}</span>`;
        } else if (role.agent === 'critic') {
          responseHtml = `<span class="ai-response-fields">assessment <b>${escapeHtml((r.assessment || '?'))}</b></span><span class="ai-response-reason">reason: ${escapeHtml((r.reason || '').slice(0, 200))}</span>`;
        } else if (role.agent === 'director') {
          responseHtml = `<span class="ai-response-fields">action <b>${escapeHtml((r.action || '?'))}</b> camera_id <b>${escapeHtml((r.camera_id || '?'))}</b></span><span class="ai-response-reason">reason: ${escapeHtml((r.reason || '').slice(0, 200))}</span>`;
        }
      }
      const detailsLine = details.length ? `<span>${escapeHtml(details.join(' · '))}</span>` : (!accepted ? `<span>${escapeHtml(status)}</span>` : '');
      return `<div class="ai-role-row" data-state="${accepted ? 'verified' : failed ? 'error' : 'pending'}"><span class="ai-role-mark" aria-hidden="true">${accepted ? '✓' : failed ? '!' : '·'}</span><span class="ai-role-copy"><span class="ai-role-head"><strong>${role.title}</strong><span class="ai-role-state">${status}</span></span>${detailsLine}${responseHtml}</span></div>`;
    }).join('');
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
    if (!session) return;
    const active = ['running', 'paused'].includes(session.status);
    const streamKey = `${session.id}:${active}`;
    if (programStreamKey !== streamKey) {
      programStreamKey = streamKey;
      refs.programImage.onload = () => { refs.programImage.classList.add('is-visible'); refs.stage.classList.add('has-image'); };
      refs.programImage.onerror = () => { programStreamKey = null; };
      refs.programImage.src = active ? `/api/program.mjpeg?session=${encodeURIComponent(session.id || '')}` : `/api/program.jpg?stopped=${Date.now()}`;
    }
    if (!active) return;
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
    const session = playbackSession(state);
    const available = Boolean(state.data && state.data.ami_available && session.input_mode === 'ami');
    if (!available) {
      if (!audioStartPending && session.input_mode !== 'ami') {
        audioEnabled = false;
        if (!refs.audio.paused) refs.audio.pause();
      } else if (!audioStartPending && !refs.audio.paused) {
        refs.audio.pause();
      }
      renderAudioControls(state);
      return;
    }
    const newSession = session.id !== audioSessionId;
    const newEpoch = session.epoch !== audioEpoch;
    if (newSession) audioMissing = false;
    if (audioMissing) return;
    if (!refs.audio.src || newSession) {
      refs.audio.src = '/api/audio';
      audioSessionId = session.id;
      audioEpoch = session.epoch;
    }
    if (newSession || newEpoch) {
      try { refs.audio.currentTime = Math.max(0, Number(session.time_s) || 0); } catch { /* Metadata may not have loaded yet. */ }
      audioEpoch = session.epoch;
    } else if (finite(session.time_s) && Number.isFinite(refs.audio.currentTime) && Math.abs(refs.audio.currentTime - session.time_s) > 0.3) {
      try { refs.audio.currentTime = Math.max(0, session.time_s); } catch { /* The media clock is not seekable yet. */ }
    }
    if (audioEnabled && session.status === 'running' && refs.audio.paused) {
      refs.audio.muted = false;
      refs.audio.play().catch(() => {
        if (audioStartPending) return;
        audioEnabled = false;
        refs.audioToggle.setAttribute('aria-pressed', 'false');
        toast('AMI audio needs a click to resume in this browser.', 'error');
        renderAudioControls(latestState || state);
      });
    } else if (session.status !== 'running' && !refs.audio.paused) {
      refs.audio.pause();
    }
    renderAudioControls(state);
  }

  function renderAudioControls(state) {
    const session = playbackSession(state);
    const hasActiveSession = ['running', 'paused'].includes(session.status);
    const inputMode = audioStartPending || !hasActiveSession ? refs.inputMode.value : (session.input_mode || refs.inputMode.value);
    const amiMode = inputMode === 'ami';
    const available = Boolean(state && state.data && state.data.ami_available);
    const active = session.status === 'running';
    const canAdjust = amiMode && available && !audioMissing;
    refs.audioToggle.disabled = !canAdjust || !active;
    refs.audioVolume.disabled = !canAdjust;
    refs.audioToggle.setAttribute('aria-pressed', String(audioEnabled && !refs.audio.paused));
    refs.audioToggle.innerHTML = ` <span aria-hidden="true">♫</span> ${audioEnabled && !refs.audio.paused ? 'Mute' : 'Unmute'} <i></i>`;
    refs.audioToggle.title = canAdjust && active ? (audioEnabled && !refs.audio.paused ? 'Mute AMI replay audio' : 'Play AMI replay audio') : 'Audio controls are available for an active AMI replay.';
    if (!amiMode) refs.audioStatus.textContent = 'Audio is available with AMI replay';
    else if (audioMissing) refs.audioStatus.textContent = 'AMI audio is unavailable in this browser';
    else if (audioStartPending) refs.audioStatus.textContent = 'Unlocking AMI audio for session start…';
    else if (!available) refs.audioStatus.textContent = 'AMI audio source is not available';
    else if (session.status === 'buffering') refs.audioStatus.textContent = 'Filling broadcast buffer · audio will start with delayed video';
    else if (!active) refs.audioStatus.textContent = 'AMI audio will start with the session';
    else if (audioEnabled && !refs.audio.paused) refs.audioStatus.textContent = `AMI replay playing · ${Math.round(refs.audio.volume * 100)}%`;
    else refs.audioStatus.textContent = 'AMI replay ready · click Unmute to listen';
  }

  function prepareAmiAudioFromGesture() {
    audioStartPending = true;
    audioMissing = false;
    audioEnabled = true;
    refs.audio.muted = true;
    refs.audio.volume = Number(refs.audioVolume.value);
    refs.audio.src = '/api/audio';
    refs.audioToggle.setAttribute('aria-pressed', 'true');
    renderAudioControls(latestState || {});
    // Call play synchronously from the Start click so browser activation is
    // captured before the session-start network request. State updates below
    // seek this same element to the session clock; camera cuts never reset it.
    try {
      const unlock = refs.audio.play();
      if (unlock && typeof unlock.then === 'function') unlock.then(() => { if (playbackSession(latestState).status !== 'running') refs.audio.pause(); }).catch(() => {
        if (!audioStartPending) return;
        refs.audioStatus.textContent = 'Session starting · waiting for AMI audio';
      });
    } catch {
      refs.audioStatus.textContent = 'Session starting · waiting for AMI audio';
    }
  }

  async function startSessionFromGesture() {
    const inputMode = refs.inputMode.value;
    if (inputMode === 'ami') prepareAmiAudioFromGesture();
    else {
      audioStartPending = false;
      audioEnabled = false;
      if (!refs.audio.paused) refs.audio.pause();
    }
    try {
      await runAction('/api/session/start', { input_mode: inputMode, output_mode: refs.outputMode.value, start_s: 0, output_delay_s: Number(refs.broadcastDelay.value) }, refs.start, 'Session start requested.');
    } catch {
      audioEnabled = false;
      if (!refs.audio.paused) refs.audio.pause();
    } finally {
      audioStartPending = false;
      if (inputMode === 'ami' && latestState && latestState.session && latestState.session.status === 'running' && latestState.session.input_mode === 'ami') {
        syncPreviewAudio(latestState);
        if (audioEnabled && refs.audio.paused && playbackSession(latestState).status === 'running') {
          refs.audio.play().catch(() => {
            audioEnabled = false;
            toast('AMI audio needs a click to resume in this browser.', 'error');
            renderAudioControls(latestState || {});
          });
        }
      }
      renderControls();
    }
  }

  refs.audio.addEventListener('loadedmetadata', () => {
    const session = playbackSession(latestState);
    if (!session || session.input_mode !== 'ami' || !finite(session.time_s)) return;
    try { refs.audio.currentTime = Math.max(0, session.time_s); } catch { /* A later state snapshot retries the seek. */ }
  });

  $('start-session').addEventListener('click', () => { void startSessionFromGesture(); });
  refs.modelProfile.addEventListener('change', async () => {
    const profile = refs.modelProfile.value;
    const models = latestState && latestState.models;
    if (!['kimi', 'minimax'].includes(profile) || !models || profile === models.selected) return;
    const choice = Array.isArray(models.options) ? models.options.find((item) => item.id === profile) : null;
    if (!choice || choice.available !== true) {
      renderModelProfile(latestState || {});
      return;
    }
    profileBusy = true;
    renderModelProfile(latestState || {});
    try {
      const state = await post('/api/models/select', { profile });
      if (state && typeof state === 'object' && state.models && state.models.selected === profile) applyState(state);
      else {
        await fetchState();
        if (!latestState || !latestState.models || latestState.models.selected !== profile) throw new Error('Profile selection was not confirmed.');
      }
      const label = choice.label || profile;
      toast(`${label} selected · waiting for fresh AI responses.`, 'success');
    } catch {
      if (latestState) renderModelProfile(latestState);
      toast('Model profile could not be changed. Check the controller and try again.', 'error');
    } finally {
      profileBusy = false;
      if (latestState) renderModelProfile(latestState);
    }
  });
  refs.inputMode.addEventListener('change', () => { inputChoiceTouched = true; renderAudioControls(latestState || {}); });
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
    if (audioStartPending) {
      refs.audioStatus.textContent = 'Session starting · waiting for AMI audio';
      return;
    }
    audioMissing = true; audioEnabled = false; refs.audioToggle.setAttribute('aria-pressed', 'false');
    renderControls(); toast('AMI audio could not be loaded from the controller.', 'error');
  });
  refs.audioToggle.addEventListener('click', async () => {
    if (refs.audioToggle.disabled) return;
    if (audioEnabled) {
      audioEnabled = false; refs.audio.pause(); refs.audioToggle.setAttribute('aria-pressed', 'false');
      renderAudioControls(latestState || {});
      return;
    }
    const session = playbackSession(latestState);
    refs.audio.volume = Number(refs.audioVolume.value);
    refs.audio.muted = false;
    if (session && finite(session.time_s)) {
      try { refs.audio.currentTime = session.time_s; } catch { /* The source initializes at the current session time on metadata load. */ }
    }
    try {
      await refs.audio.play();
      audioEnabled = true; refs.audioToggle.setAttribute('aria-pressed', 'true');
      renderAudioControls(latestState || {});
    } catch {
      toast('Audio is unavailable or blocked by this browser.', 'error');
      audioMissing = true; renderControls();
    }
  });
  refs.audioVolume.addEventListener('input', () => {
    refs.audio.volume = Number(refs.audioVolume.value);
    renderAudioControls(latestState || {});
  });

  function openCredits() {
    if (typeof refs.creditsDialog.showModal === 'function') refs.creditsDialog.showModal();
    else refs.creditsDialog.setAttribute('open', '');
  }
  $('credits-open').addEventListener('click', openCredits);
  $('credits-open-footer').addEventListener('click', openCredits);

  refs.audio.volume = Number(refs.audioVolume.value);
  renderModelProfile({});
  renderAudioControls({});

  // ── Theme toggle (day / night) ─────────────────────────
  const themeBtn = $('theme-toggle');
  const themeIcon = $('theme-toggle-icon');
  if (themeBtn && themeIcon) {
    function applyTheme(t) {
      document.documentElement.dataset.theme = t;
      themeIcon.textContent = t === 'night' ? '☽' : '☀';
      document.querySelector('meta[name="theme-color"]').content = t === 'night' ? '#09090b' : '#f5f0e8';
      try { localStorage.setItem('noesis-theme', t); } catch(e) {}
    }
    applyTheme(document.documentElement.dataset.theme || 'day');
    themeBtn.addEventListener('click', () => {
      applyTheme(document.documentElement.dataset.theme === 'night' ? 'day' : 'night');
    });
  }
  void fetchState();
  void fetchAgentSnapshot();
  startEvents();
  window.setInterval(() => { if (Date.now() - lastStateAt > 3500) void fetchState(); }, 2200);
  window.setInterval(() => { void fetchAgentSnapshot(); }, 5000);
  window.setInterval(refreshFrames, 200);
})();
