(() => {
  const editor = document.querySelector('[data-audio-studio-editor]');
  if (!editor) return;

  const tabs = [...editor.querySelectorAll('[data-sampler-mode]')];
  const panels = [...editor.querySelectorAll('[data-sampler-panel]')];
  const pads = [...editor.querySelectorAll('[data-sampler-pad]')];
  const stateLabel = editor.querySelector('[data-sampler-audio-state]');
  const recordButton = editor.querySelector('[data-sequencer-record]');
  const stopButton = editor.querySelector('[data-sequencer-stop]');
  const clock = editor.querySelector('[data-sequencer-clock]');
  const playSequenceButton = editor.querySelector('[data-sequencer-play]');
  const saveButton = editor.querySelector('[data-sequencer-save]');
  const nameInput = editor.querySelector('[data-sequencer-name]');
  const sequencerStatus = editor.querySelector('[data-sequencer-status]');
  const sequencerError = editor.querySelector('[data-sequencer-error]');
  const bpmInput = editor.querySelector('[data-sequencer-bpm]');
  const bpmRateLabel = editor.querySelector('[data-sequencer-bpm-rate]');
  const metronomeToggle = editor.querySelector('[data-metronome-toggle]');
  const metronomeBeat = editor.querySelector('[data-metronome-beat]');
  const metronomeBeatCount = editor.querySelector('[data-metronome-beat-count]');
  if (!tabs.length || !panels.length || pads.length !== 16) return;

  let audioBuffer = null;
  let trimStart = 0;
  let trimEnd = 0;
  let currentMode = 'waveform';
  let receivedWaveformState = false;
  let audioContext = null;
  let recording = false;
  let recordingStartTime = 0;
  let recordingBpm = 120;
  let sequenceBpm = 120;
  let recordingDurationMs = 0;
  let recordedEvents = [];
  let recordingBoundaries = null;
  let clockFrame = null;
  let metronomeEnabled = false;
  let metronomeRunning = false;
  let metronomeTimer = null;
  let metronomeVisualFrame = null;
  let nextMetronomeBeatTime = 0;
  let nextMetronomeBeatIndex = 0;
  const scheduledMetronomeClicks = new Set();
  const activeVoices = new Array(pads.length).fill(0);
  const keyToPad = new Map();
  const MAX_RECORDED_EVENTS = 500;
  const METRONOME_LOOKAHEAD_SECONDS = 0.1;
  const METRONOME_SCHEDULER_INTERVAL_MS = 25;
  const METRONOME_CLICK_SECONDS = 0.05;

  const formatDuration = seconds => `${Math.max(0, seconds).toFixed(2)} s`;
  const getBpm = () => {
    const value = Number(bpmInput?.value);
    return Number.isInteger(value) && value >= 60 && value <= 200 ? value : 120;
  };
  const updateBpmRateLabel = () => {
    if (!bpmRateLabel) return;
    const bpm = getBpm();
    const description = bpm === 120 ? 'Normal' : bpm < 120 ? 'Ralenti' : 'Accéléré';
    bpmRateLabel.textContent = `${bpm} BPM (${description})`;
  };
  const formatClock = milliseconds => {
    const totalTenths = Math.floor(Math.max(0, milliseconds) / 100);
    const minutes = Math.floor(totalTenths / 600);
    const seconds = Math.floor(totalTenths / 10) % 60;
    const tenths = totalTenths % 10;
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${tenths}`;
  };

  const getBoundaries = () => {
    if (!audioBuffer || !Number.isFinite(trimStart) || !Number.isFinite(trimEnd) || trimEnd <= trimStart) return null;
    const width = (trimEnd - trimStart) / pads.length;
    return pads.map((_pad, index) => {
      const start = trimStart + index * width;
      const end = index === pads.length - 1 ? trimEnd : start + width;
      return [start, end];
    });
  };

  const showError = message => {
    if (!sequencerError) return;
    sequencerError.textContent = message || '';
    sequencerError.hidden = !message;
  };

  const updateSequencerButtons = () => {
    const usable = Boolean(getBoundaries());
    if (recordButton) recordButton.disabled = !usable || recording;
    if (stopButton) {
      stopButton.hidden = !recording;
      stopButton.disabled = !recording;
    }
    if (playSequenceButton) playSequenceButton.disabled = recording || !recordedEvents.length || !recordingBoundaries;
    if (saveButton) saveButton.disabled = recording || !recordedEvents.length || !recordingBoundaries;
  };

  const setMode = mode => {
    if (recording && mode !== 'pads') stopRecording();
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
    const boundaries = getBoundaries();
    pads.forEach((pad, index) => {
      const durationElement = pad.querySelector('[data-sampler-duration]');
      const boundary = boundaries && boundaries[index];
      const duration = boundary ? boundary[1] - boundary[0] : 0;
      pad.disabled = !boundary || duration <= 0;
      if (durationElement) durationElement.textContent = boundary ? formatDuration(duration) : '—';
      pad.setAttribute('aria-label', boundary
        ? `Pad ${String(index + 1).padStart(2, '0')}, touche ${pad.dataset.samplerKey}, ${formatDuration(duration)}`
        : `Pad ${String(index + 1).padStart(2, '0')}, audio indisponible`);
    });
    if (stateLabel) {
      stateLabel.textContent = boundaries
        ? `16 pads prêts · zone ${formatDuration(trimStart)}–${formatDuration(trimEnd)}`
        : receivedWaveformState ? 'Audio source indisponible pour le sampler.' : 'Chargement du son…';
      stateLabel.classList.toggle('is-ready', Boolean(boundaries));
    }
    updateSequencerButtons();
  };

  const getAudioContext = () => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error('Web Audio API indisponible dans ce navigateur.');
    if (!audioContext || audioContext.state === 'closed') audioContext = new AudioContextClass();
    // Call resume synchronously from the user gesture for Safari's autoplay policy.
    return audioContext.state === 'suspended' ? audioContext.resume() : Promise.resolve();
  };

  const refreshMetronomeToggle = () => {
    if (!metronomeToggle) return;
    metronomeToggle.setAttribute('aria-pressed', String(metronomeEnabled));
    metronomeToggle.textContent = `♫ Métronome ${metronomeEnabled ? 'ON' : 'OFF'}`;
  };

  const scheduleMetronomeClick = (when, beatIndex) => {
    const accent = beatIndex % 4 === 0;
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();
    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(accent ? 1000 : 800, when);
    gain.gain.setValueAtTime(0.0001, when);
    gain.gain.exponentialRampToValueAtTime(accent ? 0.24 : 0.14, when + 0.006);
    gain.gain.exponentialRampToValueAtTime(0.0001, when + METRONOME_CLICK_SECONDS);
    oscillator.connect(gain);
    gain.connect(audioContext.destination);

    const click = { oscillator, gain, when };
    scheduledMetronomeClicks.add(click);
    oscillator.onended = () => {
      scheduledMetronomeClicks.delete(click);
      oscillator.disconnect();
      gain.disconnect();
    };
    oscillator.start(when);
    oscillator.stop(when + METRONOME_CLICK_SECONDS + 0.005);
  };

  const runMetronomeScheduler = () => {
    if (!metronomeRunning || !audioContext) return;
    const horizon = audioContext.currentTime + METRONOME_LOOKAHEAD_SECONDS;
    const beatDuration = 60 / recordingBpm;
    while (nextMetronomeBeatTime < horizon) {
      if (nextMetronomeBeatTime >= audioContext.currentTime - 0.005) {
        scheduleMetronomeClick(nextMetronomeBeatTime, nextMetronomeBeatIndex);
      }
      nextMetronomeBeatTime += beatDuration;
      nextMetronomeBeatIndex += 1;
    }
    metronomeTimer = window.setTimeout(runMetronomeScheduler, METRONOME_SCHEDULER_INTERVAL_MS);
  };

  const updateMetronomeBeatDisplay = () => {
    if (!metronomeRunning || !audioContext) return;
    const beatDuration = 60 / recordingBpm;
    const elapsed = Math.max(0, audioContext.currentTime - recordingStartTime);
    const beatPosition = elapsed / beatDuration;
    const beatIndex = Math.floor(beatPosition);
    const beatInMeasure = (beatIndex % 4) + 1;
    const withinBeat = beatPosition - beatIndex;
    const accentPulse = beatInMeasure === 1 && withinBeat < 0.2;
    if (metronomeBeatCount) metronomeBeatCount.textContent = String(beatInMeasure);
    if (metronomeBeat) {
      metronomeBeat.classList.toggle('is-accent', accentPulse);
      metronomeBeat.setAttribute('aria-label', `Métronome actif · temps ${beatInMeasure} sur 4${accentPulse ? ' · premier temps' : ''}`);
    }
    metronomeVisualFrame = requestAnimationFrame(updateMetronomeBeatDisplay);
  };

  const stopMetronome = () => {
    metronomeRunning = false;
    if (metronomeTimer !== null) window.clearTimeout(metronomeTimer);
    metronomeTimer = null;
    if (metronomeVisualFrame !== null) cancelAnimationFrame(metronomeVisualFrame);
    metronomeVisualFrame = null;
    const now = audioContext?.currentTime || 0;
    scheduledMetronomeClicks.forEach(({ oscillator, gain }) => {
      try {
        gain.gain.cancelScheduledValues(now);
        gain.gain.setValueAtTime(0, now);
        oscillator.stop(now);
      } catch (_error) {
        // A click that has already ended no longer needs cancellation.
      }
    });
    if (metronomeBeatCount) metronomeBeatCount.textContent = '—';
    if (metronomeBeat) {
      metronomeBeat.classList.remove('is-accent');
      metronomeBeat.setAttribute('aria-label', 'Métronome arrêté');
    }
  };

  const startMetronome = (startTime, bpm, firstBeatIndex = 0) => {
    stopMetronome();
    if (!audioContext || !metronomeEnabled) return;
    recordingStartTime = startTime;
    recordingBpm = bpm;
    const beatDuration = 60 / recordingBpm;
    nextMetronomeBeatIndex = firstBeatIndex;
    nextMetronomeBeatTime = recordingStartTime + firstBeatIndex * beatDuration;
    metronomeRunning = true;
    runMetronomeScheduler();
    updateMetronomeBeatDisplay();
  };

  const toggleMetronome = () => {
    metronomeEnabled = !metronomeEnabled;
    refreshMetronomeToggle();
    if (!recording) return;
    if (!metronomeEnabled) {
      stopMetronome();
      return;
    }
    const beatDuration = 60 / recordingBpm;
    const elapsed = Math.max(0, audioContext.currentTime - recordingStartTime);
    const nextBeatIndex = Math.max(0, Math.ceil(elapsed / beatDuration - 0.000001));
    startMetronome(recordingStartTime, recordingBpm, nextBeatIndex);
  };

  const schedulePad = (index, when, boundaries, bpm) => {
    if (!audioBuffer || !boundaries || !boundaries[index]) return;
    const [start, end] = boundaries[index];
    const duration = end - start;
    if (duration <= 0) return;
    const source = audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.playbackRate.setValueAtTime(bpm / 120, when);
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
    source.start(when, start, duration);
  };

  const playPad = async index => {
    const boundaries = getBoundaries();
    if (!boundaries || index < 0 || index >= pads.length) return;
    try {
      await getAudioContext();
      const scheduledAt = audioContext.currentTime;
      const playbackBpm = recording ? recordingBpm : getBpm();
      schedulePad(index, scheduledAt, boundaries, playbackBpm);
      if (recording) {
        if (recordedEvents.length >= MAX_RECORDED_EVENTS) {
          if (sequencerStatus) sequencerStatus.textContent = `Limite de ${MAX_RECORDED_EVENTS} déclenchements atteinte. Enregistrement arrêté.`;
          stopRecording();
          return;
        }
        recordedEvents.push({
          pad_index: index,
          timestamp_ms: Math.max(0, Math.round((scheduledAt - recordingStartTime) * 1000)),
        });
        updateSequencerButtons();
        if (sequencerStatus) sequencerStatus.textContent = `${recordedEvents.length} déclenchement(s) enregistré(s).`;
      }
    } catch (error) {
      console.error('Lecture du pad impossible.', error);
      if (stateLabel) stateLabel.textContent = 'Impossible d’activer le son. Clique à nouveau sur un pad.';
    }
  };

  const updateClock = () => {
    if (!recording || !audioContext) return;
    clock.textContent = formatClock((audioContext.currentTime - recordingStartTime) * 1000);
    clockFrame = requestAnimationFrame(updateClock);
  };

  const startRecording = async () => {
    const boundaries = getBoundaries();
    if (!boundaries) return;
    showError('');
    try {
      await getAudioContext();
      recordedEvents = [];
      recordingBoundaries = boundaries.map(boundary => [...boundary]);
      recordingDurationMs = 0;
      recordingBpm = getBpm();
      sequenceBpm = recordingBpm;
      recordingStartTime = audioContext.currentTime;
      recording = true;
      if (metronomeEnabled) startMetronome(recordingStartTime, recordingBpm);
      if (clock) clock.textContent = '00:00.0';
      if (sequencerStatus) {
        sequencerStatus.textContent = metronomeEnabled
          ? `Enregistrement en cours à ${recordingBpm} BPM… joue les pads ou utilise le clavier.`
          : 'Enregistrement en cours… joue les pads ou utilise le clavier.';
      }
      updateSequencerButtons();
      updateClock();
    } catch (error) {
      showError(error.message || 'Impossible de démarrer l’enregistrement.');
    }
  };

  function stopRecording() {
    if (!recording) return;
    recordingDurationMs = Math.max(1, Math.round((audioContext.currentTime - recordingStartTime) * 1000));
    stopMetronome();
    recording = false;
    if (clockFrame !== null) cancelAnimationFrame(clockFrame);
    clockFrame = null;
    if (clock) clock.textContent = formatClock(recordingDurationMs);
    if (sequencerStatus) {
      sequencerStatus.textContent = recordedEvents.length
        ? `${recordedEvents.length} déclenchement(s) prêt(s) à écouter ou sauvegarder.`
        : 'Aucun pad enregistré. Clique sur Enregistrer pour recommencer.';
    }
    updateSequencerButtons();
  }

  const playSequence = async () => {
    if (!recordedEvents.length || !recordingBoundaries || recording) return;
    showError('');
    try {
      await getAudioContext();
      const startAt = audioContext.currentTime + 0.08;
      recordedEvents.forEach(event => {
        schedulePad(event.pad_index, startAt + event.timestamp_ms / 1000, recordingBoundaries, sequenceBpm);
      });
      if (sequencerStatus) sequencerStatus.textContent = 'Lecture de la séquence…';
    } catch (error) {
      showError(error.message || 'Impossible de lire la séquence.');
    }
  };

  const saveSequence = async () => {
    if (!recordedEvents.length || !recordingBoundaries || recording) return;
    const name = (nameInput?.value || '').trim();
    if (!name) {
      showError('Donne un nom à la composition.');
      nameInput?.focus();
      return;
    }
    if (name.length > 120) {
      showError('Le nom ne peut pas dépasser 120 caractères.');
      return;
    }
    const csrf = editor.querySelector('input[name="csrfmiddlewaretoken"]')?.value;
    if (!csrf) {
      showError('Jeton de sécurité manquant. Recharge la page.');
      return;
    }
    saveButton.disabled = true;
    if (sequencerStatus) sequencerStatus.textContent = 'Création de la composition…';
    showError('');
    try {
      const response = await fetch(editor.dataset.sequenceSaveUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify({
          name,
          sequence_data: {
            version: 1,
            recording_duration_ms: recordingDurationMs,
            bpm: sequenceBpm,
            events: recordedEvents,
          },
          pad_boundaries: recordingBoundaries,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'La composition n’a pas pu être créée.');
      if (!data.redirect_url) throw new Error('Réponse serveur incomplète. Recharge la page.');
      window.location.assign(data.redirect_url);
    } catch (error) {
      showError(error.message || 'Impossible de sauvegarder la composition.');
      updateSequencerButtons();
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

  recordButton?.addEventListener('click', startRecording);
  stopButton?.addEventListener('click', stopRecording);
  playSequenceButton?.addEventListener('click', playSequence);
  saveButton?.addEventListener('click', saveSequence);
  metronomeToggle?.addEventListener('click', toggleMetronome);
  bpmInput?.addEventListener('change', () => {
    const value = Number(bpmInput.value);
    if (!Number.isInteger(value) || value < 60 || value > 200) {
      const fallback = bpmInput.value.trim() === '' || !Number.isFinite(value) ? 120 : Math.min(200, Math.max(60, Math.round(value)));
      bpmInput.value = String(fallback);
      updateBpmRateLabel();
      showError('Le BPM doit être un nombre entier compris entre 60 et 200.');
      return;
    }
    showError('');
    updateBpmRateLabel();
    if (recording && sequencerStatus) {
      sequencerStatus.textContent = `Prise en cours à ${recordingBpm} BPM. Le nouveau tempo sera utilisé à la prochaine prise.`;
    }
  });

  updateBpmRateLabel();

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
    if (recording) stopRecording();
    stopMetronome();
    if (audioContext && audioContext.state !== 'closed') audioContext.close().catch(() => {});
  }, {once: true});

  refreshMetronomeToggle();
  setMode('waveform');
  refreshPads();
})();
