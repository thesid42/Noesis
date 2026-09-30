(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const refs = {
    output: $('program-output'), image: $('program-feed'), live: $('output-live'), liveText: $('output-live').querySelector('span'),
    label: $('output-label'), clock: $('output-clock'), kicker: $('output-kicker'), camera: $('output-camera'),
    reason: $('output-reason'), input: $('input-label'), scene: $('scene-label'), audio: $('program-audio'),
    audioButton: $('program-audio-button'), audioLabel: $('audio-label'), error: $('program-error'), progress: $('output-progress'),
  };
  const cameraNames = { closeup1: 'Close-up 1', closeup2: 'Close-up 2', closeup3: 'Close-up 3', closeup4: 'Close-up 4', corner: 'Corner', slate: 'Unavailable slate' };
  let current = null;
  let lastStateAt = 0;
  let clockReceivedAt = performance.now();
  let stateInFlight = false;
  let frameInFlight = false;
  const obsOutput = new URLSearchParams(window.location.search).get('obs') === '1';
  let audioEnabled = obsOutput;
  let audioAvailable = false;
  let audioUnavailable = false;
  let audioSessionId = null;
  let audioEpoch = null;
  let streamKey = null;

  function playbackSession(state) {
    const session = state && state.session || {};
    const broadcast = state && state.broadcast;
    if (!broadcast || typeof broadcast.time_s !== 'number') return session;
    return { ...session, time_s: broadcast.time_s,
      status: session.status === 'running' && !broadcast.ready ? 'buffering' : session.status };
  }

  function fmtTime(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '00:00';
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  function fmt(value) { return String(value ?? ''); }
  async function json(path) {
    const response = await fetch(path, { signal: AbortSignal.timeout(3000), cache: 'no-store', headers: { Accept: 'application/json' } });
    const body = await response.text();
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return body ? JSON.parse(body) : null;
  }
  function setText(node, text) { node.textContent = text; }

  function render(state) {
    if (!state || typeof state !== 'object') return;
    const incomingSessionId = state.session && state.session.id;
    if (incomingSessionId && incomingSessionId !== audioSessionId) audioUnavailable = false;
    current = state; lastStateAt = Date.now(); clockReceivedAt = performance.now();
    const session = playbackSession(state);
    const program = state.program || {};
    const cameraId = program.camera_id;
    const camera = Array.isArray(state.cameras) ? state.cameras.find((item) => item.id === cameraId) : null;
    const name = (camera && camera.name) || cameraNames[cameraId] || cameraId || 'Awaiting program source';
    const status = session.status || 'unknown';
    const active = status === 'running';
    refs.output.dataset.status = status;
    refs.live.dataset.active = String(active);
    setText(refs.liveText, active ? 'ON AIR' : status === 'buffering' ? 'BUFFERING' : status === 'paused' ? 'PAUSED' : status === 'stopped' ? 'STOPPED' : 'STANDBY');
    const duration = Number(session.duration_s) || 0;
    const elapsed = Math.max(0, Number(session.time_s) || 0);
    setText(refs.clock, duration > 0 ? `${fmtTime(elapsed)} / ${fmtTime(duration)}` : fmtTime(elapsed));
    if (refs.progress) refs.progress.style.width = `${duration > 0 ? Math.min(100, (elapsed / duration) * 100) : 0}%`;
    setText(refs.camera, name);
    setText(refs.reason, program.reason || (program.scene ? `Scene · ${program.scene}` : 'The controller’s selected camera will appear here.'));
    setText(refs.scene, program.scene || '');

    const input = session.input_mode;
    const amiReady = Boolean(state.data && state.data.ami_available && input === 'ami');
    audioAvailable = amiReady && !audioUnavailable;
    refs.audioButton.hidden = obsOutput || (!amiReady && !audioUnavailable);
    if (audioUnavailable) {
      refs.audioButton.hidden = false;
      refs.audioButton.disabled = true;
      setText(refs.audioLabel, 'Program audio unavailable');
    } else if (amiReady) {
      refs.audioButton.disabled = session.status !== 'running' && !audioEnabled;
      setText(refs.audioLabel, audioEnabled ? 'Audio on' : 'Enable program audio');
    }
    if (input === 'ami') {
      const credits = String((state.data && state.data.credits) || '');
      const sessionMatch = credits.match(/session\s+([A-Za-z0-9_-]+)/i);
      const sessionLabel = sessionMatch ? ` · ${sessionMatch[1]}` : '';
      setText(refs.input, amiReady ? `AMI PROJECT${sessionLabel} · CC BY 4.0 · EDITED REPLAY` : 'AMI REPLAY · AUDIO UNAVAILABLE');
    } else if (input === 'synthetic') {
      setText(refs.input, 'SYNTHETIC TEST FEEDS');
    } else {
      setText(refs.input, 'INPUT NOT REPORTED');
    }
    refs.label.textContent = session.output_mode === 'preview' ? 'LOCAL PREVIEW OUTPUT' : 'LIVE PROGRAM SIGNAL';
    refs.kicker.textContent = session.output_mode === 'preview' ? 'PREVIEW · NOESIS' : 'NOESIS';
    if (state.data && Array.isArray(state.data.missing_files) && state.data.missing_files.length && input === 'ami') {
      setText(refs.error, `Media unavailable · ${state.data.missing_files.join(', ')}`);
    } else {
      setText(refs.error, '');
    }
    syncAudio(session);
  }

  function syncAudio(session) {
    if (!audioAvailable || audioUnavailable) {
      if (!refs.audio.paused) refs.audio.pause();
      return;
    }
    const newSession = session.id !== audioSessionId;
    const newEpoch = session.epoch !== audioEpoch;
    if (newSession || !refs.audio.src) {
      refs.audio.src = `/api/audio?session=${encodeURIComponent(session.id || '')}`;
      audioSessionId = session.id;
    }
    if (newSession || newEpoch) {
      try { refs.audio.currentTime = Math.max(0, Number(session.time_s) || 0); } catch { /* Wait for audio metadata before snapping its clock. */ }
      audioEpoch = session.epoch;
    } else if (typeof session.time_s === 'number' && Number.isFinite(refs.audio.currentTime)) {
      const drift = audioTarget() - refs.audio.currentTime;
      if (Math.abs(drift) > 0.3) {
        try { refs.audio.currentTime = audioTarget(); } catch { /* Wait for seekable metadata. */ }
      }
      refs.audio.playbackRate = Math.abs(drift) > 0.06 && Math.abs(drift) <= 0.3 ? (drift > 0 ? 1.03 : 0.97) : 1;
    }
    if (audioEnabled && session.status === 'running' && refs.audio.paused) {
      refs.audio.play().catch(() => setText(refs.error, 'Audio needs a user click in this browser.'));
    } else if (session.status !== 'running' && !refs.audio.paused) {
      refs.audio.pause();
    }
  }

  function audioTarget() {
    const session = playbackSession(current);
    if (!session) return 0;
    const elapsed = session.status === 'running' ? (performance.now() - clockReceivedAt) / 1000 : 0;
    return Math.max(0, Number(session.time_s) || 0) + elapsed;
  }

  refs.audio.addEventListener('loadedmetadata', () => {
    try { refs.audio.currentTime = audioTarget(); } catch { /* A subsequent state update retries. */ }
  });

  async function updateFrame() {
    const session = current && current.session;
    if (!session || Date.now() - lastStateAt > 3500) return;
    const active = ['running', 'paused'].includes(session.status);
    const key = `${session.id}:${active}`;
    if (streamKey === key) return;
    streamKey = key;
    refs.image.onload = () => { refs.image.classList.add('is-visible'); refs.output.classList.add('has-frame'); };
    refs.image.onerror = () => { streamKey = null; };
    refs.image.src = active ? `/api/program.mjpeg?session=${encodeURIComponent(session.id || '')}` : `/api/program.jpg?stopped=${Date.now()}`;
  }

  async function fetchState() {
    if (stateInFlight) return;
    stateInFlight = true;
    try { render(await json('/api/state')); }
    catch { if (!current) setText(refs.reason, 'Waiting for the local production controller.'); }
    finally { stateInFlight = false; }
  }
  function startEvents() {
    if (!('EventSource' in window)) return;
    try {
      const events = new EventSource('/api/events');
      events.addEventListener('state', (event) => { try { render(JSON.parse(event.data)); } catch { /* A polling snapshot will repair malformed event data. */ } });
    } catch { /* State polling below is the compatibility path. */ }
  }

  refs.audioButton.addEventListener('click', async () => {
    if (!current || !audioAvailable || audioUnavailable) return;
    if (audioEnabled) {
      audioEnabled = false; refs.audio.pause(); refs.audioButton.setAttribute('aria-pressed', 'false'); setText(refs.audioLabel, 'Enable program audio');
      return;
    }
    const session = playbackSession(current);
    if (session.status !== 'running') return;
    if (!refs.audio.src) { refs.audio.src = '/api/audio'; audioSessionId = session.id; audioEpoch = session.epoch; }
    try {
      if (typeof session.time_s === 'number') {
        try { refs.audio.currentTime = session.time_s; } catch { /* Metadata can arrive after playback begins. */ }
      }
      await refs.audio.play();
      audioEnabled = true; refs.audioButton.setAttribute('aria-pressed', 'true'); setText(refs.audioLabel, 'Audio on');
      setText(refs.error, '');
    } catch {
      audioUnavailable = true; audioAvailable = false; refs.audioButton.disabled = true; setText(refs.audioLabel, 'Program audio unavailable');
      setText(refs.error, 'Audio could not be loaded from the controller.');
    }
  });
  refs.audio.addEventListener('error', () => {
    if (!refs.audio.src) return;
    audioUnavailable = true; audioAvailable = false; audioEnabled = false;
    refs.audioButton.hidden = false; refs.audioButton.disabled = true; refs.audioButton.setAttribute('aria-pressed', 'false');
    setText(refs.audioLabel, 'Program audio unavailable'); setText(refs.error, 'Audio could not be loaded from the controller.');
  });

  void fetchState();
  startEvents();
  window.setInterval(() => {
    if (Date.now() - lastStateAt > 3500) {
      refs.live.dataset.active = 'false';
      setText(refs.liveText, 'CONTROLLER OFFLINE');
      setText(refs.error, 'Holding the last frame while the local controller reconnects.');
      if (!refs.audio.paused) refs.audio.pause();
      void fetchState();
    }
  }, 1000);
  window.setInterval(updateFrame, 250);
})();
