// Only authoring surfaces: characters, images and world lorebooks.
document.addEventListener('DOMContentLoaded', () => {
  for (const name of ['cardgen', 'playground', 'lorebooks']) {
    document.getElementById(`tab-${name}`)?.addEventListener('click', () => {
      for (const item of ['cardgen', 'playground', 'lorebooks']) {
        const view = document.getElementById(`view-${item}`);
        const tab = document.getElementById(`tab-${item}`);
        if (view) view.style.display = item === name ? 'block' : 'none';
        tab?.classList.toggle('btn-primary', item === name);
        tab?.classList.toggle('btn-outline', item !== name);
      }
    });
  }
});
