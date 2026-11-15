(() => {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;

  let queued = false;
  let lastRequest = '';
  const measure = () => {
    queued = false;
    const bridge = window.pywebview?.api;
    if (!bridge?.window_sidebar_minimum || document.hidden) return;
    const {top, width} = sidebar.getBoundingClientRect();
    if (!width) return;

    // An auto-height copy excludes the flexible gap that anchors the profile at
    // the bottom and measures the sidebar with its current styles and text.
    const probe = sidebar.cloneNode(true);
    Object.assign(probe.style, {
      top: '0', bottom: 'auto', left: '-10000px', height: 'auto',
      width: `${width}px`, overflow: 'visible', visibility: 'hidden',
    });
    document.body.append(probe);
    const height = Math.ceil(top + probe.getBoundingClientRect().height);
    probe.remove();

    const ratio = window.devicePixelRatio;
    const request = `${height}:${ratio}`;
    if (request === lastRequest) return;
    lastRequest = request;
    bridge.window_sidebar_minimum(height, ratio).then((result) => {
      if (!result?.ok && lastRequest === request) lastRequest = '';
    }).catch(() => {
      if (lastRequest === request) lastRequest = '';
    });
  };

  const schedule = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(measure);
  };

  new ResizeObserver(schedule).observe(sidebar);
  new MutationObserver(schedule).observe(sidebar, {
    subtree: true, childList: true, characterData: true, attributes: true,
  });
  window.addEventListener('pywebviewready', schedule);
  window.addEventListener('resize', schedule);
  document.addEventListener('visibilitychange', schedule);
  document.fonts?.ready.then(schedule);
  schedule();
})();
