// Applies the stored theme before first paint, so a dark choice never flashes light.
// A classic blocking script (the Content-Security-Policy allows no inline script); it
// mirrors src/lib/theme.ts, which keeps the theme in step after the app starts.
/* global document, window */
(function () {
  var choice = null;
  try {
    choice = localStorage.getItem('rp.theme');
  } catch {
    /* storage blocked: follow the system */
  }
  var root = document.documentElement;
  if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
  else root.removeAttribute('data-theme');
  var dark = choice === 'dark' || (choice !== 'light' && !!window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', dark ? '#080c12' : '#f4f6f8');
})();
