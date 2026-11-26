import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/inter/opsz.css';
import 'maplibre-gl/dist/maplibre-gl.css';
import './styles.css';
import './map/map.css';
import './live/live.css';
import { initTheme } from './lib/theme';
import { App } from './App';

initTheme();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
