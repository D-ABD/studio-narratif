(() => {
  let submitJobHandler = null;

  // Capture les soumissions tôt, même si une autre initialisation de la page échoue.
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('#prepareForm, #audioOnlyForm')) return;

    event.preventDefault();
    if (submitJobHandler) {
      submitJobHandler(event, form);
      return;
    }

    const status = document.querySelector('#jobStatus');
    if (status) {
      status.className = 'job-status error';
      status.textContent = 'Le formulaire a été intercepté, mais son gestionnaire n’a pas pu être initialisé. Consulte la console.';
    }
    console.error('Le gestionnaire de soumission des formulaires de génération n’a pas été initialisé.');
  }, true);

  try {
  const csrfToken = () => document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const wordCount = value => (value.trim() ? value.trim().split(/\s+/).length : 0);

  const sceneFiles = document.querySelector('#sceneFiles');
  const importPreview = document.querySelector('#importPreview');
  const importPreviewHeading = document.querySelector('#importPreviewHeading');
  const importPreviewGrid = document.querySelector('#importPreviewGrid');
  let importPreviewUrls = [];
  sceneFiles?.addEventListener('change', () => {
    importPreviewUrls.forEach(url => URL.revokeObjectURL(url));
    importPreviewUrls = [];
    importPreviewGrid.replaceChildren();
    const files = [...(sceneFiles.files || [])];
    importPreview.hidden = files.length === 0;
    if (!files.length) return;
    importPreviewHeading.textContent = `Aperçu des ${files.length} fichier${files.length > 1 ? 's' : ''} sélectionné${files.length > 1 ? 's' : ''}`;
    files.forEach(file => {
      const card = document.createElement('article');
      card.className = 'import-preview-item';
      const preview = document.createElement('div');
      preview.className = 'import-preview-image';
      const extension = file.name.split('.').pop()?.toLowerCase();
      if (file.type.startsWith('image/') || ['jpg', 'jpeg', 'png'].includes(extension)) {
        const image = document.createElement('img');
        const objectUrl = URL.createObjectURL(file);
        importPreviewUrls.push(objectUrl);
        image.src = objectUrl;
        image.alt = `Aperçu de ${file.name}`;
        preview.append(image);
      } else if (file.type.startsWith('video/') || ['mp4', 'mov', 'm4v', 'webm'].includes(extension)) {
        const video = document.createElement('video');
        const objectUrl = URL.createObjectURL(file);
        importPreviewUrls.push(objectUrl);
        video.src = objectUrl;
        video.controls = true;
        video.muted = true;
        video.preload = 'metadata';
        video.setAttribute('aria-label', `Aperçu de ${file.name}`);
        preview.append(video);
      } else {
        preview.classList.add('pdf-preview');
        preview.textContent = 'PDF';
      }
      const name = document.createElement('b');
      name.textContent = file.name;
      const info = document.createElement('small');
      info.textContent = extension === 'pdf'
        ? 'Document PDF · chaque page deviendra une scène'
        : ['mp4', 'mov', 'm4v', 'webm'].includes(extension)
          ? `Clip vidéo · ${(file.size / (1024 * 1024)).toFixed(1)} Mo`
          : `${(file.size / (1024 * 1024)).toFixed(1)} Mo`;
      card.append(preview, name, info);
      importPreviewGrid.append(card);
    });
  });

  document.querySelectorAll('.segment-form textarea').forEach(textarea => {
    const card = textarea.closest('.segment-card');
    const update = () => {
      const count = wordCount(textarea.value);
      const wordCountElement = card.querySelector('.word-count');
      if (wordCountElement) wordCountElement.textContent = count;
      const durationEstimateElement = card.querySelector('.duration-estimate');
      if (durationEstimateElement) {
        durationEstimateElement.textContent = count ? `~${Math.max(1, Math.ceil(count / 2.35))}` : '0';
      }
      textarea.dataset.dirty = textarea.value !== textarea.defaultValue ? 'true' : 'false';
    };
    textarea.addEventListener('input', update);
    update();
  });
  const sceneSettingSelector = '[data-animation-setting]';
  const animationDefaults = {
    zoom: {enabled:false, level:'medium', direction:'in'},
    pan: {enabled:false, level:'medium', direction:'horizontal'},
    parallax: {enabled:false, level:'medium'},
    handheld: {enabled:false, level:'medium'},
    light_breathing: {enabled:false, level:'medium'},
    light_leak: {enabled:false, level:'medium'}
  };
  const syncAnimationSettings = form => {
    if (!form.querySelector(sceneSettingSelector)) return;
    const effects = JSON.parse(JSON.stringify(animationDefaults));
    form.querySelectorAll('[data-effect-name]').forEach(control => {
      const effect = effects[control.dataset.effectName];
      if (!effect) return;
      const field = control.dataset.effectField;
      effect[field] = field === 'enabled' ? control.checked : control.value;
    });
    const hidden = form.querySelector('[name="animation_settings"]');
    const profile = form.querySelector('[name="camera_profile"]')?.value || 'linear';
    if (hidden) hidden.value = JSON.stringify({version:1, camera_profile:profile, effects});
  };
  const controlValue = control => control.type === 'checkbox' ? String(control.checked) : control.value;
  document.querySelectorAll('.segment-form').forEach(form => {
    const controls = [...form.querySelectorAll(sceneSettingSelector)];
    controls.forEach(control => {
      control.dataset.initialValue = controlValue(control);
      control.addEventListener('change', () => {
        syncAnimationSettings(form);
        const changed = controls.some(input => controlValue(input) !== input.dataset.initialValue);
        form.dataset.motionDirty = changed ? 'true' : 'false';
      });
    });
    syncAnimationSettings(form);
    form.addEventListener('submit', () => syncAnimationSettings(form));
  });
  const videoOptionsForm = document.querySelector('#videoOptionsForm');
  videoOptionsForm?.querySelectorAll('select,input').forEach(input => {
    input.dataset.initialValue = input.type === 'checkbox' ? String(input.checked) : input.value;
    input.addEventListener('change', () => { videoOptionsForm.dataset.dirty = 'true'; });
  });

  const sampleInput = document.querySelector('#sampleInput');
  const samplePreview = document.querySelector('#samplePreview');
  const recordButton = document.querySelector('#recordButton');
  const recordStatus = document.querySelector('#recordStatus');
  let previewUrl = '';
  let recorder = null;
  let microphoneStream = null;
  let discardRecording = false;
  let recordedChunks = [];

  const stopMicrophone = () => {
    const stream = microphoneStream;
    microphoneStream = null;
    stream?.getTracks().forEach(track => {
      try {
        track.stop();
      } catch {
        // Continue stopping the remaining tracks if one track has already ended.
      }
    });
  };

  window.addEventListener('pagehide', () => {
    discardRecording = true;
    recordedChunks = [];
    stopMicrophone();
  });

  const showSample = file => {
    if (!file || !samplePreview) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(file);
    samplePreview.src = previewUrl;
    samplePreview.hidden = false;
    if (recordStatus) recordStatus.textContent = `${file.name || 'Enregistrement microphone'} · prêt à écouter`;
  };

  sampleInput?.addEventListener('change', () => showSample(sampleInput.files?.[0]));
  recordButton?.addEventListener('click', async () => {
    if (recorder?.state === 'recording') {
      try {
        recorder.stop();
        recordButton.textContent = '● Enregistrer avec le micro';
      } catch {
        discardRecording = true;
        recordedChunks = [];
        stopMicrophone();
        recorder = null;
        recordButton.textContent = '● Enregistrer avec le micro';
        recordStatus.textContent = 'Impossible d’arrêter proprement l’enregistrement.';
      }
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      recordStatus.textContent = 'L’enregistrement micro n’est pas disponible dans ce navigateur.';
      return;
    }
    try {
      microphoneStream = await navigator.mediaDevices.getUserMedia({audio: true});
      discardRecording = false;
      recordedChunks = [];
      recorder = new MediaRecorder(microphoneStream);
      recorder.addEventListener('dataavailable', event => {
        if (event.data.size) recordedChunks.push(event.data);
      });
      recorder.addEventListener('error', () => {
        discardRecording = true;
        recordedChunks = [];
        stopMicrophone();
        recordButton.textContent = '● Enregistrer avec le micro';
        recordStatus.textContent = 'Une erreur est survenue pendant l’enregistrement.';
      });
      recorder.addEventListener('stop', () => {
        try {
          if (discardRecording) return;
          const blob = new Blob(recordedChunks, {type: recorder.mimeType || 'audio/webm'});
          const extension = blob.type.includes('mp4') ? 'm4a' : 'webm';
          const file = new File([blob], `voix-enregistree.${extension}`, {type: blob.type});
          const transfer = new DataTransfer();
          transfer.items.add(file);
          sampleInput.files = transfer.files;
          showSample(file);
          recordStatus.textContent = 'Écoute l’enregistrement avant de sauvegarder.';
        } finally {
          stopMicrophone();
          recorder = null;
          discardRecording = false;
        }
      });
      recorder.start();
      recordButton.textContent = '■ Arrêter l’enregistrement';
      recordStatus.textContent = 'Enregistrement en cours…';
    } catch (error) {
      discardRecording = true;
      recordedChunks = [];
      stopMicrophone();
      recorder = null;
      recordButton.textContent = '● Enregistrer avec le micro';
      recordStatus.textContent = error.name === 'NotAllowedError'
        ? 'Autorise le microphone dans ton navigateur puis réessaie.'
        : 'Impossible de démarrer le microphone.';
    }
  });

  const list = document.querySelector('#segmentList');
  if (list) {
    let dragged = null;
    list.addEventListener('dragstart', event => {
      const card = event.target.closest('.segment-card');
      if (!card || event.target.closest('textarea,button,input,a,select,video')) {
        event.preventDefault();
        return;
      }
      dragged = card;
      event.dataTransfer.effectAllowed = 'move';
      card.classList.add('dragging');
    });
    list.addEventListener('dragover', event => {
      event.preventDefault();
      if (!dragged) return;
      const target = event.target.closest('.segment-card');
      if (!target || target === dragged) return;
      const box = target.getBoundingClientRect();
      list.insertBefore(dragged, event.clientY < box.top + box.height / 2 ? target : target.nextSibling);
    });
    list.addEventListener('dragend', async () => {
      if (!dragged) return;
      dragged.classList.remove('dragging');
      dragged = null;
      try {
        const response = await fetch(list.dataset.reorderUrl, {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
          body: JSON.stringify({segment_ids: [...list.querySelectorAll('.segment-card')].map(card => Number(card.dataset.id))}),
        });
        if (!response.ok) throw new Error();
        window.location.reload();
      } catch {
        window.location.reload();
      }
    });
  }

  const projectVoice = document.querySelector('#projectVoice');
  const prepareForm = document.querySelector('#prepareForm');
  const audioOnlyForm = document.querySelector('#audioOnlyForm');
  const assembleForm = document.querySelector('#assembleForm');
  const voiceIdInputs = [prepareForm, audioOnlyForm]
    .filter(Boolean)
    .map(form => form.querySelector('input[name="voice_id"]'))
    .filter(Boolean);
  projectVoice?.addEventListener('change', () => {
    voiceIdInputs.forEach(input => { input.value = projectVoice.value; });
  });

  if (!prepareForm && !audioOnlyForm && !assembleForm) return;
  const jobButtons = [prepareForm, audioOnlyForm, assembleForm]
    .filter(Boolean)
    .map(form => form.querySelector('button[type="submit"]'))
    .filter(Boolean);
  const setJobButtonsDisabled = disabled => {
    jobButtons.forEach(button => { button.disabled = disabled; });
  };
  const status = document.querySelector('#jobStatus');
  const cancelJobForm = document.querySelector('#cancelJobForm');
  const cancelJobButton = document.querySelector('#cancelJobButton');
  const cancelJobContainer = document.querySelector('#cancelJobContainer');
  const progress = document.querySelector('#jobProgress');
  const details = document.querySelector('#progressDetails');
  const currentSegment = document.querySelector('#currentSegment');
  const elapsedTime = document.querySelector('#elapsedTime');
  const projectId = document.body.dataset.projectId;
  let polling = false;

  const formatElapsed = seconds => {
    const value = Math.max(0, Number(seconds) || 0);
    return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')} écoulées`;
  };
  const showFullExport = (audioUrl, subtitlesUrl) => {
    const box = document.querySelector('#fullExport');
    const audio = document.querySelector('#fullAudio');
    const audioDownload = document.querySelector('#fullAudioDownload');
    const srtDownload = document.querySelector('#subtitleDownload');
    if (!audioUrl || !box || !audio) return;
    audio.src = audioUrl;
    audioDownload.href = audioUrl;
    srtDownload.href = subtitlesUrl;
    box.hidden = false;
  };
  const showVideoExport = videoUrl => {
    if (!videoUrl) return;
    const box = document.querySelector('#videoExport');
    const video = document.querySelector('#projectVideo');
    const download = document.querySelector('#videoDownload');
    if (!box || !video || !download) return;
    video.src = videoUrl;
    download.href = videoUrl;
    box.hidden = false;
  };

  const syncCancelControl = data => {
    if (!cancelJobForm || !cancelJobContainer || !cancelJobButton) return;
    const active = ['queued', 'processing'].includes(data.status);
    cancelJobContainer.hidden = !active;
    cancelJobButton.disabled = !active || Boolean(data.cancel_requested);
    cancelJobButton.textContent = data.cancel_requested ? 'Annulation demandée…' : 'Annuler le traitement';
  };

  const poll = async () => {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch(`/projects/${projectId}/status/`, {cache: 'no-store'});
      if (!response.ok) throw new Error('État indisponible');
      const data = await response.json();
      syncCancelControl(data);
      status.textContent = data.error || data.message || 'Traitement en cours…';
      progress.value = Math.max(0, Math.min(100, Number(data.progress) || 0));
      progress.hidden = data.status !== 'processing';
      details.hidden = data.status !== 'processing';
      currentSegment.textContent = `Segment ${data.current_segment}/${data.total_segments}`;
      elapsedTime.textContent = formatElapsed(data.elapsed_seconds);
      if (data.status === 'queued') {
        status.className = 'job-status';
        status.textContent = data.cancel_requested
          ? data.message || 'Annulation demandée. Le worker s’arrêtera dès que possible…'
          : data.queue_position
            ? `En attente · position ${data.queue_position} sur ${data.queue_count}. Le worker traitera les projets dans l’ordre.`
            : 'Ajouté à la file. En attente du worker…';
        setJobButtonsDisabled(true);
        progress.hidden = true;
        details.hidden = true;
        window.setTimeout(() => { polling = false; poll(); }, 2500);
        return;
      }
      if (data.status === 'ready') {
        status.className = 'job-status success';
        status.textContent = data.message || 'Narration prête à écouter et à télécharger.';
        showFullExport(data.audio_url, data.subtitles_url);
        showVideoExport(data.video_url);
        setJobButtonsDisabled(false);
        return;
      }
      if (data.status === 'error') {
        status.className = 'job-status error';
        setJobButtonsDisabled(false);
        return;
      }
      if (data.status === 'processing') {
        setJobButtonsDisabled(true);
        window.setTimeout(() => { polling = false; poll(); }, 1300);
        return;
      }
      setJobButtonsDisabled(false);
    } catch {
      status.className = 'job-status error';
      status.textContent = 'Impossible de lire l’état du traitement. Recharge le projet pour vérifier.';
      setJobButtonsDisabled(false);
    } finally {
      polling = false;
    }
  };

  const saveDirtySegments = async () => {
    for (const form of document.querySelectorAll('.segment-form')) {
      const textarea = form.querySelector('textarea');
      if (textarea?.dataset.dirty !== 'true' && form.dataset.motionDirty !== 'true') continue;
      syncAnimationSettings(form);
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      if (!response.ok) throw new Error('Impossible d’enregistrer un segment modifié.');
      if (textarea) {
        textarea.defaultValue = textarea.value;
        textarea.dataset.dirty = 'false';
      }
      form.dataset.motionDirty = 'false';
      form.querySelectorAll(sceneSettingSelector).forEach(control => {
        control.dataset.initialValue = control.type === 'checkbox' ? String(control.checked) : control.value;
      });
    }
    if (videoOptionsForm?.dataset.dirty === 'true') {
      const response = await fetch(videoOptionsForm.action, {method: 'POST', body: new FormData(videoOptionsForm)});
      if (!response.ok) throw new Error('Impossible d’enregistrer les réglages vidéo.');
      videoOptionsForm.dataset.dirty = 'false';
    }
  };

  cancelJobForm?.addEventListener('submit', async event => {
    event.preventDefault();
    cancelJobButton.disabled = true;
    cancelJobButton.textContent = 'Annulation demandée…';
    try {
      const response = await fetch(cancelJobForm.action, {
        method: 'POST', body: new FormData(cancelJobForm),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Impossible d’annuler le traitement.');
      syncCancelControl(data);
      status.className = 'job-status';
      status.textContent = data.message || 'Annulation demandée…';
      polling = false;
      poll();
    } catch (error) {
      cancelJobButton.disabled = false;
      cancelJobButton.textContent = 'Annuler le traitement';
      status.className = 'job-status error';
      status.textContent = error.message;
    }
  });

  const submitJob = async (event, form) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    if (!button) return;
    const isAssembly = form === assembleForm;
    const isAudioOnly = form === audioOnlyForm;
    const voiceIdInput = form.querySelector('input[name="voice_id"]');
    if (!isAssembly && !voiceIdInput?.value) {
      status.className = 'job-status error';
      status.textContent = 'Choisis d’abord une voix autorisée.';
      return;
    }
    button.disabled = true;
    setJobButtonsDisabled(true);
    status.className = 'job-status';
    status.textContent = isAssembly
      ? 'Enregistrement des réglages…'
      : isAudioOnly
        ? 'Pré-génération audio XTTS et WhisperX…'
        : 'Enregistrement des textes…';
    try {
      await saveDirtySegments();
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Le traitement n’a pas pu démarrer.');
      progress.hidden = false;
      details.hidden = false;
      status.textContent = isAudioOnly
        ? 'Pré-génération audio ajoutée à la file…'
        : 'Ajout du projet à la file…';
      polling = false;
      poll();
    } catch (error) {
      status.className = 'job-status error';
      status.textContent = error.message;
      setJobButtonsDisabled(false);
    }
  };
  submitJobHandler = submitJob;

  if (['processing', 'queued'].includes(document.body.dataset.projectStatus)) {
    setJobButtonsDisabled(true);
    progress.hidden = document.body.dataset.projectStatus !== 'processing';
    details.hidden = document.body.dataset.projectStatus !== 'processing';
    poll();
  }
  } catch (error) {
    console.error('Erreur pendant l’initialisation de app.js', error);
  }
})();
