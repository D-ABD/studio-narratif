(() => {
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
      card.querySelector('.word-count').textContent = count;
      card.querySelector('.duration-estimate').textContent = count ? `~${Math.max(1, Math.ceil(count / 2.35))}` : '0';
      textarea.dataset.dirty = textarea.value !== textarea.defaultValue ? 'true' : 'false';
    };
    textarea.addEventListener('input', update);
    update();
  });
  document.querySelectorAll('.segment-form select[name="motion"]').forEach(select => {
    select.dataset.initialValue = select.value;
    select.addEventListener('change', () => {
      select.form.dataset.motionDirty = select.value !== select.dataset.initialValue ? 'true' : 'false';
    });
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
  let recordedChunks = [];

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
      recorder.stop();
      recordButton.textContent = '● Enregistrer avec le micro';
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      recordStatus.textContent = 'L’enregistrement micro n’est pas disponible dans ce navigateur.';
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      recordedChunks = [];
      recorder = new MediaRecorder(stream);
      recorder.addEventListener('dataavailable', event => {
        if (event.data.size) recordedChunks.push(event.data);
      });
      recorder.addEventListener('stop', () => {
        const blob = new Blob(recordedChunks, {type: recorder.mimeType || 'audio/webm'});
        const extension = blob.type.includes('mp4') ? 'm4a' : 'webm';
        const file = new File([blob], `voix-enregistree.${extension}`, {type: blob.type});
        const transfer = new DataTransfer();
        transfer.items.add(file);
        sampleInput.files = transfer.files;
        showSample(file);
        stream.getTracks().forEach(track => track.stop());
        recordStatus.textContent = 'Écoute l’enregistrement avant de sauvegarder.';
      });
      recorder.start();
      recordButton.textContent = '■ Arrêter l’enregistrement';
      recordStatus.textContent = 'Enregistrement en cours…';
    } catch (error) {
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
  const voiceIdInput = document.querySelector('#prepareForm input[name=voice_id]');
  projectVoice?.addEventListener('change', () => {
    if (voiceIdInput) voiceIdInput.value = projectVoice.value;
  });

  const prepareForm = document.querySelector('#prepareForm');
  const assembleForm = document.querySelector('#assembleForm');
  if (!prepareForm && !assembleForm) return;
  const button = document.querySelector('#prepareButton, #assembleButton');
  const status = document.querySelector('#jobStatus');
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

  const poll = async () => {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch(`/projects/${projectId}/status/`, {cache: 'no-store'});
      if (!response.ok) throw new Error('État indisponible');
      const data = await response.json();
      status.textContent = data.error || data.message || 'Traitement en cours…';
      progress.value = Math.max(0, Math.min(100, Number(data.progress) || 0));
      progress.hidden = data.status !== 'processing';
      details.hidden = data.status !== 'processing';
      currentSegment.textContent = `Segment ${data.current_segment}/${data.total_segments}`;
      elapsedTime.textContent = formatElapsed(data.elapsed_seconds);
      if (data.status === 'queued') {
        status.className = 'job-status';
        status.textContent = data.queue_position
          ? `En attente · position ${data.queue_position} sur ${data.queue_count}. Le worker traitera les projets dans l’ordre.`
          : 'Ajouté à la file. En attente du worker…';
        button.disabled = true;
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
        button.disabled = false;
        return;
      }
      if (data.status === 'error') {
        status.className = 'job-status error';
        button.disabled = false;
        return;
      }
      if (data.status === 'processing') {
        button.disabled = true;
        window.setTimeout(() => { polling = false; poll(); }, 1300);
        return;
      }
      button.disabled = false;
    } catch {
      status.className = 'job-status error';
      status.textContent = 'Impossible de lire l’état du traitement. Recharge le projet pour vérifier.';
      button.disabled = false;
    } finally {
      polling = false;
    }
  };

  const saveDirtySegments = async () => {
    for (const form of document.querySelectorAll('.segment-form')) {
      const textarea = form.querySelector('textarea');
      if (textarea?.dataset.dirty !== 'true' && form.dataset.motionDirty !== 'true') continue;
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      if (!response.ok) throw new Error('Impossible d’enregistrer un segment modifié.');
      if (textarea) {
        textarea.defaultValue = textarea.value;
        textarea.dataset.dirty = 'false';
      }
      form.dataset.motionDirty = 'false';
    }
    if (videoOptionsForm?.dataset.dirty === 'true') {
      const response = await fetch(videoOptionsForm.action, {method: 'POST', body: new FormData(videoOptionsForm)});
      if (!response.ok) throw new Error('Impossible d’enregistrer les réglages vidéo.');
      videoOptionsForm.dataset.dirty = 'false';
    }
  };

  const submitJob = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const isAssembly = form === assembleForm;
    if (!isAssembly && !voiceIdInput?.value) {
      status.className = 'job-status error';
      status.textContent = 'Choisis d’abord une voix autorisée.';
      return;
    }
    button.disabled = true;
    status.className = 'job-status';
    status.textContent = isAssembly ? 'Enregistrement des réglages…' : 'Enregistrement des textes…';
    try {
      await saveDirtySegments();
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Le traitement n’a pas pu démarrer.');
      progress.hidden = false;
      details.hidden = false;
      status.textContent = 'Ajout du projet à la file…';
      polling = false;
      poll();
    } catch (error) {
      status.className = 'job-status error';
      status.textContent = error.message;
      button.disabled = false;
    }
  };
  prepareForm?.addEventListener('submit', submitJob);
  assembleForm?.addEventListener('submit', submitJob);

  if (['processing', 'queued'].includes(document.body.dataset.projectStatus)) {
    button.disabled = true;
    progress.hidden = document.body.dataset.projectStatus !== 'processing';
    details.hidden = document.body.dataset.projectStatus !== 'processing';
    poll();
  }
})();
