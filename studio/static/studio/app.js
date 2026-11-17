(() => {
  let submitJobHandler = null;

  // Capture les soumissions tôt, même si une autre initialisation de la page échoue.
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('#prepareForm, #audioOnlyForm, #assemblyRenderForm')) return;

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

  document.addEventListener('click', event => {
    const target = event.target instanceof Element ? event.target : null;
    const trigger = target?.closest('[data-list-accordion-trigger]');
    if (!trigger) return;
    const panelId = trigger.getAttribute('aria-controls');
    const panel = panelId ? document.getElementById(panelId) : null;
    if (!panel) return;
    const expanded = trigger.getAttribute('aria-expanded') === 'true';
    trigger.setAttribute('aria-expanded', String(!expanded));
    panel.hidden = expanded;
  });

  const projectDeleteDialog = document.querySelector('#projectDeleteDialog');
  const projectDeleteForm = document.querySelector('#sidebarDeleteProjectForm');
  const projectDeleteName = document.querySelector('#projectDeleteName');
  const projectDeleteReturn = projectDeleteForm?.querySelector('[data-delete-return]');
  const closeProjectMenus = exceptRow => {
    document.querySelectorAll('[data-sidebar-project]').forEach(row => {
      if (row === exceptRow) return;
      const menu = row.querySelector('[data-project-dropdown]');
      const toggle = row.querySelector('[data-project-menu-toggle]');
      if (menu) menu.hidden = true;
      toggle?.setAttribute('aria-expanded', 'false');
    });
  };
  const cancelInlineRename = row => {
    if (!row) return;
    row.classList.remove('is-renaming');
    const form = row.querySelector('[data-project-rename-form]');
    if (form) form.hidden = true;
  };

  document.addEventListener('click', event => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;

    const menuToggle = target.closest('[data-project-menu-toggle]');
    if (menuToggle) {
      const row = menuToggle.closest('[data-sidebar-project]');
      const menu = row?.querySelector('[data-project-dropdown]');
      if (!row || !menu) return;
      const shouldOpen = menu.hidden;
      closeProjectMenus(row);
      menu.hidden = !shouldOpen;
      menuToggle.setAttribute('aria-expanded', String(shouldOpen));
      return;
    }

    const renameStart = target.closest('[data-project-rename-start]');
    if (renameStart) {
      const row = renameStart.closest('[data-sidebar-project]');
      const menu = row?.querySelector('[data-project-dropdown]');
      const toggle = row?.querySelector('[data-project-menu-toggle]');
      const form = row?.querySelector('[data-project-rename-form]');
      const input = form?.querySelector('[data-project-rename-input]');
      if (!row || !form || !input) return;
      if (menu) menu.hidden = true;
      toggle?.setAttribute('aria-expanded', 'false');
      row.classList.add('is-renaming');
      form.hidden = false;
      input.value = row.querySelector('[data-project-title]')?.textContent?.trim() || input.value;
      input.focus();
      input.select();
      return;
    }

    const renameCancel = target.closest('[data-project-rename-cancel]');
    if (renameCancel) {
      cancelInlineRename(renameCancel.closest('[data-sidebar-project]'));
      return;
    }

    const deleteStart = target.closest('[data-project-delete-start]');
    if (deleteStart && projectDeleteDialog && projectDeleteForm) {
      closeProjectMenus();
      projectDeleteForm.action = deleteStart.dataset.deleteUrl || '';
      if (projectDeleteReturn) projectDeleteReturn.value = deleteStart.dataset.returnUrl || window.location.pathname;
      if (projectDeleteName) projectDeleteName.textContent = deleteStart.dataset.projectTitle || '';
      if (typeof projectDeleteDialog.showModal === 'function') projectDeleteDialog.showModal();
      else projectDeleteDialog.setAttribute('open', '');
      return;
    }

    if (target.closest('[data-project-delete-cancel]')) {
      if (projectDeleteDialog?.open) projectDeleteDialog.close();
      else projectDeleteDialog?.removeAttribute('open');
      return;
    }

    if (target === projectDeleteDialog) {
      projectDeleteDialog.close();
      return;
    }

    if (!target.closest('[data-project-dropdown], [data-project-menu-toggle], [data-project-rename-form]')) {
      closeProjectMenus();
    }
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      const activeRename = document.activeElement?.closest?.('[data-project-rename-form]');
      if (activeRename) {
        event.preventDefault();
        cancelInlineRename(activeRename.closest('[data-sidebar-project]'));
        return;
      }
      closeProjectMenus();
    }
  });

  const assemblyAudioSelect = document.querySelector('#projectAudioAsset');
  const assemblyAudioPreview = document.querySelector('#assemblyAudioPreview');
  const assemblyAudioPreviewMessage = document.querySelector('#assemblyAudioPreviewMessage');
  if (assemblyAudioSelect && assemblyAudioPreview && assemblyAudioPreviewMessage) {
    const clearAssemblyAudioPreview = () => {
      assemblyAudioPreview.pause();
      assemblyAudioPreview.removeAttribute('src');
      assemblyAudioPreview.load();
      assemblyAudioPreview.hidden = true;
    };
    const setAssemblyAudioMessage = message => {
      assemblyAudioPreviewMessage.textContent = message;
      assemblyAudioPreviewMessage.hidden = false;
    };
    const updateAssemblyAudioPreview = () => {
      clearAssemblyAudioPreview();
      const selectedOption = assemblyAudioSelect.selectedOptions[0];
      const audioUrl = selectedOption?.dataset.audioUrl?.trim();
      if (!selectedOption?.value) {
        setAssemblyAudioMessage('Sélectionne un audio');
        return;
      }
      if (!audioUrl) {
        setAssemblyAudioMessage('Fichier audio introuvable');
        return;
      }
      assemblyAudioPreview.src = audioUrl;
      assemblyAudioPreview.hidden = false;
      assemblyAudioPreviewMessage.hidden = true;
    };
    assemblyAudioSelect.addEventListener('change', updateAssemblyAudioPreview);
    assemblyAudioPreview.addEventListener('error', () => {
      if (!assemblyAudioPreview.getAttribute('src')) return;
      clearAssemblyAudioPreview();
      setAssemblyAudioMessage('Fichier audio introuvable');
    });
    updateAssemblyAudioPreview();
  }

  const assemblyMusicSelect = document.querySelector('#projectBackgroundTrack');
  const assemblyMusicPreview = document.querySelector('#assemblyMusicPreview');
  const assemblyMusicPreviewMessage = document.querySelector('#assemblyMusicPreviewMessage');
  if (assemblyMusicSelect && assemblyMusicPreview && assemblyMusicPreviewMessage) {
    const clearAssemblyMusicPreview = () => {
      assemblyMusicPreview.pause();
      assemblyMusicPreview.removeAttribute('src');
      assemblyMusicPreview.load();
      assemblyMusicPreview.hidden = true;
    };
    const setAssemblyMusicMessage = message => {
      assemblyMusicPreviewMessage.textContent = message;
      assemblyMusicPreviewMessage.hidden = false;
    };
    const updateAssemblyMusicPreview = () => {
      clearAssemblyMusicPreview();
      const selectedOption = assemblyMusicSelect.selectedOptions[0];
      const musicUrl = selectedOption?.dataset.musicUrl?.trim();
      if (!selectedOption?.value) {
        setAssemblyMusicMessage('Sélectionne une musique');
        return;
      }
      if (!musicUrl) {
        setAssemblyMusicMessage('Fichier audio introuvable');
        return;
      }
      assemblyMusicPreview.src = musicUrl;
      assemblyMusicPreview.hidden = false;
      assemblyMusicPreviewMessage.hidden = true;
    };
    assemblyMusicSelect.addEventListener('change', updateAssemblyMusicPreview);
    assemblyMusicPreview.addEventListener('error', () => {
      if (!assemblyMusicPreview.getAttribute('src')) return;
      clearAssemblyMusicPreview();
      setAssemblyMusicMessage('Fichier audio introuvable');
    });
    updateAssemblyMusicPreview();
  }

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

  document.querySelectorAll('[data-assembly-visual-form]').forEach(form => {
    const kind = form.querySelector('[data-visual-type]');
    const file = form.querySelector('[data-visual-file]');
    const preview = form.closest('.scene-card')?.querySelector('[data-visual-preview]');
    const savedPreview = preview?.innerHTML || '';
    let previewUrl = null;
    const allowedTypes = {
      image: 'image/jpeg,image/png,.jpg,.jpeg,.png',
      video: 'video/mp4,video/quicktime,.mp4,.mov'
    };
    const updateVisualInput = clearSelection => {
      if (clearSelection) file.value = '';
      file.accept = allowedTypes[kind.value] || allowedTypes.image;
    };
    updateVisualInput(false);
    kind.addEventListener('change', () => {
      updateVisualInput(true);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = null;
      if (preview) preview.innerHTML = savedPreview;
    });
    file.addEventListener('change', () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = null;
      const selected = file.files?.[0];
      if (!preview || !selected) {
        if (preview) preview.innerHTML = savedPreview;
        return;
      }
      previewUrl = URL.createObjectURL(selected);
      if (kind.value === 'video') {
        const video = document.createElement('video');
        video.src = previewUrl;
        video.controls = true;
        video.muted = true;
        video.playsInline = true;
        video.preload = 'metadata';
        preview.replaceChildren(video);
      } else {
        const image = document.createElement('img');
        image.src = previewUrl;
        image.alt = `Aperçu de ${selected.name}`;
        preview.replaceChildren(image);
      }
    });
    window.addEventListener('pagehide', () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    }, {once:true});
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
  const assemblyRenderForm = document.querySelector('#assemblyRenderForm');
  const voiceIdInputs = [prepareForm, audioOnlyForm]
    .filter(Boolean)
    .map(form => form.querySelector('input[name="voice_id"]'))
    .filter(Boolean);
  projectVoice?.addEventListener('change', () => {
    voiceIdInputs.forEach(input => { input.value = projectVoice.value; });
  });

  if (!prepareForm && !audioOnlyForm && !assembleForm && !assemblyRenderForm) return;
  const jobButtons = [prepareForm, audioOnlyForm, assembleForm, assemblyRenderForm]
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
      if (response.status === 401) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.assign(`/login/?next=${next}`);
        return;
      }
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
    const isLibraryAssembly = form === assemblyRenderForm;
    const isAudioOnly = form === audioOnlyForm;
    const voiceIdInput = form.querySelector('input[name="voice_id"]');
    if (!isAssembly && !isLibraryAssembly && !isAudioOnly && !voiceIdInput?.value) {
      status.className = 'job-status error';
      status.textContent = 'Choisis d’abord une voix autorisée.';
      return;
    }
    button.disabled = true;
    setJobButtonsDisabled(true);
    status.className = 'job-status';
    status.textContent = isLibraryAssembly
      ? 'Validation de l’assemblage…'
      : isAssembly
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
        : isLibraryAssembly
          ? 'Assemblage ajouté à la file…'
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

(() => {
  const editor = document.querySelector('[data-audio-studio-editor]');
  if (!editor) return;

  try {
    const operationUrl = editor.dataset.operationsUrl;
    const previewUrl = editor.dataset.previewUrl;
    const csrf = document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
    const operationList = editor.querySelector('[data-audio-operation-list]');
    const operationData = document.getElementById('audioStudioOperationsData');
    const previewButton = editor.querySelector('[data-audio-preview]');
    const previewPlayer = editor.querySelector('#audioStudioPreviewPlayer');
    const waveformShell = editor.querySelector('[data-audio-waveform-shell]');
    const waveform = editor.querySelector('[data-audio-waveform]');
    const waveformLoading = editor.querySelector('[data-waveform-loading]');
    const waveformError = editor.querySelector('[data-waveform-error]');
    const waveformFallback = editor.querySelector('[data-waveform-fallback]');
    const waveformToolbar = editor.querySelector('[data-waveform-toolbar]');
    const waveformTime = editor.querySelector('[data-waveform-time]');
    const waveformPlay = editor.querySelector('[data-waveform-play]');
    const fadeHandles = [...editor.querySelectorAll('[data-wave-fade]')];
    const previewPlaceholder = editor.querySelector('[data-audio-preview-placeholder]');
    const previewDuration = editor.querySelector('[data-audio-preview-duration]');
    const busyMessage = editor.querySelector('[data-audio-preview-busy]');
    const errorMessage = editor.querySelector('[data-audio-editor-error]');
    const warningMessage = editor.querySelector('[data-audio-preview-warning]');
    const saveButton = editor.querySelector('[data-audio-save]');
    const dialog = document.querySelector('[data-audio-operation-dialog]');
    const dialogForm = dialog?.querySelector('[data-audio-operation-form]');
    const dialogTitle = dialog?.querySelector('[data-operation-dialog-title]');
    const effectSelect = dialog?.querySelector('[name="effect_name"]');
    const directionField = dialog?.querySelector('[data-audio-direction-field]');
    const updateDirectionField = () => {
      if (directionField) directionField.hidden = !['tempo', 'pitch'].includes(effectSelect?.value);
    };
    effectSelect?.addEventListener('change', updateDirectionField);
    let operations = [];
    try { operations = JSON.parse(operationData?.textContent || '[]'); } catch (_) {}
    let working = false;
    let hasPreview = editor.dataset.hasPreview === 'true';
    let previewValid = editor.dataset.previewValid === 'true';

    const labelFor = operation => {
      const params = operation.params || {};
      if (operation.type === 'trim') return `Trim · ${params.start}s → ${params.end}s`;
      if (operation.type === 'fade') return `Fade · entrée ${params.in}s / sortie ${params.out}s`;
      if (operation.type === 'volume') return `Volume · ${params.percent}%`;
      if (operation.type === 'effect') {
        const select = dialog?.querySelector('[name="effect_name"]');
        const label = select?.querySelector(`option[value="${CSS.escape(params.name || '')}"]`)?.textContent || params.name;
        return ['tempo', 'pitch'].includes(params.name) ? `${label} · ${params.direction}` : label;
      }
      if (operation.type === 'preset') {
        const select = dialog?.querySelector('[name="preset_name"]');
        return select?.querySelector(`option[value="${CSS.escape(params.name || '')}"]`)?.textContent || params.name;
      }
      return 'Opération audio';
    };

    const updateFreshness = () => {
      editor.dataset.hasPreview = String(hasPreview);
      editor.dataset.previewValid = String(previewValid);
      if (saveButton) saveButton.disabled = working || !previewValid;
      if (warningMessage) warningMessage.hidden = !hasPreview || previewValid;
    };

    const setWorking = (value, previewing = false) => {
      working = value;
      if (waveformShell) waveformShell.dataset.waveBusy = String(value);
      editor.querySelectorAll('[data-operation-control]').forEach(button => { button.disabled = value; });
      if (previewButton) {
        previewButton.disabled = value;
        previewButton.innerHTML = value && previewing
          ? '<span class="audio-editor-spinner" aria-hidden="true"></span>Traitement en cours… (peut prendre jusqu’à 45 s pour un fichier long)'
          : 'Écouter l’aperçu';
      }
      editor.querySelectorAll('[data-remove-operation]').forEach(button => { button.disabled = value; });
      if (busyMessage) busyMessage.hidden = !value || !previewing;
      updateFreshness();
    };

    const renderOperations = () => {
      if (!operationList) return;
      operationList.replaceChildren();
      operations.forEach((operation, index) => {
        const chip = document.createElement('span');
        chip.className = 'audio-operation-chip';
        const label = document.createElement('span');
        label.textContent = labelFor(operation);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = '×';
        remove.setAttribute('aria-label', `Supprimer ${label.textContent}`);
        remove.dataset.removeOperation = String(index);
        remove.disabled = working;
        chip.append(label, remove);
        operationList.append(chip);
      });
      updateFreshness();
    };

    let waveSurfer = null;
    let regions = null;
    let trimRegion = null;
    let waveDuration = 0;
    let visualFadeValues = null;
    let pendingTrim = null;
    let pendingRegionFlush = false;
    const operationOf = type => operations.find(operation => operation.type === type);
    const trimBounds = () => {
      const params = operationOf('trim')?.params || {};
      return {
        start: Math.max(0, Number(params.start) || 0),
        end: Math.min(waveDuration, Number(params.end) || waveDuration),
      };
    };
    const fadeValues = () => {
      if (visualFadeValues) return {...visualFadeValues};
      const params = operationOf('fade')?.params || {};
      return {in: Math.max(0, Number(params.in) || 0), out: Math.max(0, Number(params.out) || 0)};
    };
    const formatWaveTime = seconds => {
      const value = Math.max(0, Math.floor(seconds || 0));
      return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
    };
    const drawFadeHandles = () => {
      if (!waveDuration || !waveform) return;
      const bounds = trimBounds();
      const fades = fadeValues();
      const positions = {
        in: Math.min(bounds.end, bounds.start + fades.in),
        out: Math.max(bounds.start, bounds.end - fades.out),
      };
      fadeHandles.forEach(handle => {
        const kind = handle.dataset.waveFade;
        const value = kind === 'in' ? Math.max(0, positions.in - bounds.start) : Math.max(0, bounds.end - positions.out);
        const max = Math.max(0, bounds.end - bounds.start);
        handle.style.left = `${Math.min(100, Math.max(0, positions[kind] / waveDuration * 100))}%`;
        handle.setAttribute('aria-valuemax', max.toFixed(2));
        handle.setAttribute('aria-valuenow', value.toFixed(2));
        handle.title = `${kind === 'in' ? 'Fondu d’entrée' : 'Fondu de sortie'} · ${value.toFixed(2)} s`;
      });
      editor.querySelectorAll('[data-fade-band]').forEach(band => {
        const kind = band.dataset.fadeBand;
        const left = kind === 'in' ? bounds.start : positions.out;
        const width = kind === 'in' ? fades.in : fades.out;
        band.style.left = `${Math.min(100, Math.max(0, left / waveDuration * 100))}%`;
        band.style.width = `${Math.min(100, Math.max(0, width / waveDuration * 100))}%`;
      });
      if (waveformTime) waveformTime.textContent = `${formatWaveTime(bounds.start)}–${formatWaveTime(bounds.end)} / ${formatWaveTime(waveDuration)} · fondus ${fades.in.toFixed(1)} s / ${fades.out.toFixed(1)} s`;
    };
    const syncRegionFromOperations = () => {
      if (!regions || !waveDuration) return;
      const bounds = trimBounds();
      if (trimRegion && !trimRegion.isRemoved) {
        trimRegion.setOptions({start: bounds.start, end: Math.max(bounds.start + 0.01, bounds.end)});
      } else {
        trimRegion = regions.addRegion({
          start: bounds.start,
          end: Math.max(bounds.start + 0.01, bounds.end),
          drag: false,
          resize: true,
          content: 'TRIM',
          color: 'rgba(245, 158, 11, 0.14)',
          minLength: 0.02,
          id: 'audio-trim-region',
        });
      }
      drawFadeHandles();
    };
    const restoreWaveformState = () => {
      syncRegionFromOperations();
      drawFadeHandles();
    };

    const postJson = async (url, payload) => {
      const response = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'La demande audio a échoué.');
      return data;
    };

    const applyOperationState = data => {
      operations = data.operations || [];
      hasPreview = Boolean(data.has_preview);
      previewValid = Boolean(data.preview_valid);
      if (errorMessage) errorMessage.hidden = true;
      renderOperations();
    };

    const showError = error => {
      if (!errorMessage) return;
      errorMessage.textContent = error?.message || 'Le traitement audio a échoué.';
      errorMessage.hidden = false;
    };

    const replaceOperation = async (type, params) => {
      if (working) return false;
      const previous = operations.map(operation => ({type: operation.type, params: {...(operation.params || {})}}));
      const oldIndex = previous.findIndex(operation => operation.type === type);
      const oldOperation = oldIndex >= 0 ? previous[oldIndex] : null;
      let removed = false;
      visualFadeValues = null;
      restoreWaveformState();
      setWorking(true);
      try {
        if (oldIndex >= 0) {
          await postJson(operationUrl, {action: 'remove', index: oldIndex});
          removed = true;
        }
        const data = await postJson(operationUrl, {action: 'add', operation: {type, params}});
        visualFadeValues = null;
        applyOperationState(data);
        restoreWaveformState();
        return true;
      } catch (error) {
        if (removed && oldOperation) {
          try { await postJson(operationUrl, {action: 'add', operation: oldOperation}); } catch (_) {}
        }
        operations = previous;
        visualFadeValues = null;
        if (removed) previewValid = false;
        renderOperations();
        restoreWaveformState();
        showError(new Error('Erreur de synchronisation, veuillez réessayer'));
        window.alert('Erreur de synchronisation, veuillez réessayer');
        return false;
      } finally {
        setWorking(false);
        renderOperations();
      }
    };

    const syncTrim = async (start, end) => {
      const safeStart = Math.max(0, Math.min(start, waveDuration));
      const safeEnd = Math.max(safeStart + 0.02, Math.min(end, waveDuration));
      pendingTrim = null;
      await replaceOperation('trim', {start: Number(safeStart.toFixed(2)), end: Number(safeEnd.toFixed(2))});
    };
    const flushPendingTrim = () => {
      if (!pendingRegionFlush || !pendingTrim || working) return;
      pendingRegionFlush = false;
      const bounds = pendingTrim;
      syncTrim(bounds.start, bounds.end);
    };

    const initializeWaveform = () => {
      if (!waveform || !window.WaveSurfer?.create || !window.WaveSurfer?.Regions?.create) {
        if (waveformLoading) waveformLoading.hidden = true;
        if (waveform) waveform.hidden = true;
        if (waveformError) waveformError.hidden = false;
        if (waveformFallback) waveformFallback.hidden = false;
        return;
      }
      waveform.hidden = false;
      try {
        regions = window.WaveSurfer.Regions.create();
        waveSurfer = window.WaveSurfer.create({
          container: waveform,
          url: editor.dataset.sourceUrl,
          height: 116,
          normalize: true,
          waveColor: '#748196',
          progressColor: '#F59E0B',
          cursorColor: '#FFE0A0',
          cursorWidth: 2,
          barWidth: 2,
          barGap: 1,
          barRadius: 2,
          plugins: [regions],
        });
        waveSurfer.on('ready', duration => {
          waveDuration = Number(duration) || waveSurfer.getDuration();
          if (waveformLoading) waveformLoading.hidden = true;
          if (waveformToolbar) waveformToolbar.hidden = false;
          if (waveSurfer.getMediaElement()) waveSurfer.getMediaElement().setAttribute('aria-label', 'Lecture du fichier source');
          syncRegionFromOperations();
          drawFadeHandles();
        });
        waveSurfer.on('timeupdate', time => {
          if (waveformTime && waveDuration) {
            const bounds = trimBounds();
            const fades = fadeValues();
            waveformTime.textContent = `${formatWaveTime(time)} / ${formatWaveTime(waveDuration)} · trim ${formatWaveTime(bounds.start)}–${formatWaveTime(bounds.end)} · fades ${fades.in.toFixed(1)} s / ${fades.out.toFixed(1)} s`;
          }
        });
        waveSurfer.on('error', error => {
          console.warn('WaveSurfer n’a pas pu décoder le fichier source.', error);
          if (waveformLoading) waveformLoading.hidden = true;
          if (waveform) waveform.hidden = true;
          if (waveformToolbar) waveformToolbar.hidden = true;
          if (waveformError) waveformError.hidden = false;
          if (waveformFallback) waveformFallback.hidden = false;
        });
        regions.on('region-updated', region => {
          if (region !== trimRegion || working) return;
          pendingTrim = {start: region.start, end: region.end};
          pendingRegionFlush = true;
          flushPendingTrim();
        });
        regions.on('region-out', region => {
          if (region === trimRegion) flushPendingTrim();
        });
      } catch (error) {
        console.warn('Initialisation WaveSurfer impossible.', error);
        if (waveformLoading) waveformLoading.hidden = true;
        if (waveform) waveform.hidden = true;
        if (waveformError) waveformError.hidden = false;
        if (waveformFallback) waveformFallback.hidden = false;
      }
    };

    const updateFadeFromPointer = (handle, clientX) => {
      const rect = waveform?.getBoundingClientRect();
      if (!rect || !waveDuration) return null;
      const time = Math.max(0, Math.min(waveDuration, (clientX - rect.left) / rect.width * waveDuration));
      const bounds = trimBounds();
      const fades = fadeValues();
      if (handle.dataset.waveFade === 'in') fades.in = Math.max(0, Math.min(bounds.end - bounds.start, time - bounds.start));
      else fades.out = Math.max(0, Math.min(bounds.end - bounds.start, bounds.end - time));
      visualFadeValues = fades;
      drawFadeHandles();
      return fades;
    };
    fadeHandles.forEach(handle => {
      handle.addEventListener('pointerdown', event => {
        if (working || !waveDuration) return;
        event.preventDefault();
        handle.setPointerCapture(event.pointerId);
        const kind = handle.dataset.waveFade;
        const initial = fadeValues();
        const move = moveEvent => updateFadeFromPointer(handle, moveEvent.clientX);
        const finish = async finishEvent => {
          handle.removeEventListener('pointermove', move);
          handle.removeEventListener('pointerup', finish);
          handle.removeEventListener('pointercancel', finish);
          const values = updateFadeFromPointer(handle, finishEvent.clientX) || initial;
          const result = await replaceOperation('fade', {in: Number(values.in.toFixed(2)), out: Number(values.out.toFixed(2))});
          if (!result) restoreWaveformState();
        };
        handle.addEventListener('pointermove', move);
        handle.addEventListener('pointerup', finish, {once: true});
        handle.addEventListener('pointercancel', finish, {once: true});
        handle.dataset.dragKind = kind;
      });
      handle.addEventListener('keydown', async event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key) || working || !waveDuration) return;
        event.preventDefault();
        const values = fadeValues();
        const delta = (event.key === 'ArrowRight' ? 0.1 : -0.1);
        const bounds = trimBounds();
        if (handle.dataset.waveFade === 'in') values.in = Math.max(0, Math.min(bounds.end - bounds.start, values.in + delta));
        else values.out = Math.max(0, Math.min(bounds.end - bounds.start, values.out + delta));
        visualFadeValues = values;
        drawFadeHandles();
        await replaceOperation('fade', {in: Number(values.in.toFixed(2)), out: Number(values.out.toFixed(2))});
      });
    });
    waveformPlay?.addEventListener('click', () => waveSurfer?.playPause());
    initializeWaveform();

    const mutate = async payload => {
      if (working) return;
      setWorking(true);
      try { applyOperationState(await postJson(operationUrl, payload)); }
      catch (error) { showError(error); }
      finally { setWorking(false); renderOperations(); }
    };

    document.querySelectorAll('[data-operation-open]').forEach(button => {
      button.addEventListener('click', () => {
        if (!dialog || !dialogForm || working) return;
        const type = button.dataset.operationOpen;
        dialogForm.dataset.operationType = type;
        if (dialogTitle) dialogTitle.textContent = `Ajouter ${type === 'preset' ? 'un preset' : type}`;
        dialog.querySelectorAll('[data-operation-fields]').forEach(section => {
          section.hidden = section.dataset.operationFields !== type;
        });
        updateDirectionField();
        dialog.showModal?.();
      });
    });

    dialog?.querySelector('[data-operation-dialog-close]')?.addEventListener('click', () => dialog.close());
    dialogForm?.addEventListener('submit', async event => {
      event.preventDefault();
      const values = new FormData(dialogForm);
      const type = dialogForm.dataset.operationType;
      let params = {};
      if (type === 'trim') params = {start: Number(values.get('trim_start')), end: Number(values.get('trim_end'))};
      if (type === 'fade') params = {in: Number(values.get('fade_in')), out: Number(values.get('fade_out'))};
      if (type === 'volume') params = {percent: Number(values.get('volume_percent'))};
      if (type === 'effect') {
        const name = values.get('effect_name');
        params = ['tempo', 'pitch'].includes(name) ? {name, direction: values.get('effect_direction')} : {name};
      }
      if (type === 'preset') params = {name: values.get('preset_name')};
      dialog.close();
      if (type === 'trim' || type === 'fade') await replaceOperation(type, params);
      else await mutate({action: 'add', operation: {type, params}});
    });

    operationList?.addEventListener('click', event => {
      const remove = event.target.closest('[data-remove-operation]');
      if (remove) mutate({action: 'remove', index: Number(remove.dataset.removeOperation)});
    });
    editor.querySelectorAll('[data-operation-action]').forEach(button => {
      button.addEventListener('click', () => mutate({action: button.dataset.operationAction}));
    });

    previewButton?.addEventListener('click', async () => {
      if (working) return;
      if (errorMessage) errorMessage.hidden = true;
      setWorking(true);
      try {
        const data = await postJson(previewUrl, {});
        if (previewPlayer) {
          previewPlayer.src = `${data.url}${data.url.includes('?') ? '&' : '?'}_=${Date.now()}`;
          previewPlayer.hidden = false;
          previewPlayer.load();
        }
        if (previewPlaceholder) previewPlaceholder.hidden = true;
        if (previewDuration) previewDuration.textContent = `Aperçu prêt · ${Number(data.duration).toFixed(2)} s`;
        hasPreview = true;
        previewValid = true;
      } catch (error) {
        showError(error);
      } finally {
        setWorking(false);
        renderOperations();
      }
    });

    renderOperations();
    updateFreshness();
  } catch (error) {
    console.error('Erreur pendant l’initialisation du studio audio', error);
  }
})();
