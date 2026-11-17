(() => {
  const editor = document.querySelector('[data-audio-studio-editor]');
  if (!editor) return;

  const tabs = [...editor.querySelectorAll('[data-sampler-mode]')];
  const panels = [...editor.querySelectorAll('[data-sampler-panel]')];
  const pads = [...editor.querySelectorAll('[data-sampler-pad]')];
  const stateLabel = editor.querySelector('[data-sampler-audio-state]');
  if (!tabs.length || !panels.length || pads.length !== 16) return;

  let audioBuffer = null;
  let trimStart = 0;
  let trimEnd = 0;
  let currentMode = 'waveform';
  let receivedWaveformState = false;
  let audioContext = null;
  const activeVoices = new Array(pads.length).fill(0);
  const keyToPad = new Map();

  const formatDuration = seconds => `${Math.max(0, seconds).toFixed(2)} s`;

  const setMode = mode => {
    currentMode = mode;
    tabs.forEach(tab => {
      const selected = tab.dataset.samplerMode === mode;
      tab.setAttribute('aria-selected', String(selected));
      tab.classList.toggle('is-active', selected);
      tab.tabIndex = selected ? 0 : -1;
    });
    panels.forEach(panel => { panel.hidden = panel.dataset.samplerPanel !== mode; });
    if (mode === 'waveform') {
      // WaveSurfer observes resizes; this also covers browsers that missed the hidden→visible transition.
      requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
    }
  };

  const refreshPads = () => {
    const usable = audioBuffer && Number.isFinite(trimStart) && Number.isFinite(trimEnd) && trimEnd > trimStart;
    const sliceDuration = usable ? (trimEnd - trimStart) / pads.length : 0;
    pads.forEach((pad, index) => {
      const durationElement = pad.querySelector('[data-sampler-duration]');
      const start = trimStart + index * sliceDuration;
      const end = index === pads.length - 1 ? trimEnd : start + sliceDuration;
      const duration = usable ? end - start : 0;
      pad.disabled = !usable || duration <= 0;
      if (durationElement) durationElement.textContent = usable ? formatDuration(duration) : '—';
      pad.setAttribute('aria-label', usable
        ? `Pad ${String(index + 1).padStart(2, '0')}, touche ${pad.dataset.samplerKey}, ${formatDuration(duration)}`
        : `Pad ${String(index + 1).padStart(2, '0')}, audio indisponible`);
    });
    if (stateLabel) {
      stateLabel.textContent = usable
        ? `16 pads prêts · zone ${formatDuration(trimStart)}–${formatDuration(trimEnd)}`
        : receivedWaveformState ? 'Audio source indisponible pour le sampler.' : 'Chargement du son…';
      stateLabel.classList.toggle('is-ready', Boolean(usable));
    }
  };

  const getAudioContext = () => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error('Web Audio API indisponible dans ce navigateur.');
    if (!audioContext || audioContext.state === 'closed') audioContext = new AudioContextClass();
    // Call resume synchronously from the user gesture for Safari's autoplay policy.
    return audioContext.state === 'suspended' ? audioContext.resume() : Promise.resolve();
  };

  const playPad = async index => {
    if (!audioBuffer || index < 0 || index >= pads.length || trimEnd <= trimStart) return;
    const sliceDuration = (trimEnd - trimStart) / pads.length;
    const start = trimStart + index * sliceDuration;
    const duration = index === pads.length - 1 ? trimEnd - start : sliceDuration;
    if (duration <= 0) return;

    try {
      await getAudioContext();
      const source = audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(audioContext.destination);
      const pad = pads[index];
      activeVoices[index] += 1;
      pad.classList.add('is-playing');
      source.onended = () => {
        activeVoices[index] = Math.max(0, activeVoices[index] - 1);
        if (!activeVoices[index]) pad.classList.remove('is-playing');
        source.disconnect();
      };
      // A new source node per hit preserves overlapping/polyphonic playback.
      source.start(0, start, duration);
    } catch (error) {
      console.error('Lecture du pad impossible.', error);
      if (stateLabel) stateLabel.textContent = 'Impossible d’activer le son. Clique à nouveau sur un pad.';
    }
  };

  pads.forEach((pad, index) => {
    const key = (pad.dataset.samplerKey || '').toLowerCase();
    if (key) keyToPad.set(key, index);
    if (pad.dataset.samplerCode) keyToPad.set(`code:${pad.dataset.samplerCode}`, index);
    pad.addEventListener('click', () => playPad(index));
  });

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => setMode(tab.dataset.samplerMode));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const step = event.key === 'ArrowRight' ? 1 : -1;
      const nextIndex = (index + step + tabs.length) % tabs.length;
      tabs[nextIndex].focus();
      setMode(tabs[nextIndex].dataset.samplerMode);
    });
  });

  document.addEventListener('keydown', event => {
    if (currentMode !== 'pads') return;
    if (event.repeat || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    if (event.target instanceof Element && event.target.closest('input, textarea, select, [contenteditable="true"], dialog[open]')) return;
    const key = event.code.startsWith('Digit') ? `code:${event.code}` : event.key.toLowerCase();
    const index = keyToPad.get(key);
    if (index === undefined) return;
    event.preventDefault();
    playPad(index);
  });

  window.addEventListener('audio-studio:waveform-state', event => {
    const detail = event.detail || {};
    receivedWaveformState = true;
    audioBuffer = detail.buffer || null;
    trimStart = Math.max(0, Number(detail.start) || 0);
    trimEnd = Math.max(trimStart, Number(detail.end) || 0);
    refreshPads();
  });

  window.addEventListener('pagehide', () => {
    if (audioContext && audioContext.state !== 'closed') audioContext.close().catch(() => {});
  }, {once: true});

  setMode('waveform');
  refreshPads();
})();
