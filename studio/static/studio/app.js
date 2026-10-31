(() => {
  const csrfToken = () => document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const wordCount = value => (value.trim() ? value.trim().split(/\s+/).length : 0);

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
      if (!card || event.target.closest('textarea,button,input,a,select')) {
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
  if (!prepareForm) return;
  const button = document.querySelector('#prepareButton');
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
    if (!box || !audio) return;
    audio.src = audioUrl;
    audioDownload.href = audioUrl;
    srtDownload.href = subtitlesUrl;
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
      if (data.status === 'ready') {
        status.className = 'job-status success';
        status.textContent = data.message || 'Narration prête à écouter et à télécharger.';
        showFullExport(data.audio_url, data.subtitles_url);
        button.disabled = false;
        return;
      }
      if (data.status === 'error') {
        status.className = 'job-status error';
        button.disabled = false;
        return;
      }
      if (data.status === 'processing') {
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
    for (const textarea of document.querySelectorAll('.segment-form textarea[data-dirty="true"]')) {
      const form = textarea.closest('form');
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      if (!response.ok) throw new Error('Impossible d’enregistrer un segment modifié.');
      textarea.defaultValue = textarea.value;
      textarea.dataset.dirty = 'false';
    }
  };

  prepareForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (!voiceIdInput?.value) {
      status.className = 'job-status error';
      status.textContent = 'Choisis d’abord une voix autorisée.';
      return;
    }
    button.disabled = true;
    status.className = 'job-status';
    status.textContent = 'Enregistrement des textes…';
    try {
      await saveDirtySegments();
      const response = await fetch(prepareForm.action, {method: 'POST', body: new FormData(prepareForm)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Le traitement n’a pas pu démarrer.');
      progress.hidden = false;
      details.hidden = false;
      status.textContent = 'Mise en attente du moteur vocal…';
      polling = false;
      poll();
    } catch (error) {
      status.className = 'job-status error';
      status.textContent = error.message;
      button.disabled = false;
    }
  });

  if (document.body.dataset.projectStatus === 'processing') {
    button.disabled = true;
    progress.hidden = false;
    details.hidden = false;
    poll();
  }
})();
