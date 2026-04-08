import type { EventManager } from '@arkturian/audio-guide';

/**
 * IView — contract every view class must implement so skins can be
 * swapped wholesale. Views must NOT hold application state or contain
 * business logic — they only:
 *   - read DOM elements in `mount()`
 *   - subscribe to bus events in `bindEvents()`
 *   - emit user-intent events (prefixed `user:`) on the bus
 *   - clean up DOM + subscriptions in `dispose()`
 */
export interface IView {
  /** Query and cache DOM elements the view needs */
  mount(root: HTMLElement): void;

  /** Subscribe to bus events (state-driven rendering) + bind DOM events */
  bindEvents(bus: EventManager): void;

  /** Remove bus subscriptions and any created DOM state */
  dispose(): void;
}
