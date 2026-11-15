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
      const input = form?.querySelector('[data-project-renam