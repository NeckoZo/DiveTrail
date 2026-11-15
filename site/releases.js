(() => {
  const project = 'NeckoZo/DiveTrail';
  const releases = `https://github.com/${project}/releases`;
  const setup = document.getElementById('setup-download');
  const portable = document.getElementById('portable-download');
  const notes = document.getElementById('release-notes');
  const controls = document.getElementById('downloads');

  async function refreshRelease() {
    const response = await fetch(`https://api.github.com/repos/${project}/releases/latest`, {
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
    });
    if (!response.ok) return;

    const release = await response.json();
    const tag = release.tag_name;
    if (typeof tag !== 'string' || !/^v\d+\.\d+\.\d+$/.test(tag) ||
        release.prerelease || release.draft || !Array.isArray(release.assets)) return;

    const version = tag.slice(1);
    const installer = `DiveTrail-${version}-setup-win-x64.exe`;
    const zip = `DiveTrail-${version}-portable-win-x64.zip`;
    if (![installer, zip].every(name => release.assets.some(asset => asset.name === name))) return;

    setup.href = `${releases}/download/${tag}/${installer}`;
    portable.href = `${releases}/download/${tag}/${zip}`;
    notes.href = `${releases}/tag/${tag}`;
    notes.textContent = `${tag} 更新说明 ↗`;
    controls.setAttribute('aria-label', `下载 DiveTrail ${version}`);
  }

  // Keep the published v1.0.0 links available if the API is unreachable.
  refreshRelease().catch(() => {});
})();
