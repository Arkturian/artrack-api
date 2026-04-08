/**
 * artrack-api frontend — entry point (post-library-refactor)
 *
 * The entire audio-guide runtime now lives in `@arkturian/audio-guide`.
 * This entry point is a thin composition layer that:
 *
 *   1. Reads runtime config from the <script id="__GPS_CONFIG__"> tag
 *      (injected by the FastAPI backend at /api/gps-tracker)
 *   2. Constructs an AudioGuide instance from the library
 *   3. Mounts the DefaultSkin (Tokyo-Night views) into #app
 *   4. Wires the skin's views to the AudioGuide's event manager
 *   5. Runs the lifecycle: init() → mountMap() → start()
 *
 * The views under src/view/default/* subscribe to the same bus events
 * the library emits internally — the library's public API is identical
 * to the old internal EventManager contract, so the views work as-is.
 *
 * This is the "dogfood" proof that the library's facade covers every
 * feature the original gps-tracker-api shipped with. If anything is
 * missing, this file won't compile and we know what to add to the
 * library before it ships to external consumers like tscheppa-ar-web.
 */

import { AudioGuide } from '@arkturian/audio-guide';
import type { AudioGuideConfig } from '@arkturian/audio-guide';
import { DefaultSkin } from '@/view/default';

function loadConfig(): AudioGuideConfig {
  const el = document.getElementById('__GPS_CONFIG__');
  if (!el || !el.textContent) {
    throw new Error('Missing #__GPS_CONFIG__ script tag — backend config injection failed');
  }
  return JSON.parse(el.textContent) as AudioGuideConfig;
}

async function bootstrap(): Promise<void> {
  const config = loadConfig();
  console.log('[artrack-frontend] boot with session:', config.session);

  // Instantiate the facade. All services, models, and the controller
  // logic are encapsulated inside the AudioGuide class.
  const guide = new AudioGuide(config);

  // Mount the Tokyo-Night skin and wire it to the library's event bus.
  // The skin lives in src/view/default/* and reads state + user actions
  // from the shared EventManager the library exposes.
  const skin = new DefaultSkin(config.session);
  const appRoot = document.getElementById('app');
  if (!appRoot) {
    throw new Error('#app element not found in the HTML shell');
  }
  skin.mount(appRoot);
  skin.bindEvents(guide.eventManager);

  // Initialize the library (resolves IACP server, wires orchestration)
  await guide.init();

  // Mount the Mapbox map into the #map container the DefaultSkin has
  // already placed in the DOM. The library's MapRenderer takes over
  // from here — user position, trail, POIs, destination, focus marker
  // all handled internally.
  const mapContainer = document.getElementById('map');
  if (mapContainer) {
    await guide.mountMap(mapContainer);
  }

  // Expose on window for devtools debugging
  (window as unknown as { __guide: AudioGuide }).__guide = guide;
}

bootstrap().catch((err: unknown) => {
  const msg = err instanceof Error ? err.message : String(err);
  const stack = err instanceof Error && err.stack ? err.stack : '';
  console.error('Boot failed:', err);
  document.body.innerHTML = `
    <pre style="color:#f7768e;background:#1a1b26;padding:2rem;font-family:monospace;min-height:100vh;margin:0;white-space:pre-wrap;word-break:break-word">
BOOT FAILED: ${msg}

${stack}
    </pre>
  `;
});
